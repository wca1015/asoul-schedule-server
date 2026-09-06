"""把「已播过的真突击直播」并入当前周周程表（像正常直播一样出现在时间线）。

背景：突击直播只在 flash.json 记录——结束后 App UI 隐藏、48 小时自动清理，
用户无法在周程表时间线看到「已播过的突击直播」，也拿不到录播回填
(recording_bvid) 的「录像」标签。本模块把 flash.json 中**已开播的真突击**
并入当前周 latest.json（并同步归档），使其与正常直播一致：
- 出现在时间线对应日期/时间（UI 无需改动，读 latest.json 即显示）
- 之后由录播回填管道（管道C）按正常逻辑补 recording_bvid → 「录像」标签

「真突击」的判定（真突击都会发直播预约动态）：
- 只并入**动态通道**识别出的事件（source_dynamic_id = 真实动态 ID，
  说明存在预约/预告动态）
- **直播间状态兜底事件**（source_dynamic_id 以 ``live_`` 开头）**永不并入**：
  这类事件只是「房间裸检测到开播」，可能是日程内提前开播的误报——
  2026-09-06 心宜 19:50 提前为 20:00「审美积累中」开播即被误判为突击
- 仅并入已开播（start_time <= 当前时间）的事件，未来场次不提前写进时间线

幂等 / 防重复：
- 仅并入「当前周 latest.json 覆盖日期」内的（更早的突击已过期清理）
- 同 (日期, 成员, 时间 ±10 分钟) 已有日程事件则跳过
- data/merged_flash_ids.txt 记录已并入的 source_dynamic_id，防止重复并入
- 并入后 bump version + updated_at，并复制到 archive/{week_start}.json
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from common import ARCHIVE_DIR, CST, LATEST_JSON
from publish import SCHEDULE_COMMENT, next_version

MERGE_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "merged_flash_ids.txt"

# 与已有日程判重的时间容差（分钟）
DUP_WINDOW_MINUTES = 10


def _load_merged_ids() -> set[str]:
    if not MERGE_STATE_FILE.exists():
        return set()
    return {
        line.strip()
        for line in MERGE_STATE_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def _save_merged_ids(ids: set[str]) -> None:
    MERGE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MERGE_STATE_FILE.write_text(
        "\n".join(sorted(ids)) + ("\n" if ids else ""), encoding="utf-8"
    )


def _to_minutes(time_s: str) -> int | None:
    try:
        h, m = time_s.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _exists_near(day: dict, member: str, time_s: str, window: int = DUP_WINDOW_MINUTES) -> bool:
    """该日该成员在时间窗内是否已有日程事件（防与正常直播/已并入的重复）。"""
    t = _to_minutes(time_s)
    if t is None:
        return False
    for e in day.get("events", []):
        if e.get("member") != member:
            continue
        e_t = _to_minutes(str(e.get("time") or ""))
        if e_t is not None and abs(e_t - t) <= window:
            return True
    return False


def collect_inserts(
    latest: dict,
    flash_events: list[dict],
    merged: set[str],
    now: datetime | None = None,
) -> list[tuple[str, dict, str]]:
    """纯逻辑：算出需并入的 (日期, 事件, source_dynamic_id) 列表（便于离线测试）。

    只并入「真突击」= 动态通道识别出的事件（source_dynamic_id 为真实动态 ID，
    有预约/预告动态）；直播间状态兜底事件（``live_`` 前缀）永不并入。
    仅并入已开播（start_time <= now）的，避免未来场次提前写进时间线。
    """
    now = now or datetime.now(CST)
    days_by_date = {d["date"]: d for d in latest.get("days", []) if d.get("date")}
    inserts: list[tuple[str, dict, str]] = []
    for ev in flash_events:
        sid = str(ev.get("source_dynamic_id") or "")
        # 直播间状态兜底事件（live_{room}_{live_time}）可能把日程内提前开播
        # 误判为突击（2026-09-06 心宜 19:50 误报），永不并入周程表
        if sid.startswith("live_") or sid in merged:
            continue
        try:
            start_dt = datetime.fromisoformat(str(ev.get("start_time") or ""))
        except (TypeError, ValueError):
            continue
        if start_dt > now:
            continue  # 尚未开播的预约不并入
        date_s = start_dt.strftime("%Y-%m-%d")
        day = days_by_date.get(date_s)
        if day is None:
            continue  # 不在当前周
        time_s = start_dt.strftime("%H:%M")
        member = ev.get("member")
        if _exists_near(day, member, time_s):
            continue  # 已有日程（正常直播/已并入），不重复
        inserts.append(
            (
                date_s,
                {
                    "time": time_s,
                    "member": member,
                    "title": str(ev.get("title") or "突击直播"),
                    "desc": "",
                    "tag": "live",
                    "group_type": "none",
                    "format": "normal",
                },
                sid,
            )
        )
    return inserts


def merge_aired_flash_into_schedule(
    flash_events: list[dict] | None = None
) -> int:
    """把已开播的真突击并入当前周 latest.json；返回并入数量。

    flash_events 缺省时读 flash.json（突击管道调用）；也可传入外部候选
    （如 backfill_flash 回填扫描追回漏抓的直播预约动态）。
    """
    if not LATEST_JSON.exists():
        return 0
    try:
        latest = json.loads(LATEST_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0

    now = datetime.now(CST)
    merged = _load_merged_ids()
    if flash_events is None:
        from flash_manager import load_flash_data

        flash_events = load_flash_data().get("events", [])
    inserts = collect_inserts(latest, flash_events, merged, now=now)
    if not inserts:
        return 0

    days_by_date = {d["date"]: d for d in latest.get("days", []) if d.get("date")}
    for date_s, event, sid in inserts:
        days_by_date[date_s]["events"].append(event)
        merged.add(sid)
    # 各日按时间排序
    for day in days_by_date.values():
        day["events"].sort(key=lambda e: str(e.get("time") or ""))

    latest["version"] = next_version(int(latest.get("version") or 0), now)
    latest["updated_at"] = now.isoformat()
    data = {"_comment": latest.get("_comment") or SCHEDULE_COMMENT, **latest}
    with open(LATEST_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    week_start = latest.get("week_start")
    if week_start:
        shutil.copy(LATEST_JSON, ARCHIVE_DIR / f"{week_start}.json")

    # 记录已并入的 source_dynamic_id（从 inserts 对应 flash 事件取）
    _save_merged_ids(merged)
    print(f"[schedule-flash] 已将 {len(inserts)} 场已开播真突击并入周程表，"
          f"版本 {data['version']}")
    return len(inserts)
