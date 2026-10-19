"""管道D（兜底）：枝江站（asoul.love）日历 → 本周周程表。

为什么需要：
- 管道A 依赖官号「周程表」图文动态 + Qwen-VL 识别，识别失败/官号未发周程表时，
  ``data/latest.json`` 会一直停留在上一周，本周日程只能靠 App 端临时并入
  asoul.love 的 ICS，服务端（OSS）本身没有本周数据；
- 本站点日历页已包含本周全部日程与 **B 站录播（直播回放）BV 号**，可直接转成
  正式数据发布，任何客户端都能拿到。

触发条件（见 ``publish_current_week_from_asoul_love``）：
- 本周（周一~周日，CST）在 ``latest.json`` 中没有任何日程；
- 且 ``latest.json`` 没有跑到更后面的周（说明管道A 正常，不做回退）；
- 已有本周数据且 ``source=auto`` 时绝不覆盖（周程表优先）；
- 已有本周数据且 ``source=asoul_love`` 时按 ``REFRESH_INTERVAL`` 增量刷新。

「从枝江站获取的日程会被清除」的三道保护：
1. 只有本周完全没数据才兜底，管道A 只要发布过本周数据就不再介入；
2. 刷新时与旧数据合并：站点删掉的条目、已回填的 ``recording_bvid`` 都保留；
3. ``publish.publish_schedule`` 拒绝用空日程覆盖同周已有数据，并在周切换时
   把上一周补归档，避免数据凭空消失。
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from common import CST, DATA_DIR, LATEST_JSON, ensure_dirs
from validate import validate_schedule

# 站点日历页（Astro SSR，页面内嵌 astro-island 的 initialEvents）
CALENDAR_URL = "https://asoul.love/calendar/{week}"
CALENDAR_LATEST_URL = "https://asoul.love/calendar/latest"
SOURCE_NAME = "asoul_love"
REFRESH_INTERVAL = timedelta(hours=3)
REQUEST_TIMEOUT = 20
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 兜底草稿（与管道A 的 draft.json 分开，避免并发/残留互相覆盖）
FALLBACK_DRAFT_JSON = DATA_DIR / "asoul_love_draft.json"
# 失败告警节流（与 bili_session.alert_once 同思路，但不给它引入 requests 依赖）
ALERT_MARKER = DATA_DIR / "alert_asoul_love_last.txt"
ALERT_INTERVAL_SECONDS = 6 * 3600

# 站点成员键 → 服务端 member_key
SITE_MEMBER_MAP = {
    "bella": "bella",   # 贝拉
    "diana": "jiaran",  # 嘉然
    "eileen": "nailin",  # 乃琳
    "fiona": "xinyi",   # 心宜
    "gladys": "sinuo",  # 思诺
}
# 一期双人同播：服务端用组合键表示（与 README「双人组合」规则一致）
_TWO_PERSON_MEMBERS = {"bella_jiaran", "bella_nailin", "jiaran_nailin"}
_FIRST_GEN = {"bella", "jiaran", "nailin"}

_WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
_BV_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")

# astro-island 的 props 属性（HTML 转义后的 devalue 序列化 JSON）
_PROPS_RE = re.compile(r'props="([^"]*)"')
_ASTRO_VALUE = 0
_ASTRO_ARRAY = 1


# ---------------------------------------------------------------------------
# 解析：HTML → initialEvents
# ---------------------------------------------------------------------------

def _decode_astro(node):
    """还原 Astro 的 devalue 序列化：``[0, x]`` 为普通值，``[1, [...]]`` 为数组。"""
    if isinstance(node, dict):
        return {key: _decode_astro(value) for key, value in node.items()}
    if isinstance(node, list):
        if len(node) == 2 and isinstance(node[0], int):
            tag, value = node
            if tag == _ASTRO_VALUE:
                return _decode_astro(value) if isinstance(value, (dict, list)) else value
            if tag == _ASTRO_ARRAY and isinstance(value, list):
                return [_decode_astro(item) for item in value]
            # Date/Map 等其它序列化类型当前页面未使用，递归兜底避免整页解析失败
            return _decode_astro(value) if isinstance(value, (dict, list)) else value
        return [_decode_astro(item) for item in node]
    return node


def parse_calendar_payload(page_html: str) -> dict:
    """从日历页 HTML 中取出 astro-island 的 props 并解码。"""
    for raw in _PROPS_RE.findall(page_html or ""):
        if "initialEvents" not in raw:
            continue
        try:
            decoded = json.loads(html_lib.unescape(raw))
        except ValueError:
            continue
        data = _decode_astro(decoded)
        if isinstance(data, dict) and isinstance(data.get("initialEvents"), list):
            return data
    raise ValueError("日历页中未找到 initialEvents 数据（站点结构可能已变化）")


def _parse_start(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST)


def _normalize_event(raw: dict) -> dict | None:
    """单条站点事件 → 内部结构；时间非法直接丢弃（宁可少一条也不发脏数据）。"""
    try:
        start = _parse_start(raw.get("startTime") or "")
    except (TypeError, ValueError):
        return None

    recordings = []
    for item in raw.get("recordings") or []:
        if not isinstance(item, dict):
            continue
        bvid = str(item.get("id") or "").strip()
        if _BV_RE.match(bvid):
            recordings.append({"id": bvid, "title": str(item.get("title") or "")})

    return {
        "id": str(raw.get("id") or "").strip(),
        "title": str(raw.get("title") or "").strip(),
        "description": str(raw.get("description") or "").strip(),
        "type": str(raw.get("type") or "").strip(),
        "format": str(raw.get("format") or "").strip(),
        "start": start,
        "host": str(raw.get("host") or "").strip(),
        "performers": [str(p).strip() for p in (raw.get("performers") or []) if str(p).strip()],
        "status": str(raw.get("status") or "").strip(),
        "recordings": recordings,
    }


def parse_events(page_html: str) -> list[dict]:
    """页面 HTML → 去重后的事件列表（已取消的事件不发布）。"""
    payload = parse_calendar_payload(page_html)
    events: list[dict] = []
    seen: set[str] = set()
    for raw in payload["initialEvents"]:
        if not isinstance(raw, dict):
            continue
        event = _normalize_event(raw)
        if event is None or not event["id"] or event["status"] == "cancelled":
            continue
        if event["id"] in seen:
            continue
        seen.add(event["id"])
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# 映射：站点事件 → 服务端日程契约
# ---------------------------------------------------------------------------

def week_start_of(day: date) -> date:
    """本周一（CST 自然周，与服务端其它管道一致）。"""
    return day - timedelta(days=day.weekday())


def events_for_week(events: list[dict], week_start: date) -> list[dict]:
    """页面固定返回 4 周窗口，按 CST 日期过滤出目标周。"""
    week_end = week_start + timedelta(days=6)
    selected = [e for e in events if week_start <= e["start"].date() <= week_end]
    return sorted(selected, key=lambda e: (e["start"], e["id"]))


def _member_keys(event: dict) -> list[str]:
    site_keys = event["performers"] or ([event["host"]] if event["host"] else [])
    keys: list[str] = []
    for site_key in site_keys:
        mapped = SITE_MEMBER_MAP.get(site_key)
        if mapped and mapped not in keys:
            keys.append(mapped)
    return sorted(keys)


def resolve_member(keys: list[str]) -> tuple[str, str]:
    """成员组合 → (member, group_type)。

    - 单人 → (成员键, none)
    - 一期双人同播 → (组合键, none)
    - 心宜+思诺 → (unknown, xinyi_sinuo)
    - 一期三人团播 → (unknown, asoul)
    - 其它多人 → (unknown, zhijiang_variety)（客户端会同时展示全部参与成员）
    """
    if not keys:
        return "unknown", "none"
    if len(keys) == 1:
        return keys[0], "none"
    if len(keys) == 2:
        pair = "_".join(keys)
        if pair in _TWO_PERSON_MEMBERS:
            return pair, "none"
        if set(keys) == {"xinyi", "sinuo"}:
            return "unknown", "xinyi_sinuo"
        return "unknown", "zhijiang_variety"
    if set(keys) <= _FIRST_GEN:
        return "unknown", "asoul"
    return "unknown", "zhijiang_variety"


def _tag_for(event: dict) -> str:
    """站点类型 → 服务端 tag（live/show/special/rest）。"""
    if event["format"] == "offline" or event["type"] == "活动":
        return "special"
    if event["type"] == "节目":
        return "show"
    return "live"


def _title_for(event: dict) -> str:
    """标题保留站点类型前缀：【突击】… 能让 App 正确显示「突击」标签。"""
    base = (event["description"] or event["title"] or "直播").strip()
    etype = event["type"]
    if etype and etype not in base:
        base = f"【{etype}】{base}"
    return base


def build_schedule(week_events: list[dict], week_start: date) -> dict:
    """事件列表 → latest.json 的 7 天结构（含 recording_bvid）。"""
    days = []
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        entries = []
        for event in week_events:
            if event["start"].date() != day:
                continue
            member, group_type = resolve_member(_member_keys(event))
            title = _title_for(event)
            entry = {
                "time": event["start"].strftime("%H:%M"),
                "member": member,
                "title": title,
                "desc": event["title"] if event["title"] != title else "",
                "tag": _tag_for(event),
                "group_type": group_type,
                "format": "normal",  # 直播形式（剧场/游戏室…）站点未提供，用缺省值
            }
            if event["recordings"]:
                entry["recording_bvid"] = event["recordings"][0]["id"]
            entries.append(entry)
        entries.sort(key=lambda item: (item["time"], item["title"]))
        days.append({
            "date": day.isoformat(),
            "weekday": _WEEKDAY_CN[day.weekday()],
            "events": entries,
        })
    return {
        "week_start": week_start.isoformat(),
        "week_end": (week_start + timedelta(days=6)).isoformat(),
        "days": days,
    }


def _entry_key(day_date: str, event: dict) -> tuple:
    return (day_date, event.get("time") or "", event.get("member") or "")


def merge_schedules(new: dict, old: dict | None) -> dict:
    """以站点数据为主合并旧数据，避免刷新时「枝江站日程被清除」：

    - 站点新数据优先（时间/标题/成员以站点为准）；
    - 旧数据里已回填的 recording_bvid 若站点暂时没有，保留；
    - 站点已删掉的旧条目保留（宁可多一条，也不静默消失）。
    """
    if not old or old.get("week_start") != new.get("week_start"):
        return new

    old_events: dict[tuple, dict] = {}
    for day in old.get("days") or []:
        for event in day.get("events") or []:
            old_events.setdefault(_entry_key(day.get("date") or "", event), event)

    merged = json.loads(json.dumps(new, ensure_ascii=False))
    seen: set[tuple] = set()
    for day in merged["days"]:
        for event in day["events"]:
            key = _entry_key(day["date"], event)
            seen.add(key)
            previous = old_events.get(key)
            if previous and not event.get("recording_bvid") and previous.get("recording_bvid"):
                event["recording_bvid"] = previous["recording_bvid"]

    for key, previous in old_events.items():
        if key in seen:
            continue
        for day in merged["days"]:
            if day["date"] == key[0]:
                day["events"].append(json.loads(json.dumps(previous, ensure_ascii=False)))
                break

    for day in merged["days"]:
        day["events"].sort(key=lambda item: (item.get("time") or "", item.get("title") or ""))
    return merged


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------

def fetch_calendar_html(week_start: date, timeout: int = REQUEST_TIMEOUT) -> str:
    """抓取目标周的日历页（参数化周路径，避免站点「最新」锚点漂移）。"""
    import requests  # 延迟导入：离线冒烟测试/本地无依赖时仍可导入本模块

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    resp = requests.get(CALENDAR_URL.format(week=week_start.isoformat()), headers=headers, timeout=timeout)
    if resp.status_code == 404:
        # 目标周页面尚未生成时退回站点「最新」页（仍按 CST 日期过滤）
        resp = requests.get(CALENDAR_LATEST_URL, headers=headers, timeout=timeout)
    if resp.status_code == 429:
        raise RuntimeError(f"枝江站限流（HTTP 429）：{resp.url}")
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


# ---------------------------------------------------------------------------
# 发布判定
# ---------------------------------------------------------------------------

def _event_count(data: dict | None) -> int:
    if not isinstance(data, dict):
        return 0
    return sum(len(day.get("events") or []) for day in data.get("days") or [])


def _read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _refresh_due(latest: dict, now: datetime) -> bool:
    stamp = latest.get("updated_at")
    if not stamp:
        return True
    try:
        updated = datetime.fromisoformat(str(stamp))
    except ValueError:
        return True
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=CST)
    return now - updated >= REFRESH_INTERVAL


def _same_days(left: dict, right: dict) -> bool:
    return json.dumps(left.get("days"), ensure_ascii=False, sort_keys=True) == json.dumps(
        right.get("days"), ensure_ascii=False, sort_keys=True
    )


def _alert_once(detail: str) -> None:
    """抓取/解析失败告警（6 小时节流），避免 Actions 每轮刷屏。"""
    try:
        now_ts = time.time()
        if ALERT_MARKER.exists():
            last = float(ALERT_MARKER.read_text(encoding="utf-8").strip() or 0)
            if now_ts - last < ALERT_INTERVAL_SECONDS:
                return
        ensure_dirs()
        ALERT_MARKER.write_text(str(int(now_ts)), encoding="utf-8")
    except (OSError, ValueError):
        pass
    try:
        from notify import send_alert

        send_alert("枝江站兜底抓取失败", detail)
    except Exception:  # 通知失败不影响主流程
        pass


def publish_current_week_from_asoul_love(
    now: datetime | None = None,
    dry_run: bool = False,
) -> bool:
    """本周没有周程表时，从枝江站日历补一份（含直播回放 BV 号）。

    返回是否真正发布了新数据。
    """
    now = now or datetime.now(CST)
    week = week_start_of(now.date()).isoformat()
    latest = _read_json(LATEST_JSON)
    latest_week = latest.get("week_start")
    latest_events = _event_count(latest)
    same_week = latest_week == week and latest_events > 0

    if same_week and latest.get("source") != SOURCE_NAME:
        print(f"[asoul_love] 本周已有周程表（source={latest.get('source')}），无需兜底")
        return False
    if latest_week and latest_week > week:
        # 管道A 已发布更后面的周（周日发下周周程表）：此时回退会覆盖新周数据
        print(f"[asoul_love] latest.json 已是更后面的周（{latest_week} > {week}），跳过兜底")
        return False
    if same_week and not _refresh_due(latest, now):
        print("[asoul_love] 本周兜底数据仍在刷新间隔内，跳过")
        return False

    try:
        page = fetch_calendar_html(week_start_of(now.date()))
        events = parse_events(page)
    except Exception as exc:  # 站点不可用/改版：只告警，不影响其它管道
        print(f"[asoul_love] 枝江站日历获取/解析失败: {exc}")
        _alert_once(f"目标周: {week}\n错误: {exc}\n本周周程表仍缺失，下一轮自动重试")
        return False

    week_events = events_for_week(events, week_start_of(now.date()))
    if not week_events:
        print(f"[asoul_love] 枝江站 {week} 暂无日程，保持现状（不发布空表）")
        return False

    data = build_schedule(week_events, week_start_of(now.date()))
    if same_week:
        data = merge_schedules(data, latest)

    errors = validate_schedule(data, now)
    if errors:
        detail = "\n".join(f"- {error}" for error in errors)
        print(f"[asoul_love] 兜底数据校验失败:\n{detail}")
        _alert_once(f"目标周: {week}\n校验错误:\n{detail}")
        return False

    if same_week and _same_days(latest, data):
        print("[asoul_love] 枝江站日程无变化，跳过发布")
        return False

    if dry_run:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        print(f"[asoul_love] dry-run：本周可兜底 {len(week_events)} 条，未写入")
        return False

    ensure_dirs()
    with open(FALLBACK_DRAFT_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        from publish import publish_schedule

        publish_schedule(draft_path=FALLBACK_DRAFT_JSON, source=SOURCE_NAME)
    finally:
        FALLBACK_DRAFT_JSON.unlink(missing_ok=True)

    try:
        from notify import send_schedule_published_card

        send_schedule_published_card(data, "")
    except Exception as exc:  # 通知失败不影响已发布的数据
        print(f"[asoul_love] 飞书通知失败（数据已发布）: {exc}")

    recordings = sum(1 for day in data["days"] for event in day["events"] if event.get("recording_bvid"))
    print(
        f"[asoul_love] 已从枝江站发布本周兜底周程表: {len(week_events)} 条"
        f"（含录播 {recordings} 条），week={week}"
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="枝江站（asoul.love）周程表兜底")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不写文件")
    args = parser.parse_args()
    published = publish_current_week_from_asoul_love(dry_run=args.dry_run)
    print("[asoul_love] 已发布" if published else "[asoul_love] 未发布")


if __name__ == "__main__":
    main()
