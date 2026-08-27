"""人工审核操作脚本（配合 manual_review.yml 工作流）。

飞书自定义机器人卡片按钮无回调能力，因此审核动作通过
GitHub Actions workflow_dispatch 完成：管理员点击飞书卡片上的
跳转按钮 → 打开审核工作流页面 → 选择 target/action → Run。

用法：
    python scripts/review_action.py --target schedule --action publish
    python scripts/review_action.py --target flash --action reject
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common import DRAFT_JSON, FLASH_DRAFT_JSON
from notify import send_alert
from publish import publish_flash, publish_schedule


def do_publish(target: str) -> None:
    if target == "schedule":
        if not DRAFT_JSON.exists():
            send_alert("审核操作失败", "未找到周程表草稿（data/draft.json），可能已处理过")
            print("[review] 无周程表草稿，跳过")
            return
        publish_schedule()
        DRAFT_JSON.unlink(missing_ok=True)  # 发布后清理草稿，避免重复发布
        send_alert("✅ 周程表已人工发布", "管理员已通过 GitHub 审核工作流确认发布，数据将在下一轮同步到 OSS")
    else:
        if not FLASH_DRAFT_JSON.exists():
            send_alert("审核操作失败", "未找到突击直播草稿（data/flash_draft.json），可能已处理/超时自动发布")
            print("[review] 无突击直播草稿，跳过")
            return
        added = publish_flash()
        send_alert(
            "✅ 突击直播已人工发布" if added else "突击直播审核完成",
            "管理员已通过 GitHub 审核工作流确认发布"
            + ("" if added else "（无新事件）"),
        )


def do_reject(target: str) -> None:
    draft_path: Path = DRAFT_JSON if target == "schedule" else FLASH_DRAFT_JSON
    name = "周程表" if target == "schedule" else "突击直播"
    if not draft_path.exists():
        print(f"[review] 无{name}草稿，无需驳回")
        return
    draft_path.unlink()
    send_alert(f"❌ {name}草稿已驳回", "管理员已通过 GitHub 审核工作流驳回草稿，等待下一轮重新识别")
    print(f"[review] {name}草稿已驳回并删除")


def main() -> int:
    parser = argparse.ArgumentParser(description="人工审核操作")
    parser.add_argument("--target", choices=["schedule", "flash"], required=True)
    parser.add_argument("--action", choices=["publish", "reject"], required=True)
    args = parser.parse_args()

    if args.action == "publish":
        do_publish(args.target)
    else:
        do_reject(args.target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
