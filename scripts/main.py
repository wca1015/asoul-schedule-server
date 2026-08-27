"""主流程编排（统一入口）。

两种模式：
- ``--mode schedule``  管道A：官号周程表 抓取 → VLM识别 → 校验 → draft.json → 飞书审核通知
- ``--mode flash``     管道B：多账号突击直播监控 → 三级识别 → 校验 → flash_draft.json → 飞书紧急通知
                       （每轮开始先处理"超时草稿自动发布"与"过期事件清理"）

用法::

    python scripts/main.py --mode schedule
    python scripts/main.py --mode flash
    python scripts/main.py --mode flash --loop --interval 300   # 常驻进程（轻量服务器备选方案）

失败策略：
- 识别/校验失败时【不推进游标】，下一轮自动重试，同时飞书告警；
- 单条动态识别抛异常时【推进游标】，避免"毒消息"卡死后续动态处理。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime

import yaml

from common import (
    CST,
    DRAFT_JSON,
    FLASH_DRAFT_JSON,
    MEMBERS_YAML,
    ensure_dirs,
)


def load_config() -> dict:
    """读取 config/members.yaml。"""
    if not MEMBERS_YAML.exists():
        raise FileNotFoundError(f"缺少配置文件: {MEMBERS_YAML}")
    with open(MEMBERS_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# 管道A：周程表
# ---------------------------------------------------------------------------

def run_schedule(config: dict) -> None:
    """周程表管道：抓取官号最新图文动态 → 识别 → 校验 → 写草稿 → 通知审核。"""
    from fetch_dynamic import get_latest_draw_dynamic, is_new_dynamic, save_dynamic_id
    from notify import send_alert, send_schedule_review_card
    from recognize import recognize_schedule
    from validate import validate_schedule

    uid = os.environ.get("BILIBILI_UID") or str(config.get("official_uid") or "")
    if not uid:
        send_alert("周程表管道未运行", "缺少环境变量 BILIBILI_UID 或配置 official_uid")
        return

    dynamic = get_latest_draw_dynamic(uid)
    if dynamic is None:
        print("[schedule] 未获取到图文动态（可能风控/无新动态），本轮结束")
        return

    if not is_new_dynamic(dynamic):
        print(f"[schedule] 动态 {dynamic['dynamic_id']} 已处理过，跳过")
        return

    # 仅对"疑似周程表"的动态做识别：控制成本，避免把活动海报误当周程表
    trigger_keywords = config.get("schedule_keywords") or []
    text = dynamic.get("text", "")
    if trigger_keywords and text and not any(kw in text for kw in trigger_keywords):
        print(f"[schedule] 动态正文未包含周程表关键词，跳过识别: {text[:50]!r}")
        save_dynamic_id(dynamic["dynamic_id"])
        return

    if not dynamic.get("images"):
        print("[schedule] 动态无图片，跳过")
        save_dynamic_id(dynamic["dynamic_id"])
        return

    image_url = dynamic["images"][0]
    print(f"[schedule] 开始识别动态 {dynamic['dynamic_id']}，图片: {image_url}")

    try:
        result = recognize_schedule(image_url)
    except Exception as exc:  # 识别失败：告警 + 不推进游标，下一轮自动重试
        send_alert(
            "周程表识别失败",
            f"动态ID: {dynamic['dynamic_id']}\n原图: {image_url}\n"
            f"错误: {exc}\n下一轮将自动重试，如持续失败请人工处理",
        )
        return

    errors = validate_schedule(result)
    if errors:
        send_alert(
            "周程表校验失败",
            f"动态ID: {dynamic['dynamic_id']}\n原图: {image_url}\n"
            + "\n".join(f"- {e}" for e in errors)
            + "\n下一轮将自动重试，如持续失败请人工处理",
        )
        return

    draft = dict(result)
    draft["_meta"] = {
        "dynamic_id": dynamic["dynamic_id"],
        "image_url": image_url,
        "recognized_at": datetime.now(CST).isoformat(),
        "status": "pending_review",
    }

    ensure_dirs()
    with open(DRAFT_JSON, "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False, indent=2)

    save_dynamic_id(dynamic["dynamic_id"])
    send_schedule_review_card(draft, image_url)
    print(f"[schedule] 草稿已写入 {DRAFT_JSON}，等待人工审核")


# ---------------------------------------------------------------------------
# 管道B：突击直播
# ---------------------------------------------------------------------------

def _read_pending_flash_draft() -> tuple[list[dict], dict | None]:
    """读取待审核的突击直播草稿。

    返回 (事件列表, _meta)；文件不存在 / 非法 / 非 pending 状态时返回 ([], None)。
    兼容两种结构：包装式 {"_meta":..., "events":[...]} 与扁平单事件式。
    """
    if not FLASH_DRAFT_JSON.exists():
        return [], None
    try:
        with open(FLASH_DRAFT_JSON, encoding="utf-8") as f:
            draft = json.load(f)
    except (ValueError, OSError):
        return [], None

    meta = draft.get("_meta") or {}
    if meta.get("status") != "pending_review":
        return [], None

    events = draft.get("events")
    if isinstance(events, list):
        return events, meta
    # 扁平单事件结构
    return [{k: v for k, v in draft.items() if k != "_meta"}], meta


def _write_flash_draft(events: list[dict], meta: dict) -> None:
    ensure_dirs()
    with open(FLASH_DRAFT_JSON, "w", encoding="utf-8") as f:
        json.dump({"_meta": meta, "events": events}, f, ensure_ascii=False, indent=2)


def run_flash(config: dict) -> None:
    """突击直播管道：超时检查 → 过期清理 → 多账号抓取 → 三级识别 → 草稿 + 通知。"""
    from auto_publish_timeout import check_and_auto_publish
    from flash_manager import cleanup_expired
    from flash_monitor import fetch_all, update_cursor
    from flash_recognize import recognize_flash
    from notify import send_alert, send_flash_review_card
    from validate import validate_flash_event

    # 1. 优先处理上一轮遗留的超时草稿（可能触发自动发布）
    check_and_auto_publish()

    # 2. 清理过期事件（48小时）
    cleanup_expired()

    members = config.get("members") or []
    if not members:
        send_alert("突击直播管道未运行", "members.yaml 中未配置 members")
        return

    pairs = fetch_all(members)
    if not pairs:
        print("[flash] 无新动态")
        return

    new_events: list[dict] = []
    for account, dynamic in pairs:
        uid = str(account["uid"])
        dynamic_id = dynamic["dynamic_id"]
        label = f"{account.get('name', uid)} ({dynamic_id})"

        try:
            event = recognize_flash(dynamic, account, config)
        except Exception as exc:
            # 异常时推进游标，避免单条"毒消息"卡死后续所有动态
            send_alert(
                "突击直播识别异常",
                f"动态: {label}\n错误: {exc}\n已推进游标，请人工核查该动态",
            )
            update_cursor(uid, dynamic_id)
            continue

        update_cursor(uid, dynamic_id)

        if event is None:
            continue

        errors = validate_flash_event(event)
        if errors:
            send_alert(
                "突击直播校验失败",
                f"动态: {label}\n"
                + "\n".join(f"- {e}" for e in errors)
                + f"\n原动态: {event.get('source_url')}\n请人工核查",
            )
            continue

        new_events.append(event)

    if not new_events:
        return

    # 合并进待审核草稿：若已有未审核草稿则追加（不重置超时倒计时）
    pending_events, pending_meta = _read_pending_flash_draft()
    merged = pending_events + new_events
    if pending_meta:
        meta = dict(pending_meta)
        meta["status"] = "pending_review"
    else:
        meta = {
            "status": "pending_review",
            "recognized_at": datetime.now(CST).isoformat(),
        }
    _write_flash_draft(merged, meta)

    for event in new_events:
        send_flash_review_card(event)

    print(f"[flash] 草稿已写入 {FLASH_DRAFT_JSON}（共 {len(merged)} 条待审核），10分钟未审核将自动发布")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _run_once(mode: str, config: dict) -> None:
    if mode == "schedule":
        run_schedule(config)
    else:
        run_flash(config)


def main() -> None:
    parser = argparse.ArgumentParser(description="A-SOUL 周程表服务器主流程")
    parser.add_argument("--mode", choices=["schedule", "flash"], required=True,
                        help="schedule=周程表管道，flash=突击直播管道")
    parser.add_argument("--loop", action="store_true",
                        help="常驻循环运行（轻量服务器部署用，推荐仅 flash 模式使用）")
    parser.add_argument("--interval", type=int, default=300,
                        help="循环模式间隔秒数，默认300（5分钟）")
    args = parser.parse_args()

    config = load_config()
    ensure_dirs()

    if not args.loop:
        _run_once(args.mode, config)
        return

    print(f"[main] 常驻模式启动: mode={args.mode} interval={args.interval}s")
    while True:
        try:
            _run_once(args.mode, config)
        except Exception as exc:
            print(f"[main] 本轮执行异常: {exc}")
            try:
                from notify import send_alert

                send_alert("主流程异常", f"mode={args.mode}\n{exc}")
            except Exception:
                pass
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
