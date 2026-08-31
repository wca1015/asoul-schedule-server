"""发布脚本（共享）。

将识别结果提升为正式文件：
- 周程表：draft.json  ->  latest.json （并归档到 archive/）
- 突击直播：事件列表直接合并进 flash.json（按 source_dynamic_id 去重）

支持 `--manual` 参数用于异常处理场景：管理员手动编辑草稿后重新发布。
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

from common import (
    ARCHIVE_DIR,
    CST,
    DRAFT_JSON,
    FLASH_DRAFT_JSON,
    FLASH_JSON,
    LATEST_JSON,
    ensure_dirs,
)


def _now() -> datetime:
    return datetime.now(CST)


# 正式数据文件顶部注释：JSON 不支持原生注释，统一以 `_comment` 字段承载，
# 便于管理员在 OSS 上直接发现问题并手动修正（客户端仅按 version 判断缓存，忽略该字段）。
SCHEDULE_COMMENT = (
    "本周直播日程表（周程表），由系统自动生成。发现问题可手动修正："
    "① 推荐：修改 GitHub 仓库 data/latest.json 后 push，Actions 会自动同步覆盖本对象；"
    "② 快速修复：直接编辑本 OSS 对象，立即生效（注意：会被下一次自动同步覆盖，持久修复请走①）。"
    "字段说明：days[].events[] 中 time=开播时间(HH:MM)、member=成员"
    "(bella/jiaran/nailin/xinyi/sinuo/unknown)、title=标题、tag=live/show/special/rest、"
    "group_type=none/asoul/xinyi_sinuo/zhijiang_variety、"
    "format=normal/theater/night_talk/game_room/collab/commercial；"
    "recording_bvid=录播回放BV号（录播有误可删除该字段或替换为正确BV号）。"
    "修改数据后请递增 version 字段，客户端才会刷新。"
)

FLASH_COMMENT = (
    "突击直播（临时加场直播）列表，由系统自动发布。发现问题可手动修正："
    "① 推荐：修改 GitHub 仓库 data/flash.json 后 push，Actions 会自动同步覆盖本对象；"
    "② 快速修复：直接编辑本 OSS 对象，立即生效（注意：会被下一次自动同步覆盖，持久修复请走①）。"
    "字段说明：events[] 中 id=事件ID、member=成员、title=标题、start_time=开播时间、"
    "end_time=预计结束、source_url=原始动态链接、status=upcoming/live/ended。"
    "删除错误事件或修正字段后，请递增 version 字段，客户端才会刷新。"
)


def next_version(current: int | None, now: datetime | None = None) -> int:
    """版本号递增规则：max(当前小时戳, 当前版本+1)，保证严格单调递增。

    所有正式数据文件（latest.json / flash.json）的版本号统一走该函数：
    周程表发布与录播回填会改写 latest.json，突击直播发布/清理会改写
    flash.json，同一时间窗口内多次写入时，仅用时间戳会出现版本号持平/
    倒退，客户端据此判断更新会漏拉数据。
    """
    stamp = int((now or _now()).strftime("%Y%m%d%H"))
    return max(stamp, int(current or 0) + 1)


def publish_schedule(draft_path: Path = DRAFT_JSON, latest_path: Path = LATEST_JSON) -> None:
    """周程表发布：草稿 → 正式文件，并归档。"""
    ensure_dirs()
    with open(draft_path, encoding="utf-8") as f:
        data = json.load(f)

    # 补全团播分组/直播形式字段：旧草稿或手动编辑可能缺失，缺省 none/normal，
    # 保证客户端拿到的每条日程标签字段完整（客户端缺省也兼容，此处为契约兜底）
    for day in data.get("days", []):
        for event in day.get("events", []):
            event.setdefault("group_type", "none")
            event.setdefault("format", "normal")

    # 若已有正式文件，版本号须在其基础上严格递增（同小时重复发布场景）
    previous_version = 0
    if latest_path.exists():
        try:
            with open(latest_path, encoding="utf-8") as f:
                previous_version = int(json.load(f).get("version") or 0)
        except (ValueError, OSError):
            previous_version = 0

    data.pop("_meta", None)
    data["version"] = next_version(previous_version)
    data["updated_at"] = _now().isoformat()
    data["source"] = "auto"
    data = {"_comment": SCHEDULE_COMMENT, **data}  # 管理员注释置于文件顶部

    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # 归档到 archive/{week_start}.json
    archive_file = ARCHIVE_DIR / f"{data['week_start']}.json"
    shutil.copy(latest_path, archive_file)
    print(f"[publish] 周程表已发布: {latest_path}，归档: {archive_file}")


def publish_flash(draft: dict | None = None) -> bool:
    """突击直播发布：将草稿事件合并入 flash.json。

    draft 为 None 时从 flash_draft.json 读取。
    返回是否真正产生了新事件。
    """
    ensure_dirs()

    if draft is None:
        with open(FLASH_DRAFT_JSON, encoding="utf-8") as f:
            draft = json.load(f)

    # 草稿可能是单事件，也可能是 {"events": [...]} 包装
    new_events = draft.get("events", [draft]) if isinstance(draft, dict) else []

    # 读取现有正式文件
    if FLASH_JSON.exists():
        with open(FLASH_JSON, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"version": 0, "updated_at": _now().isoformat(), "events": []}

    existing_ids = {e.get("source_dynamic_id") for e in data["events"]}
    added = False
    for event in new_events:
        event.pop("_meta", None)
        if event.get("source_dynamic_id") in existing_ids:
            continue
        data["events"].append(event)
        existing_ids.add(event.get("source_dynamic_id"))
        added = True

    if added:
        # 同一分钟内多次发布时，分钟戳会持平，客户端按版本号比较将漏拉新事件，
        # 统一走 next_version 保证严格单调递增
        data["version"] = next_version(data.get("version"))
        data["updated_at"] = _now().isoformat()
        data = {"_comment": FLASH_COMMENT, **data}  # 管理员注释置于文件顶部
        with open(FLASH_JSON, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[publish] 突击直播已发布 {len(new_events)} 条，共 {len(data['events'])} 条")
    else:
        print("[publish] 无新突击直播事件，跳过写入")

    # 发布后清理草稿
    if FLASH_DRAFT_JSON.exists():
        FLASH_DRAFT_JSON.unlink()
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description="发布脚本")
    parser.add_argument(
        "--target",
        choices=["schedule", "flash"],
        default="schedule",
        help="发布目标：schedule=周程表，flash=突击直播",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="手动模式：管理员已手动修改草稿，直接发布",
    )
    args = parser.parse_args()

    if args.target == "schedule":
        publish_schedule()
    else:
        publish_flash()


if __name__ == "__main__":
    main()
