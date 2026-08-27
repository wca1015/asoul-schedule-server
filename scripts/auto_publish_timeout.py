import json
from datetime import datetime

from common import CST, FLASH_DRAFT_JSON
from notify import send_alert
from publish import publish_flash

REVIEW_TIMEOUT_SECONDS = 600  # 10分钟


def _load_pending_draft() -> dict | None:
    """加载待审核草稿；文件不存在 / 非法 / 非 pending 状态时返回 None。"""
    if not FLASH_DRAFT_JSON.exists():
        return None
    try:
        with open(FLASH_DRAFT_JSON, encoding="utf-8") as f:
            draft = json.load(f)
    except (ValueError, OSError):
        return None
    if not isinstance(draft, dict):
        return None
    if draft.get("_meta", {}).get("status") != "pending_review":
        return None
    return draft


def _extract_events(draft: dict) -> list[dict]:
    """把草稿规范化为事件列表（兼容包装式与扁平单事件两种结构）。"""
    events = draft.get("events")
    if isinstance(events, list):
        return [e for e in events if isinstance(e, dict)]
    return [{k: v for k, v in draft.items() if k != "_meta"}]


def check_and_auto_publish() -> bool:
    """检查草稿是否审核超时，超时则自动发布。返回是否执行了发布。"""
    draft = _load_pending_draft()
    if draft is None:
        return False

    try:
        recognized_at = datetime.fromisoformat(draft["_meta"]["recognized_at"])
    except (KeyError, ValueError, TypeError):
        return False

    if recognized_at.tzinfo is None:
        recognized_at = recognized_at.replace(tzinfo=CST)
    elapsed = (datetime.now(CST) - recognized_at).total_seconds()

    if elapsed <= REVIEW_TIMEOUT_SECONDS:
        print(f"[timeout] 草稿仍在审核期内（已等待 {int(elapsed)}s），暂不处理")
        return False

    # 超时 → 逐条标记后发布（标记必须打在事件上，客户端按事件读取）
    events = _extract_events(draft)
    if not events:
        print("[timeout] 草稿为空，直接清理")
        FLASH_DRAFT_JSON.unlink(missing_ok=True)
        return False

    for event in events:
        event["auto_published"] = True
        event["review_note"] = "管理员未在10分钟内审核，系统自动发布"
    draft["events"] = events

    publish_flash(draft)

    summary = "\n".join(
        f"- {e.get('member')}: {e.get('title')} @ {e.get('start_time')}"
        for e in events
    )
    send_alert(
        "突击直播已自动发布（超时未审核）",
        f"共 {len(events)} 条事件：\n{summary}\n请事后核实准确性",
    )
    print(f"[timeout] 草稿审核超时，已自动发布 {len(events)} 条事件")
    return True
