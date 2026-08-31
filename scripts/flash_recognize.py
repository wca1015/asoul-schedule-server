"""管道B：三级识别引擎。

Stage 1: 关键词预筛  —— 成本≈0，过滤绝大多数无关动态
Stage 2: 规则提取    —— 纯文字动态用正则提取时间+标题，快且便宜
Stage 3: AI 识别     —— 图文混合/复杂内容交给 Qwen-VL 兜底
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from common import CST

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
4. 今天是 {today}
"""


# ---------------------------------------------------------------------------
# Stage 1：关键词预筛
# ---------------------------------------------------------------------------

def keyword_filter(text: str, config: dict) -> bool:
    """返回 True 表示可能是突击直播动态。"""
    if not text:
        return False
    text_lower = text.lower()
    keywords = config.get("keywords", {})
    has_include = any(kw in text_lower for kw in keywords.get("include", []))
    has_exclude = any(kw in text_lower for kw in keywords.get("exclude", []))
    return has_include and not has_exclude


# ---------------------------------------------------------------------------
# Stage 2：规则提取
# ---------------------------------------------------------------------------

# 规则提取仅处理"当天"时间（日期会硬编码为今天）：
# 必须带 今晚/今天/晚上 等当天前缀，"明天/下周"等一律交给 AI。
_TIME_PATTERNS = [
    # 冒号式：今晚8:30 / 今晚 8：30 / 今天19:00
    re.compile(r"(?:今[晚天]|晚上)\s*(\d{1,2})\s*[:：]\s*(\d{2})"),
    # 点式：今晚 8 点 / 今天19点30分 / 晚上18点 / 今晚8点半
    re.compile(r"(?:今[晚天]|晚上)\s*(\d{1,2})\s*点\s*(?:(半)|(\d{1,2})\s*分?)?"),
]

# 直播预约卡片（MAJOR_TYPE_LIVE）注入的确定性时间标记，
# 格式由 flash_monitor 拼出："直播预约时间: 2026-08-30 20:00"
_LIVE_PLAN_RE = re.compile(
    r"直播预约时间[:：]\s*(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})"
)


def _first_content_line(text: str) -> str:
    """取正文第一行作为标题候选（跳过注入的"直播预约时间"标记行）。"""
    for line in text.strip().splitlines():
        if not line.startswith("直播预约时间"):
            return line.strip()
    return ""


def extract_by_rules(text: str, member_key: str) -> dict | None:
    """尝试用正则从纯文字动态中提取突击直播信息。

    无法提取出开播时间时返回 None，交给 AI 处理。
    """
    now = datetime.now(CST)

    # 直播预约卡片的确定性时间优先（不依赖"今晚/今天"前缀）
    m = _LIVE_PLAN_RE.search(text)
    if m:
        try:
            dt = datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)), tzinfo=CST,
            )
        except ValueError:
            dt = None
        if dt is not None:
            title = re.sub(
                r"^突击[！!]?\s*", "", _first_content_line(text)
            ).strip() or "直播预约"
            return {
                "member": member_key,
                "title": title[:50],
                "start_time": dt.isoformat(),
                "desc": text[:200],
                "extract_method": "rule",
            }

    matched_time = None

    for pattern in _TIME_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        hour = int(m.group(1))
        minute = 0
        # group(1) 之后的分组：可能是分钟数字或"半"
        for g in m.groups()[1:]:
            if not g:
                continue
            if g == "半":
                minute = 30
            elif g.isdigit():
                minute = int(g)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            continue
        matched_time = now.strftime("%Y-%m-%d") + f"T{hour:02d}:{minute:02d}:00+08:00"
        break

    if not matched_time:
        return None

    # 标题：取第一行去掉突击前缀
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    title = re.sub(r"^突击[！!]?\s*", "", first_line).strip() or "突击直播"

    return {
        "member": member_key,
        "title": title,
        "start_time": matched_time,
        "desc": text[:200],
        "extract_method": "rule",
    }


# ---------------------------------------------------------------------------
# Stage 3：AI 识别
# ---------------------------------------------------------------------------

def recognize_by_ai(text: str, images: list[str], member_key: str) -> dict | None:
    """调用 Qwen-VL 判断并提取突击直播信息。

    返回含 member/title/start_time 的 dict；非直播动态返回 None。
    """
    from dashscope import MultiModalConversation

    from common import get_env

    get_env("DASHSCOPE_API_KEY")

    today = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    prompt = FLASH_RECOGNITION_PROMPT.replace("{today}", today)

    content: list[dict] = [{"image": url} for url in images]
    content.append({"text": f"动态正文：{text}\n该动态发布人成员key：{member_key}\n\n{prompt}"})

    response = MultiModalConversation.call(
        model="qwen-vl-max",
        messages=[{"role": "user", "content": content}],
        result_format="message",
    )

    if response.status_code != 200:
        raise RuntimeError(f"VLM 调用失败: {response.code} {response.message}")

    raw = response.output.choices[0].message.content[0]["text"].strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.removeprefix("json").strip()
    result = json.loads(raw)

    if not result.get("is_flash_live"):
        return None

    result.setdefault("member", member_key)
    result["extract_method"] = "ai"
    return result


# ---------------------------------------------------------------------------
# 三级流水线入口
# ---------------------------------------------------------------------------

def recognize_flash(dynamic: dict, account: dict, config: dict) -> dict | None:
    """对单条动态执行三级识别流水线。

    返回事件 dict（待校验/去重）或 None（判定无关）。
    """
    text = dynamic.get("text", "")
    images = dynamic.get("images", [])
    member_key = account.get("member_key", "unknown")
    account_type = account.get("type")

    # Stage 1: 关键词预筛
    if not keyword_filter(text, config):
        print(f"[flash] {dynamic['dynamic_id']} 未命中关键词，跳过")
        return None

    print(f"[flash] {dynamic['dynamic_id']} 命中关键词，进入提取阶段")

    # Stage 2: 规则提取（仅对有明确 member_key 的个人号）
    event = None
    if not images or account_type != "official":
        event = extract_by_rules(text, member_key)

    # Stage 3: AI 兜底
    if event is None:
        event = recognize_by_ai(text, images, member_key)

    if event is None:
        print(f"[flash] {dynamic['dynamic_id']} AI 判定为非直播动态")
        return None

    # 补全公共字段
    now = datetime.now(CST)
    start_time = event["start_time"]
    try:
        start_dt = datetime.fromisoformat(start_time)
        event_id = f"flash_{start_dt.strftime('%Y%m%d')}_{event.get('member', member_key)}_{start_dt.strftime('%H%M')}"
    except ValueError:
        # AI 返回的时间不规范时退化为动态ID去重，保证流程不中断
        event_id = f"flash_{dynamic['dynamic_id']}"
    event.update(
        {
            "id": event_id,
            "member": event.get("member", member_key),
            "title": event.get("title") or "突击直播",
            "desc": event.get("desc", ""),
            "start_time": start_time,
            "end_time": event.get("end_time"),
            "source_dynamic_id": dynamic["dynamic_id"],
            "source_url": f"https://t.bilibili.com/{dynamic['dynamic_id']}",
            "status": "upcoming",
            "auto_published": False,
            "recognized_at": now.isoformat(),
        }
    )
    return event
