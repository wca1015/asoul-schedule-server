"""管道B：突击直播事件管理。

职责：
- 去重：按 source_dynamic_id 防止同一条动态重复入库
- 生命周期：超过 48 小时的事件自动清理
- 读写 flash.json
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from common import CST, FLASH_JSON, ensure_dirs
from publish import next_version

MAX_AGE_HOURS = 48  # 超过48小时自动清理


def _now() -> datetime:
    return datetime.now(CST)


def load_flash_data() -> dict:
    """读取 flash.json，不存在时返回空结构。"""
    if FLASH_JSON.exists():
        with open(FLASH_JSON, encoding="utf-8") as f:
            return json.load(f)
    return {"version": 0, "updated_at": _now().isoformat(), "events": []}


def save_flash_data(data: dict) -> None:
    ensure_dirs()
    with open(FLASH_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_flash_event(event: dict) -> bool:
    """添加事件，返回是否为新事件（重复时返回 False）。"""
    data = load_flash_data()
    existing_ids = {e.get("source_dynamic_id") for e in data["events"]}
    if event["source_dynamic_id"] in existing_ids:
        print(f"[flash-manager] 动态 {event['source_dynamic_id']} 已存在，跳过")
        return False

    data["events"].append(event)
    data["version"] = next_version(data.get("version"))
    data["updated_at"] = _now().isoformat()
    save_flash_data(data)
    print(f"[flash-manager] 新增事件: {event['id']}")
    return True


def cleanup_expired() -> int:
    """清理过期事件，返回清理数量。"""
    data = load_flash_data()
    cutoff = _now() - timedelta(hours=MAX_AGE_HOURS)

    def is_expired(event: dict) -> bool:
        try:
            return datetime.fromisoformat(event["start_time"]) < cutoff
        except (ValueError, KeyError, TypeError):
            return True  # 无法解析的视为过期

    original_count = len(data["events"])
    data["events"] = [e for e in data["events"] if not is_expired(e)]
    removed = original_count - len(data["events"])

    if removed > 0:
        data["version"] = next_version(data.get("version"))
        data["updated_at"] = _now().isoformat()
        save_flash_data(data)
        print(f"[flash-manager] 清理过期事件 {removed} 条")
    return removed
