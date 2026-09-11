"""直播间状态轮询 —— 突击直播兜底通道。

背景：B 站「成员空间动态」接口对数据中心 IP 风控严格（412 时通时断，
GitHub Actions 出口 IP 池轮换导致不稳定），「无预告直接开播」的突击直播
仅靠动态抓取不可靠。而直播间接口 getRoomPlayInfo **匿名稳定**
（Actions 直连实测 HTTP 200），据此轮询成员直播间状态：

- live_status == 1（直播中）且 live_time 与上次记录不同 → 判定「新开播」
- 产出与动态识别**同构**的 flash 事件：
    source_dynamic_id = "live_{room_id}_{live_time}"（每场直播唯一）
  由既有 publish_flash 按 source_dynamic_id 去重发布，跨轮次幂等
- 本地状态文件 data/live_state_{room_id}.txt 记录最近一场的 live_time，
  避免同一场直播每轮重复上报（重复上报也无害——发布端会去重）

事件字段与 validate_flash_event / flash.json 契约完全兼容：
member / title / start_time / source_dynamic_id 均满足校验。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from bili_session import build_session, get_json
from common import CST, DATA_DIR, LATEST_JSON

ROOM_PLAY_API = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"

# getRoomPlayInfo 参数（protocol/format/codec/qn 为播放信息所需，qn=0 最省）
ROOM_PLAY_PARAMS = {
    "protocol": "0,1",
    "format": "0,1,2",
    "codec": "0,1",
    "qn": 0,
}

# live_status 语义：0=未开播，1=直播中，2=轮播
LIVE_STATUS_LIVE = 1

# 日程交叉比对时间窗（分钟）：
# - 成员单播：开播通常比日程早 ~10 分钟，也可能迟到 → 较宽窗口
# - 其他场次（团播/节目/他人单播）：只认紧贴场次前后的开播（大概率就是该场）
SCHEDULE_SOLO_BEFORE_MIN = 30
SCHEDULE_SOLO_AFTER_MIN = 150
SCHEDULE_ANY_BEFORE_MIN = 5
SCHEDULE_ANY_AFTER_MIN = 20


def _state_file(room_id: str | int) -> Path:
    return DATA_DIR / f"live_state_{room_id}.txt"


def _last_live_time(room_id: str | int) -> int:
    f = _state_file(room_id)
    if not f.exists():
        return 0
    try:
        return int(f.read_text().strip() or "0")
    except ValueError:
        return 0


def _save_live_time(room_id: str | int, live_time: int) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _state_file(room_id).write_text(str(live_time))


def is_new_live_session(status: dict, last_live_time: int) -> bool:
    """是否为新开播：直播中（status==1）且 live_time > 上次记录。

    纯函数，便于离线测试。轮播（status==2）/未开播（status==0）不算直播中。
    """
    live_time = int(status.get("live_time", 0) or 0)
    is_live = int(status.get("live_status", 0) or 0) == LIVE_STATUS_LIVE and live_time > 0
    return is_live and live_time != last_live_time


def schedule_rows_from(data: dict) -> list[tuple[str, int]]:
    """从周程表数据（latest.json 结构）解析 [(member, 开播 epoch 秒), ...]（纯函数）。

    供 [is_scheduled_stream] 与突击并入（schedule_flash）共用同一套判定数据。
    """
    rows: list[tuple[str, int]] = []
    for day in data.get("days", []):
        date_s = day.get("date", "")
        for ev in day.get("events", []):
            time_s = ev.get("time") or ""
            try:
                dt = datetime.strptime(
                    f"{date_s} {time_s}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=CST)
            except ValueError:
                continue
            rows.append((ev.get("member") or "unknown", int(dt.timestamp())))
    return rows


def _schedule_rows() -> list[tuple[str, int]]:
    """读取最新周程表 latest.json，返回 [(member, 开播 epoch 秒), ...]。"""
    try:
        if not LATEST_JSON.exists():
            return []
        data = json.loads(LATEST_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return schedule_rows_from(data)


def is_scheduled_stream(
    member_key: str, live_time: int, rows: list[tuple[str, int]] | None = None
) -> bool:
    """该场直播是否属于周程表内的常规直播（而非突击直播）。

    匹配规则（rows 缺省时读 latest.json）：
    - 成员单播（ev_member == member_key）：时间差 ∈ [-30, +150] 分钟
    - 其他场次（团播/节目，member 可能是 unknown）：只认紧贴场次的前后
      ∈ [-5, +20] 分钟（成员常在日程前 ~10 分钟先开个人房）
    """
    if rows is None:
        rows = _schedule_rows()
    for ev_member, ev_epoch in rows:
        diff_min = (ev_epoch - live_time) / 60.0
        if ev_member == member_key:
            if -SCHEDULE_SOLO_BEFORE_MIN <= diff_min <= SCHEDULE_SOLO_AFTER_MIN:
                return True
        elif -SCHEDULE_ANY_BEFORE_MIN <= diff_min <= SCHEDULE_ANY_AFTER_MIN:
            return True
    return False


def fetch_room_status(session, room_id: str | int) -> dict | None:
    """拉取直播间状态；失败返回 None。

    返回 {live_status, live_time, title}；live_time 为开播的 unix 秒（未开播为 0）。
    """
    data = get_json(
        session,
        ROOM_PLAY_API,
        params={"room_id": room_id, **ROOM_PLAY_PARAMS},
        referer=f"https://live.bilibili.com/{room_id}",
    )
    if data is None or data.get("code") != 0:
        return None
    body = data.get("data") or {}
    return {
        "live_status": int(body.get("live_status", 0) or 0),
        "live_time": int(body.get("live_time", 0) or 0),
        "title": (body.get("title") or "").strip(),
    }


def build_live_event(member: dict, room_id: str | int, status: dict) -> dict:
    """构造与动态识别同构的 flash 事件（该成员当前正在直播）。

    source_dynamic_id 用「live_{room}_{live_time}」——每场直播的 live_time 唯一，
    既是 flash.json 的去重键，也天然幂等。
    """
    live_time = status["live_time"]
    start_dt = datetime.fromtimestamp(live_time, tz=CST)
    member_key = member["member_key"]
    return {
        "id": f"live_{room_id}_{live_time}",
        "member": member_key,
        "title": status["title"] or "突击直播",
        "desc": "直播间状态轮询检测到开播（兜底通道，可能无预告动态）",
        "start_time": start_dt.isoformat(),
        "end_time": None,
        "source_dynamic_id": f"live_{room_id}_{live_time}",
        "source_url": f"https://live.bilibili.com/{room_id}",
        "status": "live",
        "auto_published": False,
        "recognized_at": datetime.now(CST).isoformat(),
    }


def check_live(members: list[dict]) -> tuple[list[dict], set[str]]:
    """轮询各成员直播间。

    members：members.yaml 中带 member_key + room_id 的成员（官号无 member_key，跳过）。

    :return: (新开播事件列表, 当前仍在直播的 source_dynamic_id 集合)
    - 新开播：live_status==1 且 live_time 变化，且**不在周程表时间窗内**
      （日程内直播不报为突击，避免与周程表重复/误导）
    - live_now_ids 供调用方把已结束的 live 事件标记为 ended
    """
    session = build_session()
    events: list[dict] = []
    live_now: set[str] = set()
    for member in members:
        room_id = str(member.get("room_id") or "")
        member_key = member.get("member_key")
        if not room_id or not member_key:
            continue

        status = fetch_room_status(session, room_id)
        if status is None:
            print(f"[live] {member_key} 房间 {room_id} 状态查询失败，跳过")
            continue

        live_time = status["live_time"]
        is_live = status["live_status"] == LIVE_STATUS_LIVE and live_time > 0
        if is_live:
            live_now.add(f"live_{room_id}_{live_time}")

        last = _last_live_time(room_id)

        if is_new_live_session(status, last):
            if is_scheduled_stream(member_key, live_time):
                # 日程内直播（如开播比日程早 ~10 分钟），不是突击直播
                print(
                    f"[live] {member_key} 开播 {live_time} 命中周程表时间窗，"
                    f"判定为日程内直播，不上报为突击"
                )
                _save_live_time(room_id, live_time)
                continue
            event = build_live_event(member, room_id, status)
            events.append(event)
            print(
                f"[live] {member_key} 检测到突击开播: "
                f"{event['title']!r} live_time={live_time} 房间={room_id}"
            )
            # 记录本场 live_time：同一场直播后续轮次不再触发
            _save_live_time(room_id, live_time)
        elif is_live:
            print(f"[live] {member_key} 直播中（已上报过 live_time={live_time}），跳过")
        else:
            print(
                f"[live] {member_key} 未开播（status={status['live_status']}），跳过"
            )

    return events, live_now


def sync_live_endings(live_now_ids: set[str]) -> int:
    """把 flash.json 中「已结束」的直播事件标记为 ended。

    live_now_ids 为当前仍在直播的 source_dynamic_id 集合；flash.json 里
    status=live 但不在该集合的事件说明直播已结束 → 标 ended + end_time，
    否则 App 会一直显示「直播中」。返回标记数量。
    """
    from flash_manager import load_flash_data, save_flash_data
    from publish import next_version

    data = load_flash_data()
    now = datetime.now(CST)
    ended = 0
    for event in data["events"]:
        sid = str(event.get("source_dynamic_id") or "")
        if event.get("status") == "live" and sid.startswith("live_") and sid not in live_now_ids:
            event["status"] = "ended"
            event["end_time"] = now.isoformat()
            ended += 1

    if ended:
        data["version"] = next_version(data.get("version"))
        data["updated_at"] = now.isoformat()
        save_flash_data(data)
        print(f"[live] 已将 {ended} 场已结束直播标记为 ended")
    return ended

