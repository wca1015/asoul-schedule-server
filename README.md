# asoul-schedule-server

A-SOUL 直播日程服务 —— 粉丝向 B 站直播日程表 App 的服务器端。

三条数据管道（A/B/C），最终产物是两个静态 JSON（国内客户端从阿里云 OSS 读取）：

| 管道 | 数据源 | 时效 | 发布模式 | 产物 |
|:---|:---|:---|:---|:---|
| **A：周程表** | 官号图文动态（1 个账号） | 天级 | 全自动（校验通过即发布，飞书通知） | `latest.json` |
| **B：突击直播** | 成员个人号（5 个账号） | 分钟级 | 全自动（校验通过即发布，飞书通知） | `flash.json` |
| **C：录播回填** | 成员投稿列表（`/x/space/wbi/arc/search`） | 小时级 | 全自动（时长+时间窗+标题匹配，幂等回填） | `latest.json` 的 `recording_bvid` |

核心原则：**极简架构**（无数据库、无后端服务）、**全自动**（三条管道均校验通过即发布，飞书仅通知）、**零运维**（Serverless 调度）、月成本 < ¥10。

完整技术方案见 [`server.markdown`](server.markdown)。

## 架构

```
┌─ 管道A：周程表 ────────────────────────────────────────┐
│ 官号图片动态 → 抓取(30min) → Qwen-VL识别 → 校验          │
│             → 自动发布 latest.json（飞书通知已发布）      │
├─ 管道B：突击直播 ──────────────────────────────────────┤
│ 成员动态(5min轮询) → 关键词预筛 → 规则提取/AI识别 → 校验  │
│                    → 自动发布 flash.json（飞书通知已发布）│
├─ 管道C：录播回填 ──────────────────────────────────────┤
│ 已结束且缺录播的事件 → 扫描成员投稿(30min) → 时长+时间窗   │
│                     +标题匹配 → 回填 latest.json 的       │
│                      recording_bvid（幂等，全自动）       │
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
│   ├── flash_monitor.yml       # 管道B 定时任务（每5分钟）
│   └── recording_backfill.yml  # 管道C 定时任务（每30分钟）
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
│   ├── recording_backfill.py   # 管道C：录播 bvid 回填（幂等，时长+联动检测）
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
python scripts/test_smoke.py          # 离线冒烟测试，11 项全绿即环境就绪
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
| `FEISHU_WEBHOOK` | ✅ | 飞书自定义机器人，已发布通知/告警（缺失则只打印日志） |
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
| `data/draft.json` | 周程表发布临时草稿（发布后即清理，不同步） |
| `data/last_dynamic_id.txt` / `data/last_flash_{uid}.txt` | 抓取游标 |
| `archive/{week_start}.json` | 历史周程归档 |

客户端对接要点：

- 从 `https://<bucket>.<endpoint>/latest.json` 与 `/flash.json` 拉取（对象级公共读）
- 通过 `version` 字段判断本地缓存是否需更新；断网时展示上次缓存
- 事件 `auto_published` 字段保留为 `false`（所有事件均为系统自动发布，无需"待确认"标识）

## 发布流程（全自动）

三条管道均为「校验通过即**自动发布**」，飞书只做已发布通知，无需任何人工操作：

**周程表**：官号动态 → VLM 识别 → 校验 → 直接发布 `latest.json`（含归档）→ 飞书「✅ 新周程表已自动发布」

**突击直播**：成员动态 → 三级识别 → 校验 → 直接发布 `flash.json`（按 `source_dynamic_id` 去重、幂等）→ 飞书「⚡ 突击直播已自动发布」

**录播回填**：已结束且缺录播的事件 → 扫描成员投稿 → 时长+时间窗+标题匹配 → 回填 `recording_bvid`（幂等）→ 飞书「✅ 录播回填完成」

> 注：飞书自定义机器人的卡片按钮为展示用，无回调能力；本方案已完全取消人工审核环节，如遇特殊情况需人工修正数据，可直接编辑 `data/` 下 JSON 后 git push，或手动运行 `publish.py --manual` 重发。

## 异常处理策略

- 识别/校验失败 → **不推进游标**，下一轮自动重试，同时飞书告警
- 单条动态处理异常 → **推进游标**，避免"毒消息"卡死整个管道
- B 站风控（412/403） → 静默跳过，下一轮重试
- OSS 同步失败 → 仅告警不阻断（仓库数据完整，可手动重跑 `python scripts/sync_oss.py`）

## 测试

```powershell
python scripts/test_smoke.py
```

覆盖：规则时间提取、事件 ID 生成、突击直播自动发布与去重、过期清理、数据校验、发布字段兜底、版本号单调递增（周程+突击）、录播回填匹配、联合直播不误配。全部离线，不请求网络、不调用 AI。

## 成本

| 项目 | 月费用 |
|:---|:---|
| GitHub Actions / Cloudflare Workers Cron | ¥0 |
| Qwen-VL-Max API（合计） | ≈ ¥3~7 |
| 阿里云 OSS（个人规模） | ≈ ¥0~1 |
| 飞书机器人 | ¥0 |
| **合计** | **< ¥10**（极端情况 < ¥35） |
