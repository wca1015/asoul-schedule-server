# asoul-schedule-server

A-SOUL 直播日程服务 —— 粉丝向 B 站直播日程表 App 的服务器端。

三条数据管道（A/B/C）持续产出静态 JSON（国内客户端从阿里云 OSS 读取）：

| 管道 | 数据源 | 时效 | 发布模式 | 产物 |
|:---|:---|:---|:---|:---|
| **A：周程表** | 官号图文动态（1 个账号） | 天级 | 全自动（校验通过即发布，飞书通知） | `latest.json` |
| **B：突击直播** | ① 成员及官号动态（6 个账号，5min 轮询）② 成员直播间状态（兜底通道） | 分钟级 | 全自动（动态三级识别 / 直播状态判定 → 校验 → 发布，飞书通知） | `flash.json`；已播突击随后并入 `latest.json` |
| **C：录播回填 + 突击回扫** | 成员投稿列表（`/x/space/wbi/arc/search`）+ 成员近期动态的直播预约卡片 | 小时级 | 全自动（时长+时间窗+标题匹配，幂等回填） | `latest.json` 的 `recording_bvid` / 追回的已播突击条目 |

核心原则：**极简架构**（无数据库、无后端服务）、**全自动**（三条管道均校验通过即发布，飞书仅通知）、**零运维**（Serverless 调度）、月成本 < ¥10。

完整技术方案见 [`server.markdown`](server.markdown)。

## 架构

```
┌─ 管道A：周程表 ────────────────────────────────────────┐
│ 官号图片动态 → 抓取(30min) → Qwen-VL识别 → 校验          │
│             → 自动发布 latest.json（飞书通知已发布）      │
├─ 管道B：突击直播 ──────────────────────────────────────┤
│ 通道1 成员动态(5min轮询) → 关键词预筛 → 规则/AI 三级识别  │
│       → 校验 → flash.json（动态接口 412 时通时断）        │
│ 通道2 成员直播间状态(getRoomPlayInfo，匿名稳定) →         │
│       新开播判定 → 周程表时间窗复核（日程内直播不算突击）   │
│       → flash.json（兜底）                              │
│ 已播突击并入周程表 → latest.json（幂等，merged_flash_ids） │
├─ 管道C：录播回填 + 突击回扫 ────────────────────────────┤
│ 已结束且缺录播的事件 → 扫描成员投稿(30min) → 时长+时间窗   │
│                     +标题匹配 → 回填 recording_bvid      │
│ 突击回扫 backfill_flash：不看游标扫近期预约卡片(30min)    │
│                     → 追回漏抓的已播突击 → latest.json   │
└──────────────────┬─────────────────────────────────────┘
                   │ Actions 每轮自动提交；数据 push 即触发镜像
                   ▼
   GitHub 仓库 data/（唯一事实来源，可回滚、可审计）
                   │
                   ▼  sync_oss.py（含超出回看窗口的归档清理）
   阿里云 OSS（客户端实际读取：就近、快速）

触发：GitHub Actions cron（周程表 30min / 突击 5min / 录播+回扫 30min）
     + 可选 Cloudflare Worker 秒级触发 flash_monitor（GitHub 免费 cron 排队延迟大）
```

## 文件结构

```
├── .github/workflows/
│   ├── cron_schedule.yml       # 管道A 定时任务（每30分钟）
│   ├── flash_monitor.yml       # 管道B 定时任务（每5分钟，也供 CF Worker 通过 API 触发）
│   ├── recording_backfill.yml  # 管道C 定时任务（每30分钟，含突击回扫 backfill_flash）
│   ├── data_sync.yml           # data/ 或 archive/ 变更 push 后自动镜像 OSS
│   ├── upload_app.yml          # 手动：发布新版 App 的版本清单到 OSS
│   └── follow_members.yml      # 手动：让小号关注全部成员（一次性工具）
├── cloudflare/workers/
│   ├── flash_cron.js           # 可选：秒级触发管道B（兜底走 GitHub cron）
│   └── bili_proxy.js           # 可选：B 站 API 反代（换出口 IP 绕 412）
├── config/
│   └── members.yaml            # 成员 UID/member_key/直播间 + 关键词 + 录播回填配置（换号/改名只改这里）
├── scripts/
│   ├── main.py                 # 主流程编排：--mode schedule / flash（双通道 + 并入周程表）
│   ├── fetch_dynamic.py        # 管道A：官号动态抓取
│   ├── recognize.py            # 管道A：周程表 VLM 识别
│   ├── flash_monitor.py        # 管道B：多账号动态抓取（防风控）
│   ├── flash_recognize.py      # 管道B：三级识别（关键词→规则→AI）
│   ├── flash_manager.py        # 管道B：去重 / 48h 过期清理
│   ├── live_monitor.py         # 管道B：直播间状态兜底通道 + 日程窗复核
│   ├── schedule_flash.py       # 管道B：已播突击幂等并入周程表
│   ├── recording_backfill.py   # 管道C：录播 bvid 回填（幂等，时长+联动检测）
│   ├── backfill_flash.py       # 管道C：突击回扫（不看游标，追回漏抓的预约动态）
│   ├── validate.py             # 共享：数据校验（非法数据绝不发布）
│   ├── notify.py               # 共享：飞书卡片通知
│   ├── publish.py              # 共享：草稿 → 正式文件发布
│   ├── sync_oss.py             # 共享：正式数据镜像到国内 OSS（含归档过期清理）
│   ├── upload_app.py           # 共享：App 版本清单上传 OSS
│   ├── bili_session.py         # 共享：B 站会话/签名/反代/Cookie失效告警（防风控统一入口）
│   ├── follow_members.py       # 工具：BILIBILI_COOKIE 账号关注全部成员
│   ├── common.py               # 共享：路径/时区/环境
│   └── test_smoke.py           # 离线冒烟测试（不走网络不调AI）
├── data/                       # 运行数据（入库，Actions 靠 git 持久化）
└── archive/                    # 历史周程表归档（OSS 仅保留最近 4 周）
```

## 业务规则速览

- **突击直播判定**：以「直播预约/预告动态」为准；直播间状态裸检测仅作兜底，且必须通过周程表时间窗复核（成员单播 −30/+150 分钟、任一场次紧贴 −5/+20 分钟的不算突击）——成员常提前约 10 分钟开播，不能当作突击
- **突击并入周程表**：动态通道事件（有真实动态 ID）已开播即并入；直播间兜底事件（`live_` 前缀）需「已结束 + 复核不在日程窗内」才并入；同 (日期, 成员, 时间 ±10min) 判重，`data/merged_flash_ids.txt` 幂等
- **漏抓追回**：`backfill_flash.py` 不看游标扫成员近期预约卡片（仅认带确定开播时间的），覆盖动态通道 412 与「仅粉丝可见」造成的漏抓
- **双人组合**：一期双人同播的 `member` 为组合键（两 key 按 贝拉<嘉然<乃琳 字典序用 `_` 连接，如 `bella_nailin`），App 侧同时命中两名参与成员
- **录播回填**：仅匹配「完整回放」（时长下限 + 上传时间窗 + 标题/日期校验），双人联动录像不归属个人单播

## 快速开始

```powershell
pip install -r requirements.txt
python scripts/test_smoke.py          # 离线冒烟测试，18 项全绿即环境就绪
python scripts/main.py --mode flash   # 手动跑一轮突击直播管道（动态 + 直播间双通道）
python scripts/backfill_flash.py --dry-run   # 预览本周可追回的已播突击（不写文件）
```

常驻模式（轻量服务器备选方案）：

```powershell
python scripts/main.py --mode flash --loop --interval 300
```

> 工具：`python scripts/follow_members.py`（需 `BILIBILI_COOKIE`）让抓取小号关注全部成员，消除「仅粉丝可见」动态导致的漏抓；只需跑一次。

## 环境变量

| 变量 | 必需 | 说明 |
|:---|:---:|:---|
| `DASHSCOPE_API_KEY` | ✅ | 通义千问 VLM API（Qwen-VL-Max） |
| `FEISHU_WEBHOOK` | ✅ | 飞书自定义机器人，已发布通知/告警（缺失则只打印日志） |
| `BILIBILI_UID` | ✅ | 官号 UID（管道A；也可用 members.yaml 的 official_uid） |
| `BILIBILI_COOKIE` | 建议 | 登录 Cookie，显著降低风控概率；失效时自动飞书告警（见「防风控配置」） |
| `BILI_PROXY_URL` | 可选 | Cloudflare Worker 反代地址（换出口 IP 规避 412；实测 workers.dev 出口收益有限，见「防风控配置」） |
| `BILI_PROXY_KEY` | 可选 | 反代共享密钥（防反代被滥用）：需与 Worker 侧 secret 同名同值，Python 侧经 `X-Bili-Key` 头携带 |
| `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` / `OSS_ENDPOINT` / `OSS_BUCKET` | 可选 | 国内数据分发；未配置时自动跳过同步 |
| `OSS_PREFIX` | 可选 | 对象键前缀（多环境隔离用） |

> GitHub Actions 场景下在仓库 Settings → Secrets and variables → Actions 中配置。

## 防风控配置（可选）

B 站对「匿名 + 共享数据中心 IP」（如 GitHub Actions）的风控较严，会返回 412 / -352。
两条缓解手段建议都做，代码已全部内置支持（`scripts/bili_session.py` 统一封装，
三条管道 A/B/C 无需各自改代码）：

### 1. 登录 Cookie（最有效）

用一个不常用的 B 站小号登录（F12 → Network → 任意接口 → 复制 `Cookie` 请求头），
把关键字段配置到 Actions Secret `BILIBILI_COOKIE`：

```
SESSDATA=xxx; bili_jct=xxx; DedeUserID=xxx; DedeUserID__ckMd5=xxx
```

- SESSDATA 会过期（约 1 个月起，视是否勾选「记住我」），过期后接口回退匿名态；
  系统检测到 `code=-101`（未登录）会**自动飞书告警**提醒更新（6 小时节流，避免刷屏）
- 只做低频只读请求，小号基本无风险

### 2. Cloudflare Worker 反代（换出口 IP）

`cloudflare/workers/bili_proxy.js` 是一个透明转发 Worker，把请求从 GitHub Actions
的共享 IP 改道到 Cloudflare 出口 IP，规避 412。

部署：

1. Cloudflare Dashboard 创建 Worker（或 `wrangler init`），粘贴 `bili_proxy.js` 内容并部署
2. Actions Secrets 配置 `BILI_PROXY_URL=https://<你的worker域名>`
3. （可选，防反代被滥用）Worker 侧 `wrangler secret put BILI_PROXY_KEY` 设置共享密钥，
   并在 Actions Secret 配置同名 `BILI_PROXY_KEY`（Python 侧经 `X-Bili-Key` 头携带；
   Worker 未设密钥时无需配置，设了密钥但两边不一致会被 403 拒绝）

> ⚠️ 实测结论（2026-09）：`*.workers.dev` 出口 IP 已被 B 站风控加重——连匿名直连可用的
> 接口（如 nav）经其访问也会变成 412。反代当前收益为负，建议直接删除 `BILI_PROXY_URL` 走直连
> （Cookie 才是主要手段）；如仍需要反代，请使用自备域名 / 非 workers.dev 的出口。

> 注意：匿名模式（未配 Cookie）下，WBI 签名会自动带上 `buvid3` 参与签名，
> 并缓存 WBI key（1 小时 TTL）减少 nav 请求；签名返回 -352 时自动清缓存重取。

## 数据文件

| 文件 | 说明 |
|:---|:---|
| `data/latest.json` | 周程表（每周更新 + 已播突击并入；客户端每小时轮询） |
| `data/flash.json` | 突击直播（实时，客户端每 5 分钟轮询；48h 自动清理） |
| `data/draft.json` | 发布临时草稿（发布后即清理，不同步） |
| `data/last_dynamic_id.txt` / `data/last_flash_{uid}.txt` | 抓取游标 |
| `data/live_state_{room_id}.txt` | 直播间兜底通道的最近开播记录（防重复上报） |
| `data/merged_flash_ids.txt` | 已并入周程表的突击事件（幂等，防重复并入） |
| `data/alert_*.txt` | 飞书告警节流状态（Cookie 失效、feed 空等） |
| `archive/{week_start}.json` | 历史周程归档（OSS 仅保留最近 4 周） |

客户端对接要点：

- 从 `https://<bucket>.<endpoint>/` 拉取 `latest.json`、`flash.json`、`week/{week_start}.json`（对象级公共读），启动时另拉 `app_version.json` 做更新检查
- 通过 `version` 字段判断本地缓存是否需更新；断网时展示上次缓存
- `week/` 归档仅在「当前周 + 往前 4 周」窗口内维护（与 App 最多回看 4 周对齐），超期对象由 `sync_oss.py` 清理
- `latest.json` 中可能混有「已播突击」条目（来自 `schedule_flash` / `backfill_flash` 并入），字段契约与普通日程一致
- 一期双人直播的 `member` 为组合键（`bella_jiaran` / `bella_nailin` / `jiaran_nailin`），App 侧解析为两名参与成员
- 事件 `auto_published` 字段保留为 `false`（所有事件均为系统自动发布，无需"待确认"标识）

## 发布流程（全自动）

三条管道均为「校验通过即**自动发布**」，飞书只做已发布通知，无需任何人工操作：

**周程表**：官号动态 → VLM 识别 → 校验 → 直接发布 `latest.json`（含归档）→ 飞书「✅ 新周程表已自动发布」

**突击直播**：成员动态 → 三级识别 → 校验 → 直接发布 `flash.json`（按 `source_dynamic_id` 去重、幂等）→ 飞书「⚡ 突击直播已自动发布」；直播间状态兜底通道无预告也可发布；已播突击随后由 `schedule_flash`（每轮）/ `backfill_flash`（每30min）幂等并入 `latest.json` 日历

**录播回填**：已结束且缺录播的事件 → 扫描成员投稿 → 时长+时间窗+标题匹配 → 回填 `recording_bvid`（幂等）→ 飞书「✅ 录播回填完成」

> 注：飞书自定义机器人的卡片按钮为展示用，无回调能力；本方案已完全取消人工审核环节，如遇特殊情况需人工修正数据，可直接编辑 `data/` 下 JSON 后 git push，或手动运行 `publish.py --manual` 重发。

### App 版本清单发布（手动触发）

APK 分发与 App 内更新走独立流程（阿里云 OSS 禁止默认域名直接分发 APK，直接 GET 会返回 `ApkDownloadForbidden`）：

1. 构建 Release APK：`gradlew assembleRelease`（产物 `app/build/outputs/apk/release/app-release.apk`）
2. 上传到 GitHub Releases（资产名固定）：

   ```powershell
   gh release create vX.Y path/to/app-release.apk#app-release-X.Y.apk `
       --repo wca1015/AsoulSchedule-APP --title "vX.Y"
   ```

3. 更新 OSS 版本清单（二选一）：
   - Actions 手动触发：`gh workflow run upload_app.yml -f version_code=N -f version_name=X.Y -f notes="..."`
   - 本地运行：`python scripts/upload_app.py --version-code N --version-name X.Y --notes "..."`（需 OSS 四环境变量）

客户端启动后拉取 `app_version.json`，`version_code` 高于本地时弹更新弹窗，下载地址指向 GitHub Release 资产。

## 异常处理策略

- 识别/校验失败 → **不推进游标**，下一轮自动重试，同时飞书告警
- 单条动态处理异常 → **推进游标**，避免"毒消息"卡死整个管道
- B 站风控（412/403） → 静默跳过，下一轮重试
- 动态通道 412 / feed 为空 → 直播间状态兜底通道继续工作；连续空 feed 触发飞书告警（文件节流）
- 直播间兜底判定为新开播 → 先用周程表时间窗复核，命中日程窗的不上报为突击
- 突击并入周程表失败/重复 → `merged_flash_ids.txt` + 同 (日期, 成员, 时间 ±10min) 双重幂等
- BILIBILI_COOKIE 失效（code=-101） → 自动飞书告警（6 小时节流）
- OSS 同步失败 → 仅告警不阻断（仓库数据完整，可手动重跑 `python scripts/sync_oss.py`）
- 归档超出客户端回看窗口（4 周） → OSS 上自动清理，GitHub 仓库完整保留

## 测试

```powershell
python scripts/test_smoke.py
```

覆盖（18 项）：规则时间提取、事件 ID 生成、突击直播自动发布与去重、过期清理、数据校验、发布字段兜底、版本号单调递增（周程+突击）、归档扫描、录播回填匹配与准确率、联合直播不误配、直播预约卡片解析、直播间开播检测与日程窗过滤、已播突击并入周程表、突击回扫候选、反代密钥头携带。全部离线，不请求网络、不调用 AI。

## 成本

| 项目 | 月费用 |
|:---|:---|
| GitHub Actions / Cloudflare Workers Cron | ¥0 |
| Qwen-VL-Max API（合计） | ≈ ¥3~7 |
| 阿里云 OSS（个人规模） | ≈ ¥0~1 |
| 飞书机器人 | ¥0 |
| **合计** | **< ¥10**（极端情况 < ¥35） |
