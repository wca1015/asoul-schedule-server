"""主流程编排（统一入口）。

两种模式：
- ``--mode schedule``  管道A：官号周程表 抓取 → VLM识别 → 校验 → 自动发布 → 飞书已发布通知
                       （无需人工审核，校验通过即发布）
- ``--mode flash``     管道B：多账号突击直播监控 → 三级识别 → 校验 → 直接自动发布 → 飞书已发布通知
                       （无需人工审核；每轮开始先清理过期事件）

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
    LATEST_JSON,
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
    """周程表管道：抓取官号最新图文动态 → 识别 → 校验 → 自动发布 → 飞书已发布通知。

    周程表不再人工审核：校验通过即直接发布（latest.json + 归档），
    只通过飞书通知"已发布"，降低人工成本。
    """
    from fetch_dynamic import get_latest_draw_dynamic, is_new_dynamic, save_dynamic_id
    from notify import send_alert, send_schedule_published_card
    from publish import publish_schedule
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
    }

    # 校验通过 → 直接自动发布：写临时草稿 → publish_schedule() → 清理草稿 → 推进游标 → 飞书通知
    # 发布成功后才推进游标：若发布抛异常，下一轮自动重试（草稿已清理）
    ensure_dirs()
    with open(DRAFT_JSON, "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False, indent=2)

    try:
        publish_schedule()
    finally:
        DRAFT_JSON.unlink(missing_ok=True)  # 发布后清理临时草稿，避免残留
    save_dynamic_id(dynamic["dynamic_id"])
    send_schedule_published_card(draft, image_url)
    print(f"[schedule] 周程表已自动发布: {LATEST_JSON}")


# ---------------------------------------------------------------------------
# 管道B：突击直播
# ---------------------------------------------------------------------------

def run_flash(config: dict) -> None:
    """突击直播管道：过期清理 → 多账号抓取 → 三级识别 → 校验 → 直接自动发布 + 飞书通知。

    突击直播不再人工审核：识别校验通过即直接发布到 flash.json（按
    source_dynamic_id 去重、幂等），并飞书通知"已发布"。
    """
    from flash_manager import cleanup_expired
    from flash_monitor import fetch_all, update_cursor
    from flash_recognize import recognize_flash
    from notify import send_alert, send_flash_published_card
    from publish import publish_flash
    from validate import validate_flash_event

    # 清理过期事件（48小时）
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

    # 校验通过 → 直接自动发布（无需草稿/人工审核；publish_flash 按 source_dynamic_id 去重）
    draft = {
        "_meta": {"recognized_at": datetime.now(CST).isoformat()},
        "events": new_events,
    }
    if not publish_flash(draft):
        print("[flash] 无新增突击直播事件（均已发布过），跳过通知")
        return

    for event in new_events:
        send_flash_published_card(event)

    print(f"[flash] 突击直播已自动发布 {len(new_events)} 条")


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
