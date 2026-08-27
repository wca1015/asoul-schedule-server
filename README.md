# asoul-schedule-server

A-SOUL 直播日程服务 —— 粉丝向 B 站直播日程表 App 的服务器端。

双管道数据生产线，最终产物是两个静态 JSON（国内客户端从阿里云 OSS 读取）：

| 管道 | 数据源 | 时效 | 审核模式 | 产物 |
|:---|:---|:---|:---|:---|
| **A：周程表** | 官号图文动态（1 个账号） | 天级 | 人工审核后发布 | `latest.json` |
| **B：突击直播** | 成员个人号（5 个账号） | 分钟级 | 人工审核 + 10 分钟超时自动发布 | `flash.json` |

核心原则：**极简架构**（无数据库、无后端服务）、**半自动化**（AI 提取 + 人工审核兜底）、**零运维**（Serverless 调度）、月成本 < ¥10。

完整技术方案见 [`server.markdown`](server.markdown)。

## 架构

```
┌─ 管道A：周程表 ────────────────────────────────────────┐
│ 官号图片动态 → 抓取(30min) → Qwen-VL识别 → 校验          │
│             → draft.json → 人工审核 → latest.json       │
├─ 管道B：突击直播 ──────────────────────────────────────┤
│ 成员动态(5min轮询) → 关键词预筛 → 规则提取/AI识别 → 校验  │
│                    → flash_draft.json → 审核(10min超时   │
│                      自动发布+⚠️标记) → flash.json        │
└──────────────────┬─────────────────────────────────────┘
                   │ Actions 每轮自动提交并镜像
                   ▼
   GitHub 仓库 data/（唯一事实来源，可回滚）
                   │
                   ▼  sync_oss.py
   阿里云 OSS（客户端实际读取：就近、快速）
```

## 文件结构

```
├── .github/workflows/
│   ├── cron_schedule.yml       # 管道A 定时任务（每30分钟）
│   └── flash_monitor.yml       # 管道B 定时任务（每5分钟）
├── cloudflare/workers/
│   └── flash_cron.js           # 可选：秒级触发管道B（兜底走 GitHub cron）
├── config/
│   └── members.yaml            # 成员UID + 关键词（换号/改名只改这里）
├── scripts/
│   ├── main.py                 # 主流程编排：--mode schedule / flash
│   ├── fetch_dynamic.py        # 管道A：官号动态抓取
│   ├── recognize.py            # 管道A：周程表 VLM 识别
│   ├── flash_monitor.py        # 管道B：多账号抓取（防风控）
│   ├── flash_recognize.py      # 管道B：三级识别（关键词→规则→AI）
│   ├── flash_manager.py        # 管道B：去重 / 48h 过期清理
│   ├── auto_publish_timeout.py # 管道B：审核超时自动发布
│   ├── validate.py             # 共享：数据校验（非法数据绝不发布）
│   ├── notify.py               # 共享：飞书卡片通知
│   ├── publish.py              # 共享：草稿 → 正式文件发布
│   ├── sync_oss.py             # 共享：正式数据镜像到国内 OSS
│   ├── common.py               # 共享：路径/时区/环境
│   └── test_smoke.py           # 离线冒烟测试（不走网络不调AI）
├── data/                       # 运行数据（入库，Actions 靠 git 持久化）
└── archive/                    # 历史周程表归档
```

## 快速开始

```powershell
pip install -r requirements.txt
python scripts/test_smoke.py          # 离线冒烟测试，7 项全绿即环境就绪
python scripts/main.py --mode flash   # 手动跑一轮突击直播管道
```

常驻模式（轻量服务器备选方案）：

```powershell
python scripts/main.py --mode flash --loop --interval 300
```

## 环境变量

| 变量 | 必需 | 说明 |
|:---|:---:|:---|
| `DASHSCOPE_API_KEY` | ✅ | 通义千问 VLM API（Qwen-VL-Max） |
| `FEISHU_WEBHOOK` | ✅ | 飞书自定义机器人，审核通知/告警（缺失则只打印日志） |
| `BILIBILI_UID` | ✅ | 官号 UID（管道A；也可用 members.yaml 的 official_uid） |
| `BILIBILI_COOKIE` | 建议 | 降低 B 站风控概率 |
| `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` / `OSS_ENDPOINT` / `OSS_BUCKET` | 可选 | 国内数据分发；未配置时自动跳过同步 |
| `OSS_PREFIX` | 可选 | 对象键前缀（多环境隔离用） |

> GitHub Actions 场景下在仓库 Settings → Secrets and variables → Actions 中配置。

## 数据文件

| 文件 | 说明 |
|:---|:---|
| `data/latest.json` | 周程表（每周更新，客户端每小时轮询） |
| `data/flash.json` | 突击直播（实时，客户端每 5 分钟轮询） |
| `data/draft.json` / `data/flash_draft.json` | 待审核草稿（内部数据，不同步） |
| `data/last_dynamic_id.txt` / `data/last_flash_{uid}.txt` | 抓取游标 |
| `archive/{week_start}.json` | 历史周程归档 |

客户端对接要点：

- 从 `https://<bucket>.<endpoint>/latest.json` 与 `/flash.json` 拉取（对象级公共读）
- 通过 `version` 字段判断本地缓存是否需更新；断网时展示上次缓存
- `auto_published: true` 的事件需展示 "⚠️ 待确认" 标识

## 审核流程

**周程表**：飞书卡片通知 → 管理员核对 → 确认后运行发布脚本：

```powershell
python scripts/publish.py --target schedule
```

**突击直播**：飞书紧急通知（@所有人）→ 10 分钟内确认发布；超时未审核则**自动发布**（事件带 `auto_published: true` 标记），宁可发布可能有误的数据也不能漏发。

> 注：飞书自定义机器人的卡片按钮为展示用，无回调能力；实际发布动作由超时机制或手动运行 `publish.py` 完成。

## 异常处理策略

- 识别/校验失败 → **不推进游标**，下一轮自动重试，同时飞书告警
- 单条动态处理异常 → **推进游标**，避免"毒消息"卡死整个管道
- B 站风控（412/403） → 静默跳过，下一轮重试
- OSS 同步失败 → 仅告警不阻断（仓库数据完整，可手动重跑 `python scripts/sync_oss.py`）

## 测试

```powershell
python scripts/test_smoke.py
```

覆盖：规则时间提取、事件 ID 生成、超时自动发布、过期清理、数据校验、草稿合并。全部离线，不请求网络、不调用 AI。

## 成本

| 项目 | 月费用 |
|:---|:---|
| GitHub Actions / Cloudflare Workers Cron | ¥0 |
| Qwen-VL-Max API（合计） | ≈ ¥3~7 |
| 阿里云 OSS（个人规模） | ≈ ¥0~1 |
| 飞书机器人 | ¥0 |
| **合计** | **< ¥10**（极端情况 < ¥35） |
