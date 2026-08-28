# A-SOUL 周程表 App — 服务器端技术方案书

> **版本**：v2.0 | **日期**：2026-08-23 | **状态**：待评审
> **变更说明**：合并周程表抓取 + 突击直播监控两条管道为统一方案

---

## 一、项目概述

### 1.1 项目背景

A-SOUL 粉丝需要一个轻量级移动端应用，用于查看每周直播日程及临时加场信息。当前存在两个痛点：

- **周程表**：官方以图片形式发布在 B站官号动态中，粉丝需手动查看，体验不佳
- **突击直播**：成员个人号随时可能发布临时直播预告，时效性强、格式不固定，极易遗漏

### 1.2 项目目标

构建一套**低成本、半自动化**的服务器端数据管道，覆盖两条业务线：

| 业务线 | 数据源 | 时效要求 | 审核模式 |
|:---|:---|:---|:---|
| **周程表** | 官号（1个账号） | 天级 | 人工审核后发布 |
| **突击直播** | 成员个人号（5~6个账号） | **分钟级** | 人工审核 + 超时自动发布 |

### 1.3 核心原则

| 原则 | 说明 |
|:---|:---|
| 极简架构 | 不引入数据库、不写后端服务，最终产物是静态 JSON 文件 |
| 半自动化 | AI 提取 + 人工审核，**不做全自动发布**，确保数据准确性 |
| 双管道独立 | 周程表与突击直播主流程解耦，互不影响 |
| 零运维 | 优先使用 Serverless / 静态托管，无需管理服务器 |
| 低成本 | 月成本控制在 ¥35 以内 |

---

## 二、系统架构

### 2.1 整体架构图

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          数据生产层                                      │
│                                                                         │
│  ┌─── 管道A：周程表 ──────────────────────────────────────────────┐     │
│  │  B站官号动态 ──► 定时抓取(30min) ──► VLM识别 ──► draft.json   │     │
│  │                                                ──► 人工审核    │     │
│  │                                                ──► latest.json │     │
│  └────────────────────────────────────────────────────────────────┘     │
│                                                                         │
│  ┌─── 管道B：突击直播 ────────────────────────────────────────────┐     │
│  │  成员个人号×6 ──► 高频轮询(5min) ──► 关键词预筛               │     │
│  │                                     ──► 规则提取 / VLM识别    │     │
│  │                                     ──► flash_draft.json      │     │
│  │                                     ──► 人工审核(10min超时)    │     │
│  │                                     ──► flash.json            │     │
│  └────────────────────────────────────────────────────────────────┘     │
│                                                                         │
│  共享模块：飞书通知 / 数据校验 / 发布脚本                                │
└─────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          数据托管层                                      │
│                                                                         │
│   GitHub 仓库（唯一事实来源，可回滚）                                    │
│     /data/latest.json    ← 周程表（每周更新1次）                         │
│     /data/flash.json     ← 突击直播（实时更新，过期自动清理）             │
│         │                                                               │
│         │ Actions 每轮自动镜像（国内就近访问）                            │
│         ▼                                                               │
│   阿里云 OSS + CDN（客户端实际读取源）                                   │
│     /latest.json  Cache-Control: max-age=300                            │
│     /flash.json   Cache-Control: max-age=60                             │
└─────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          数据消费层                                      │
│                                                                         │
│         Flutter 客户端 (只读)                                           │
│         缓存优先 → 后台拉新 → 断网兜底                                   │
│         周程表：每小时轮询 / 下拉刷新                                    │
│         突击直播：每5分钟轮询 / App回前台时立即拉取                      │
└─────────────────────────────────────────────────────────────────────────┘
```

### 2.2 数据流对比

```
管道A（周程表）：
  官号图片 → 抓取 → VLM识别 → draft.json → 人工审核 → latest.json → CDN → App

管道B（突击直播）：
  成员动态 → 高频轮询 → 关键词预筛 → 规则/AI提取 → flash_draft.json
  → 人工审核(≤10min，超时自动发布) → flash.json → CDN → App
```

---

## 三、技术选型

| 模块 | 选型 | 备选 | 理由 |
|:---|:---|:---|:---|
| 周程表定时触发 | **GitHub Actions Cron**（每30分钟） | — | 免费，与代码仓库天然集成 |
| 突击直播定时触发 | **Cloudflare Workers Cron**（每5分钟） | 轻量服务器常驻脚本 | 秒级触发，无排队延迟，免费额度充足 |
| B站数据抓取 | **bilibili-api-python** | 直接请求API | 封装完善，处理了签名和风控 |
| 图片/内容识别 | **Qwen-VL-Max** | Gemini 2.5 Flash / GPT-4o | 中文理解极强，原生支持JSON输出，国内访问稳定 |
| 数据托管 | **阿里云 OSS + CDN**（Actions 自动镜像） | GitHub Pages / Cloudflare Pages | GitHub 仓库为事实来源，客户端从国内 CDN 就近读取 |
| 审核通知 | **飞书自定义机器人** | 钉钉 / 企业微信 | 免费、支持卡片消息、支持按钮回调 |
| 脚本语言 | **Python 3.11** | Node.js | AI SDK生态最成熟 |

---

## 四、数据模型设计

### 4.1 周程表 `latest.json`

```json
{
  "version": 2026082301,
  "week_start": "2026-08-17",
  "week_end": "2026-08-23",
  "updated_at": "2026-08-16T20:00:00+08:00",
  "source": "auto",
  "days": [
    {
      "date": "2026-08-17",
      "weekday": "星期一",
      "events": []
    },
    {
      "date": "2026-08-19",
      "weekday": "星期三",
      "events": [
        {
          "time": "19:00",
          "member": "jiaran",
          "title": "嘉然七夕直播",
          "desc": "我们时代的偏爱",
          "tag": "special",
          "group_type": "none",
          "format": "normal",
          "recording_bvid": null
        }
      ]
    }
  ]
}
```

#### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|:---|:---|:---|:---|
| `version` | int | ✅ | 每次发布递增，客户端判断是否有更新 |
| `week_start` / `week_end` | string | ✅ | 本周起止日期 |
| `updated_at` | string | ✅ | 数据发布时间 |
| `source` | string | ✅ | `"auto"` AI识别 / `"manual"` 手动修改 |
| `days[].date` | string | ✅ | YYYY-MM-DD |
| `days[].weekday` | string | ✅ | 星期几（中文） |
| `days[].events[]` | array | ✅ | 当天事件列表，无事件则为空数组 |
| `events[].time` | string | ✅ | "HH:MM" 24小时制 |
| `events[].member` | string | ✅ | `bella / jiaran / nailin / xinyi / sinuo / unknown` |
| `events[].title` | string | ✅ | 直播标题 |
| `events[].desc` | string | ❌ | 副标题/描述 |
| `events[].tag` | string | ❌ | `live / show / special / rest` |
| `events[].group_type` | string | ❌ | 团播分组：`none`（默认，单播）/ `asoul`（Asoul团播）/ `xinyi_sinuo`（心宜思诺团播）/ `zhijiang_variety`（枝江综艺）；客户端据此展示团播标签与专属头像，缺失时发布脚本兜底为 `none` |
| `events[].format` | string | ❌ | 直播形式：`normal`（默认）/ `theater`（小剧场）/ `night_talk`（夜谈）/ `game_room`（游戏室）/ `collab`（联动）/ `commercial`（工商直播）；客户端据此展示「节目/联动/工商」标签，缺失时发布脚本兜底为 `normal` |
| `events[].recording_bvid` | string | ❌ | 直播回放 BV 号；由录播管道在直播结束后回填（客户端据此展示可点击的「录像」标签），未上传时为 `null` |

> **兼容性原则**：`group_type` / `format` 为只增字段（Schema 只增不改名），旧客户端忽略未知字段不受影响；新客户端对缺省字段回退为默认值。

### 4.2 突击直播 `flash.json`

```json
{
  "version": 202608231430,
  "updated_at": "2026-08-23T14:30:00+08:00",
  "events": [
    {
      "id": "flash_20260823_jiaran_1900",
      "member": "jiaran",
      "title": "突击！唱歌练习室",
      "desc": "新曲首唱，来听听看~",
      "start_time": "2026-08-23T19:00:00+08:00",
      "end_time": null,
      "source_dynamic_id": "328174562817",
      "source_url": "https://t.bilibili.com/328174562817",
      "status": "upcoming",
      "auto_published": false,
      "recognized_at": "2026-08-23T14:32:00+08:00"
    }
  ]
}
```

#### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|:---|:---|:---|:---|
| `id` | string | ✅ | 唯一标识，格式 `flash_{日期}_{member}_{HHMM}`，用于去重 |
| `member` | string | ✅ | 成员枚举 |
| `title` | string | ✅ | 直播标题 |
| `desc` | string | ❌ | 补充描述 |
| `start_time` | string | ✅ | ISO8601 开播时间（含时区） |
| `end_time` | string | ❌ | 预计结束时间，未知则 `null` |
| `source_dynamic_id` | string | ✅ | B站动态ID，去重+溯源 |
| `source_url` | string | ✅ | 原始动态链接，客户端可跳转 |
| `status` | string | ✅ | `upcoming` / `live` / `ended` |
| `auto_published` | bool | ✅ | 是否超时自动发布（未审核） |
| `recognized_at` | string | ✅ | AI 识别时间 |

### 4.3 草稿文件

`draft.json`（周程表）和 `flash_draft.json`（突击直播）结构与正式文件一致，额外包含审核元数据：

```json
{
  "_meta": {
    "dynamic_id": "328174562817",
    "image_url": "https://i0.hdslb.com/xxx.jpg",
    "recognized_at": "2026-08-16T18:32:00+08:00",
    "confidence": 0.92,
    "status": "pending_review"
  }
}
```

---

## 五、管道A：周程表详细设计

### 5.1 B站动态抓取

```python
# scripts/fetch_dynamic.py
import requests, os

UID = os.environ["BILIBILI_UID"]  # 官号UID
STATE_FILE = "data/last_dynamic_id.txt"

def get_latest_draw_dynamic():
    """获取最新一条图文动态"""
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    params = {"host_mid": UID}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)...",
        "Referer": "https://space.bilibili.com/"
    }
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    for item in data["data"]["items"]:
        if item["type"] == "DYNAMIC_TYPE_DRAW":
            images = [
                img["src"]
                for img in item["modules"]["module_dynamic"]["major"]["draw"]["items"]
            ]
            return {
                "dynamic_id": item["id_str"],
                "images": images,
                "pub_ts": item["modules"]["module_author"]["pub_ts"],
                "text": item["modules"]["module_dynamic"]["desc"].get("text", "")
            }
    return None

def is_new_dynamic(dynamic):
    if not os.path.exists(STATE_FILE):
        return True
    with open(STATE_FILE) as f:
        return dynamic["dynamic_id"] != f.read().strip()

def save_dynamic_id(dynamic_id):
    with open(STATE_FILE, "w") as f:
        f.write(dynamic_id)
```

**防风控策略**：
- 请求间隔 ≥ 30 分钟
- 携带真实 User-Agent 和 Referer
- 单账号单IP，不做并发
- 遇 412/风控响应，静默跳过，下一轮重试

### 5.2 AI 图片识别

```python
# scripts/recognize.py
from dashscope import MultiModalConversation
import json

RECOGNITION_PROMPT = """你是一个A-SOUL周程表信息提取助手。

请仔细观察这张日程表图片，提取所有直播安排信息。

输出要求：
1. 严格输出合法JSON，不要包含任何markdown标记、注释或多余文字
2. 按以下结构输出：
{
  "week_start": "YYYY-MM-DD",
  "week_end": "YYYY-MM-DD",
  "days": [
    {
      "date": "YYYY-MM-DD",
      "weekday": "星期X",
      "events": [
        {
          "time": "HH:MM",
          "member": "成员英文名",
          "title": "直播标题",
          "desc": "副标题或描述，没有则留空字符串",
          "tag": "live|show|special|rest",
          "group_type": "none|asoul|xinyi_sinuo|zhijiang_variety",
          "format": "normal|theater|night_talk|game_room|collab|commercial"
        }
      ]
    }
  ]
}

3. member 只能是：bella, jiaran, nailin, xinyi, sinuo
   无法确定时填 "unknown"；团播/多人企划条目一律填 "unknown"
4. 某天没有安排，events 为空数组 []
5. "休息"字样对应 tag 填 "rest"
6. 时间统一为24小时制 "HH:MM"
7. 一周7天必须全部列出，不能遗漏
8. group_type：单人直播填 "none"；一期全员/"A-SOUL"团播填 "asoul"；
   心宜+思诺双人企划填 "xinyi_sinuo"；一期+二期共同/"枝江综艺"填 "zhijiang_variety"
9. format：普通直播填 "normal"；小剧场/夜谈/游戏室/联动/工商分别填对应枚举
"""

def recognize_schedule(image_url: str) -> dict:
    response = MultiModalConversation.call(
        model="qwen-vl-max",
        messages=[{
            "role": "user",
            "content": [
                {"image": image_url},
                {"text": RECOGNITION_PROMPT}
            ]
        }],
        result_format="message"
    )
    raw_text = response.output.choices[0].message.content[0]["text"]
    raw_text = raw_text.strip().removeprefix("```json").removesuffix("```").strip()
    return json.loads(raw_text)
```

### 5.3 数据校验

```python
# scripts/validate.py
VALID_MEMBERS = {"bella", "jiaran", "nailin", "xinyi", "sinuo", "unknown"}
VALID_TAGS = {"live", "show", "special", "rest"}

def validate_schedule(data: dict) -> list[str]:
    errors = []
    if "week_start" not in data or "week_end" not in data:
        errors.append("缺少 week_start 或 week_end")

    days = data.get("days", [])
    if len(days) != 7:
        errors.append(f"期望7天，实际{len(days)}天")

    for day in days:
        for event in day.get("events", []):
            if event.get("member") not in VALID_MEMBERS:
                errors.append(f"未知成员: {event['member']}")
            if event.get("tag") not in VALID_TAGS:
                errors.append(f"未知标签: {event['tag']}")
    return errors
```

### 5.4 审核与发布

```python
# scripts/publish.py
import json, shutil
from datetime import datetime

def next_version(current: int | None, now: datetime | None = None) -> int:
    """版本号递增规则：max(当前小时戳, 当前版本+1)，保证严格单调递增。

    所有正式数据文件（latest.json / flash.json）的版本号统一走该函数：
    同一时间窗口内多次写入时，仅用时间戳会出现版本号持平/倒退，
    客户端按版本号比较判断更新会漏拉数据。
    """
    stamp = int((now or datetime.now()).strftime("%Y%m%d%H"))
    return max(stamp, int(current or 0) + 1)

def publish_schedule(draft_path="data/draft.json", latest_path="data/latest.json"):
    with open(draft_path) as f:
        data = json.load(f)

    # 若已有正式文件，版本号须在其基础上严格递增（同小时重复发布场景）
    previous_version = int(json.load(open(latest_path)).get("version") or 0) \
        if os.path.exists(latest_path) else 0

    data.pop("_meta", None)
    data["version"] = next_version(previous_version)
    data["updated_at"] = datetime.now().isoformat()
    data["source"] = "auto"

    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # 归档
    shutil.copy(latest_path, f"archive/{data['week_start']}.json")
```

---

## 六、管道B：突击直播详细设计

### 6.1 监控范围

同时监控 **6 个账号**的动态：

| 账号 | 说明 |
|:---|:---|
| A-SOUL Official（官号） | 官号转发/官宣突击 |
| 向晚Ava | 成员个人动态 |
| 贝拉Bella | 成员个人动态 |
| 珈乐Carol | 成员个人动态 |
| 嘉然Diana | 成员个人动态 |
| 乃琳Eileen | 成员个人动态 |

> 如有新成员或成员换号，修改 `config/members.yaml` 即可。

```yaml
# config/members.yaml
members:
  - name: "A-SOUL Official"
    uid: "xxx"
    type: "official"
  - name: "向晚Ava"
    uid: "xxx"
    member_key: "xinyi"
  - name: "贝拉Bella"
    uid: "xxx"
    member_key: "bella"
  - name: "珈乐Carol"
    uid: "xxx"
    member_key: "nailin"
  - name: "嘉然Diana"
    uid: "xxx"
    member_key: "jiaran"
  - name: "乃琳Eileen"
    uid: "xxx"
    member_key: "sinuo"

keywords:
  include: ["突击", "加场", "临时", "今晚", "今天", "马上", "等下", "直播预约", "开播", "速来", "惊喜", "特别放送"]
  exclude: ["周报", "日程", "请假", "休息", "停播", "公告", "生日会预约"]
```

### 6.2 三阶段识别引擎

突击直播内容形式多样，采用 **关键词预筛 → 规则提取 → AI 兜底** 三级流水线：

```
新动态进入
    │
    ▼
┌─────────────────────────┐
│ Stage 1: 关键词预筛      │  ← 成本≈0，过滤90%+无关动态
│ 命中include且不命中exclude│
└─────────┬───────────────┘
          │ 命中
          ▼
┌─────────────────────────┐
│ Stage 2: 规则提取        │  ← 纯文字动态，正则直接提取
│ 解析时间+成员+标题       │     成本低、速度快（<100ms）
└─────────┬───────────────┘
          │ 规则无法提取
          ▼
┌─────────────────────────┐
│ Stage 3: AI 识别         │  ← 仅对图文混合/复杂内容调用
│ Qwen-VL 提取三要素       │     成本高、准确率高（<3s）
└─────────┬───────────────┘
          │
          ▼
    校验 → 去重 → 审核通知
```

#### Stage 1：关键词预筛

```python
def keyword_filter(text: str, config: dict) -> bool:
    """返回True表示可能是突击直播动态"""
    text_lower = text.lower()
    has_include = any(kw in text_lower for kw in config["keywords"]["include"])
    has_exclude = any(kw in text_lower for kw in config["keywords"]["exclude"])
    return has_include and not has_exclude
```

#### Stage 2：规则提取

```python
import re
from datetime import datetime

def extract_by_rules(text: str, member_key: str) -> dict | None:
    """尝试用正则从纯文字动态中提取突击直播信息"""
    time_patterns = [
        r'今[晚天]\s*(\d{1,2})[:：](\d{2})',
        r'(\d{1,2})[:：](\d{2})\s*(?:开播|直播|开始)',
        r'(\d{1,2})点\s*(?:半)?',
    ]

    matched_time = None
    for pattern in time_patterns:
        m = re.search(pattern, text)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2)) if m.lastindex >= 2 else 0
            today = datetime.now().strftime("%Y-%m-%d")
            matched_time = f"{today}T{hour:02d}:{minute:02d}:00+08:00"
            break

    if not matched_time:
        return None  # 规则无法提取，交给 AI

    title_match = re.search(r'(?:突击[！!]?\s*)?(.+?)(?:\n|$)', text)
    title = title_match.group(1).strip() if title_match else "突击直播"

    return {
        "member": member_key,
        "title": title,
        "start_time": matched_time,
        "desc": text[:200],
        "extract_method": "rule"
    }
```

#### Stage 3：AI 识别（专用 Prompt）

```python
FLASH_RECOGNITION_PROMPT = """你是一个A-SOUL直播信息提取助手。

这是一条成员发布的动态，可能包含突击直播/临时加场的信息。

请判断这条动态是否包含直播预约信息。如果是，提取以下信息并输出JSON：
{
  "is_flash_live": true,
  "member": "成员英文名(bella/jiaran/nailin/xinyi/sinuo)",
  "title": "直播标题",
  "desc": "补充描述",
  "start_time": "YYYY-MM-DDTHH:MM:SS+08:00",
  "end_time": null
}

如果不是直播预约（如日常分享、请假、周报等），输出：
{
  "is_flash_live": false
}

注意：
1. "明天""下周"等非今天的时间，仍然提取，start_time 换算为具体日期
2. 只说了"今晚"没给具体时间，start_time 设为当天 19:00:00
3. 严格输出合法JSON，不要包含其他文字
"""
```

### 6.3 去重与生命周期管理

```python
# scripts/flash_manager.py
import json
from datetime import datetime, timedelta
from publish import next_version  # 版本号统一走 next_version，保证严格单调

FLASH_FILE = "data/flash.json"
MAX_AGE_HOURS = 48  # 超过48小时自动清理

def add_flash_event(event: dict) -> bool:
    """添加事件，返回是否为新事件"""
    data = load_flash_data()
    existing_ids = {e["source_dynamic_id"] for e in data["events"]}
    if event["source_dynamic_id"] in existing_ids:
        return False

    data["events"].append(event)
    # 同一分钟内可能多次写入（新增事件/清理过期/发布草稿），
    # 分钟戳会持平，客户端按版本号比较会漏更新，统一走 next_version
    data["version"] = next_version(data.get("version"))
    data["updated_at"] = datetime.now().isoformat()
    save_flash_data(data)
    return True

def cleanup_expired():
    """清理过期事件"""
    data = load_flash_data()
    cutoff = datetime.now() - timedelta(hours=MAX_AGE_HOURS)
    original_count = len(data["events"])
    data["events"] = [
        e for e in data["events"]
        if datetime.fromisoformat(e["start_time"]) > cutoff
    ]
    if len(data["events"]) != original_count:
        data["version"] = next_version(data.get("version"))
        data["updated_at"] = datetime.now().isoformat()
        save_flash_data(data)
```

### 6.4 审核流程（含超时自动发布）

突击直播审核与周程表有重要区别：

| 环节 | 周程表 | 突击直播 |
|:---|:---|:---|
| 审核时限 | 宽松（几小时内） | **紧急（≤10分钟）** |
| 通知方式 | 飞书卡片 | **飞书卡片 + @管理员** |
| 超时机制 | 无 | **10分钟未审核 → 自动发布 + 标记"未经审核"** |
| 驳回后果 | 等下周 | 本次直播信息丢失（不可接受） |

> ⚠️ **核心逻辑**：突击直播宁可发布可能有误的数据，也不能不发。客户端对 `auto_published: true` 的事件标注"⚠️ 待确认"。

```python
# scripts/auto_publish_timeout.py
from datetime import datetime, timedelta

REVIEW_TIMEOUT_SECONDS = 600  # 10分钟

def check_and_auto_publish(draft: dict):
    recognized_at = datetime.fromisoformat(draft["_meta"]["recognized_at"])
    elapsed = (datetime.now() - recognized_at).total_seconds()

    if elapsed > REVIEW_TIMEOUT_SECONDS and draft["_meta"]["status"] == "pending_review":
        draft["auto_published"] = True
        draft["review_note"] = "管理员未在10分钟内审核，系统自动发布"
        publish_flash(draft)
        send_feishu_alert(
            "⚠️ 突击直播已自动发布（超时未审核）",
            f"成员：{draft['member']}\n标题：{draft['title']}\n请事后核实准确性"
        )
```

### 6.5 飞书通知

```python
# scripts/notify.py
import requests

FEISHU_WEBHOOK = os.environ["FEISHU_WEBHOOK"]

def send_schedule_review_card(draft: dict, image_url: str):
    """周程表审核通知"""
    card = build_card(
        title="📋 新周程表待审核",
        content=(
            f"**周期**：{draft['week_start']} ~ {draft['week_end']}\n"
            f"**事件数**：{sum(len(d['events']) for d in draft['days'])}\n"
            f"**识别时间**：{draft['_meta']['recognized_at']}"
        ),
        image_url=image_url,
        actions=["✅ 确认发布", "❌ 驳回"]
    )
    requests.post(FEISHU_WEBHOOK, json=card)

def send_flash_review_card(draft: dict):
    """突击直播紧急审核通知"""
    card = build_card(
        title="🔴 突击直播待审核（10分钟超时）",
        content=(
            f"**成员**：{draft['member']}\n"
            f"**标题**：{draft['title']}\n"
            f"**开播时间**：{draft['start_time']}\n"
            f"**来源**：{draft['source_url']}"
        ),
        actions=["✅ 确认发布", "❌ 驳回"],
        at_all=True  # @所有人
    )
    requests.post(FEISHU_WEBHOOK, json=card)

def send_alert(title: str, detail: str):
    """异常告警"""
    card = build_card(title=f"🚨 {title}", content=detail)
    requests.post(FEISHU_WEBHOOK, json=card)
```

---

## 七、审核流程总览

### 7.1 周程表审核

```
AI识别完成
    │
    ├── 校验失败 ──► 飞书告警："识别异常，请人工处理"（附原图+错误信息）
    │
    └── 校验通过 ──► 生成 draft.json
                        │
                        ▼
                  飞书卡片通知管理员
                  ┌─────────────────────────────┐
                  │ 📋 新周程表已识别             │
                  │ 周期：08/17 - 08/23         │
                  │ 事件数：12                  │
                  │                             │
                  │ [查看草稿]  [原图]           │
                  │ [✅ 确认发布]  [❌ 驳回]     │
                  └─────────────────────────────┘
                        │
              ┌─────────┴─────────┐
              ▼                   ▼
         确认发布              驳回
              │                   │
              ▼                   ▼
    draft → latest.json     管理员手动修改
    git commit + push       后重新提交
```

### 7.2 突击直播审核

```
识别完成
    │
    ├── 校验失败 ──► 飞书告警 + 跳过本次
    │
    └── 校验通过 ──► 生成 flash_draft.json
                        │
                        ▼
                  飞书紧急通知（@管理员）
                  ┌─────────────────────────────┐
                  │ 🔴 突击直播待审核            │
                  │ 成员：嘉然                   │
                  │ 标题：突击！唱歌练习室        │
                  │ 时间：今晚 19:00             │
                  │ 倒计时：10:00               │
                  │                             │
                  │ [✅ 确认]  [❌ 驳回]         │
                  └─────────────────────────────┘
                        │
              ┌─────────┼─────────┐
              ▼         │         ▼
         确认发布    超时10min   驳回
              │         │         │
              ▼         ▼         ▼
         flash.json  自动发布   删除草稿
         正常发布    + ⚠️标记   飞书通知
```

---

## 八、项目文件结构

```
asoul-schedule-server/
├── .github/
│   └── workflows/
│       └── cron_schedule.yml         # 周程表定时任务（每30分钟）
├── cloudflare/
│   └── workers/
│       └── flash_cron.js             # 突击直播定时触发（每5分钟）
├── config/
│   └── members.yaml                  # 成员UID + 关键词配置
├── scripts/
│   ├── main.py                       # 主流程编排（区分两种模式）
│   ├── fetch_dynamic.py              # 管道A：B站官号动态抓取
│   ├── recognize.py                  # 管道A：周程表VLM识别
│   ├── flash_monitor.py              # 管道B：多账号突击直播监控
│   ├── flash_recognize.py            # 管道B：三级识别引擎
│   ├── flash_manager.py              # 管道B：去重/过期清理
│   ├── auto_publish_timeout.py       # 管道B：超时自动发布
│   ├── validate.py                   # 共享：数据校验
│   ├── notify.py                     # 共享：飞书通知
│   ├── publish.py                    # 共享：发布脚本
│   └── sync_oss.py                   # 共享：正式数据镜像到国内 OSS
├── data/
│   ├── latest.json                   # 线上周程表
│   ├── flash.json                    # 线上突击直播
│   ├── draft.json                    # 周程表草稿
│   ├── flash_draft.json              # 突击直播草稿
│   ├── last_dynamic_id.txt           # 周程表：上次处理的动态ID
│   └── last_flash_{uid}.txt          # 突击：各成员上次处理的动态ID
├── archive/                          # 历史周程归档
│   ├── 2026-08-10.json
│   └── 2026-08-17.json
├── requirements.txt
└── README.md
```

---

## 九、部署配置

### 9.1 GitHub Actions（周程表）

```yaml
# .github/workflows/cron_schedule.yml
name: 定时抓取B站周程表

on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:

jobs:
  fetch-and-recognize:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: 安装依赖
        run: pip install -r requirements.txt
            - name: 执行抓取与识别
        env:
          BILIBILI_UID: ${{ secrets.BILIBILI_UID }}
          DASHSCOPE_API_KEY: ${{ secrets.DASHSCOPE_API_KEY }}
          FEISHU_WEBHOOK: ${{ secrets.FEISHU_WEBHOOK }}
        run: python scripts/main.py --mode schedule
      - name: 提交变更
        run: |
          git config user.name "schedule-bot"
          git config user.email "bot@example.com"
          git add data/ archive/
          git diff --cached --quiet || git commit -m "auto: 更新周程数据"
          git push
      - name: 同步到国内 OSS
        env:
          OSS_ACCESS_KEY_ID: ${{ secrets.OSS_ACCESS_KEY_ID }}
          OSS_ACCESS_KEY_SECRET: ${{ secrets.OSS_ACCESS_KEY_SECRET }}
          OSS_ENDPOINT: ${{ secrets.OSS_ENDPOINT }}
          OSS_BUCKET: ${{ secrets.OSS_BUCKET }}
        run: python scripts/sync_oss.py
```

### 9.3 阿里云 OSS 镜像（数据分发层）

客户端不从 GitHub 拉数据（慢），而是从国内 OSS 就近读取：

- **事实来源**：GitHub 仓库 `data/` 目录（可回滚、可审计）
- **分发副本**：两条管道的 Actions 每轮提交后自动运行 `scripts/sync_oss.py`，把 `latest.json` / `flash.json` 上传到 OSS（对象级 `public-read`，bucket 本身保持私有）
- **缓存策略**：`flash.json` 设 `Cache-Control: max-age=60`，`latest.json` 设 `max-age=300`
- **未配置 OSS 密钥时**：脚本静默跳过，不影响管道主流程（可灰度迁移）
- 后续可再套一层 CDN 加速，并绑定自定义域名

### 9.2 Cloudflare Workers Cron（突击直播）

```javascript
// cloudflare/workers/flash_cron.js
export default {
  async scheduled(event, env, ctx) {
    // 每5分钟触发，调用部署在 Workers 上的 Python 脚本
    // 或通过 HTTP 触发 GitHub Actions workflow_dispatch
    const resp = await fetch(env.TRIGGER_URL, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ mode: "flash" })
    });
    console.log("Flash monitor triggered:", resp.status);
  }
};
```

> **备选方案**：如不想引入 Cloudflare，可用一台 ¥30/月的轻量服务器跑 `while True: monitor(); sleep(300)` 常驻脚本。

---

## 十、客户端对接要点

### 10.1 双数据源轮询策略

| 数据源 | 轮询频率 | 触发时机 |
|:---|:---|:---|
| `latest.json`（周程表） | 每小时 / 下拉刷新 | App启动、下拉刷新 |
| `flash.json`（突击直播） | **每5分钟** | App启动、回前台、定时轮询 |

### 10.2 客户端缓存策略

```
缓存优先 → 后台拉新 → 断网兜底
```

- 通过 `version` 字段判断是否需要更新本地缓存
- 网络失败时展示上次缓存 + "数据更新于 xx" 提示

### 10.3 突击直播 UI 展示建议

- 首页顶部增加 **"🔴 突击直播"** 横幅卡片，仅在 `flash.json` 有 `upcoming` 事件时显示
- 点击跳转到原始B站动态（`source_url`），方便粉丝一键预约
- `auto_published: true` 的事件加 **⚠️ 待确认** 标识
- 直播开始后（`start_time` 已过），卡片变为 **"🟢 正在直播"** 样式
- 直播结束后自动隐藏

---

## 十一、成本估算

| 项目 | 月费用 | 备注 |
|:---|:---|:---|
| GitHub Actions | ¥0 | 免费 2000 分钟/月 |
| Cloudflare Workers Cron | ¥0 | 免费 10万次调用/月 |
| Qwen-VL-Max API（周程表） | ≈ ¥1~2 | 每周 1~2 张图 |
| Qwen-VL-Max API（突击直播） | ≈ ¥2~5 | 每月 5~15 次（关键词预筛过滤90%+） |
| 阿里云 OSS 托管 | ≈ ¥0~1 | 存储+请求流量，个人规模基本免费 |
| 飞书机器人 | ¥0 | 免费 |
| **合计** | **< ¥10/月** | 极端情况 < ¥35 |

---

## 十二、实施路线图

| 阶段 | 时间 | 任务 | 产出 |
|:---|:---|:---|:---|
| **P1** 基础管道 | Day 1~2 | B站动态抓取 + 防风控 | 能稳定拿到最新图片 |
| **P2** AI 识别 | Day 3~4 | 周程表 Prompt 调优 + 校验逻辑 | 历史5周图片测试，准确率 ≥ 95% |
| **P3** 审核流 | Day 5 | 飞书通知 + draft/latest 分离 | 管理员收到卡片可确认/驳回 |
| **P4** 发布流 | Day 6 | 发布脚本 + 归档 + OSS 镜像部署 | latest.json 上线可访问 |
| **P5** 联调 | Day 7 | 端到端跑通周程表管道 | 完整闭环验证 |
| **P6** 客户端对接 | Day 8 | Flutter 替换 Mock 为 HTTP + 缓存 | App 展示真实周程数据 |
| **P7** 突击抓取 | Day 9~10 | 多账号监控 + 关键词预筛 + 状态记录 | 6个账号动态稳定获取 |
| **P8** 双引擎识别 | Day 11~12 | 规则提取 + AI Prompt 调优 + 去重/过期 | 突击直播识别准确率 ≥ 90% |
| **P9** 审核与超时 | Day 13 | 飞书紧急通知 + 10分钟超时自动发布 | 审核流完整可用 |
| **P10** 客户端适配 | Day 14 | flash.json 轮询 + 突击直播UI卡片 | App 展示突击直播信息 |

**总计：约 14 天完成全部开发。**

---

## 十三、风险与应对

| 风险 | 影响 | 应对措施 |
|:---|:---|:---|
| B站接口变更/风控 | 无法抓取动态 | 脚本加异常重试；保留手动上传入口兜底 |
| AI 识别错误 | 日程信息不准 | **人工审核兜底**；校验不通过自动告警 |
| 突击直播识别太慢 | 粉丝已从其他渠道得知 | 超时自动发布；客户端展示"待确认"标识 |
| 官号/成员未按时发布 | 本周无新数据 | 客户端展示上周数据 + 更新时间提示 |
| 图片/文本格式大改 | AI 识别失败 | 飞书告警 → 管理员手动编辑 JSON 上传 |
| 非直播动态被误识别 | 发布错误信息 | 关键词预筛 + AI `is_flash_live` 双重过滤 + 人工审核 |
| 多个成员同时发突击 | 并发处理压力 | 串行处理（单次<3秒），无需并发 |
| 成员换号/改名 | 监控失效 | UID 配置化，改 `members.yaml` 即可 |
| OSS 同步失败 | 客户端读到旧数据 | 同步失败仅告警不阻断；仓库数据完整，可随时手动重跑 `python scripts/sync_oss.py` |
| JSON 结构需扩展 | 旧客户端不兼容 | Schema 只增字段不改名；客户端做容错解析 |
| 直播取消/改期 | 信息过时 | 管理员手动删除/修改；客户端轮询自动同步 |

---

## 十四、后续扩展方向（本期不做）

- 🔔 **开播提醒推送**：接入 FCM / APNs，用户订阅特定成员
- 📜 **历史周程回看**：利用 `archive/` 目录，客户端查询历史数据
- 🤖 **全自动发布**：当 AI 识别准确率持续 > 99% 后，逐步放开
- 📊 **识别准确率看板**：记录每次识别结果与人工修正的 diff，持续优化 Prompt
- 💬 **社区互动**：评论、弹幕、分享等（需引入后端服务）

---

## 附录：管理员操作手册

### 周程表（正常流程，每周一次，约 1 分钟）

1. 收到飞书卡片通知
2. 点击"查看草稿"核对内容
3. 点击"✅ 确认发布"
4. 完成

### 突击直播（正常流程，约 2 分钟）

1. 收到飞书紧急通知（@提醒）
2. 查看成员、标题、时间是否正确
3. 点击"✅ 确认发布"
4. 完成

### 突击直播（超时自动发布）

1. 系统已在10分钟后自动发布
2. 管理员事后核实准确性
3. 如有错误，手动修改 `flash.json` 并 git push

### 异常处理

1. 收到"识别异常"告警
2. 打开对应草稿文件手动修改
3. 运行 `python scripts/publish.py --manual`
4. 完成

---

> **本方案书完（v2.0）。**
> 两条管道共享基础设施（飞书通知、数据校验、发布脚本、静态托管），但主流程完全独立、互不影响。建议按路线图顺序实施，先跑通周程表管道（P1~P6），再叠加突击直播管道（P7~P10）。