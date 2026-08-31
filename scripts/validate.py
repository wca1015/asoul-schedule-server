"""数据校验模块（共享）。

对 AI 识别结果做合法性校验，不合法的数据绝不进入发布流程，
改为飞书告警 + 人工介入。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from common import CST

VALID_MEMBERS = {"bella", "jiaran", "nailin", "xinyi", "sinuo", "unknown"}
VALID_TAGS = {"live", "show", "special", "rest"}
# 团播分组 / 直播形式枚举（客户端据此展示标签；缺省时发布脚本兜底为 none/normal）
VALID_GROUP_TYPES = {"none", "asoul", "xinyi_sinuo", "zhijiang_variety"}
VALID_FORMATS = {"normal", "theater", "night_talk", "game_room", "collab", "commercial"}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_schedule(data: dict, now: datetime | None = None) -> list[str]:
    """校验周程表结构，返回错误列表（空列表表示通过）。"""
    errors: list[str] = []

    if not data.get("week_start") or not data.get("week_end"):
        errors.append("缺少 week_start 或 week_end")
    elif not (DATE_RE.match(data["week_start"]) and DATE_RE.match(data["week_end"])):
        errors.append("week_start / week_end 格式必须为 YYYY-MM-DD")
    else:
        # 合理性校验：海报通常不印年份，VLM 易把年份认错（如 2026 认成 2023）。
        # 格式合法但年份错误的数据一旦发布，客户端会把日程落到完全错误的周
        # （甚至超出可回看范围，表现为「显示不了数据」），此处限制合理窗口：
        # week_start ∈ [当前 - 8 周, 当前 + 2 周]。
        try:
            week_start = datetime.strptime(data["week_start"], "%Y-%m-%d").date()
            today = (now or datetime.now(CST)).date()
            if not (today - timedelta(weeks=8)) <= week_start <= (today + timedelta(weeks=2)):
                errors.append(
                    f"week_start 与当前时间偏差过大（疑似年份识别错误）: {data['week_start']}"
                )
        except ValueError:
            pass

    days = data.get("days", [])
    if len(days) != 7:
        errors.append(f"期望7天，实际{len(days)}天")

    seen_dates: set[str] = set()
    for day in days:
        date = day.get("date", "")
        if not DATE_RE.match(date):
            errors.append(f"非法日期: {date!r}")
        elif date in seen_dates:
            errors.append(f"日期重复: {date}")
        seen_dates.add(date)

        if not day.get("weekday"):
            errors.append(f"{date}: 缺少 weekday")

        events = day.get("events")
        if not isinstance(events, list):
            errors.append(f"{date}: events 必须为数组")
            continue

        for i, event in enumerate(events):
            prefix = f"{date} 第{i + 1}条"
            if not TIME_RE.match(event.get("time", "")):
                errors.append(f"{prefix}: 时间格式非法 {event.get('time')!r}，应为 HH:MM")
            if event.get("member") not in VALID_MEMBERS:
                errors.append(f"{prefix}: 未知成员 {event.get('member')!r}")
            if event.get("tag") and event["tag"] not in VALID_TAGS:
                errors.append(f"{prefix}: 未知标签 {event['tag']!r}")
            if event.get("group_type") and event["group_type"] not in VALID_GROUP_TYPES:
                errors.append(f"{prefix}: 未知团播分组 {event['group_type']!r}")
            if event.get("format") and event["format"] not in VALID_FORMATS:
                errors.append(f"{prefix}: 未知直播形式 {event['format']!r}")
            if not event.get("title"):
                errors.append(f"{prefix}: 缺少 title")
    return errors


def validate_flash_event(event: dict) -> list[str]:
    """校验单条突击直播事件，返回错误列表。"""
    errors: list[str] = []

    if event.get("member") not in VALID_MEMBERS:
        errors.append(f"未知成员 {event.get('member')!r}")
    if not event.get("title"):
        errors.append("缺少 title")

    start_time = event.get("start_time")
    if not start_time:
        errors.append("缺少 start_time")
    else:
        try:
            parsed = datetime.fromisoformat(start_time)
            if parsed.tzinfo is None:
                errors.append("start_time 缺少时区信息")
        except ValueError:
            errors.append(f"start_time 格式非法: {start_time!r}")

    if not event.get("source_dynamic_id"):
        errors.append("缺少 source_dynamic_id")
    return errors
