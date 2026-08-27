"""飞书通知模块（共享）。

使用自定义机器人 Webhook（环境变量 FEISHU_WEBHOOK），
发送交互卡片：周程表审核 / 突击直播紧急审核 / 异常告警。

若未配置 FEISHU_WEBHOOK，仅打印日志不报错，便于本地调试。
"""
from __future__ import annotations

import os

import requests

REQUEST_TIMEOUT = 10


def _webhook() -> str | None:
    return os.environ.get("FEISHU_WEBHOOK")


def _post(payload: dict) -> None:
    url = _webhook()
    if not url:
        print(f"[notify] 未配置 FEISHU_WEBHOOK，跳过推送: {payload}")
        return
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        print(f"[notify] 飞书消息已发送: {resp.text[:200]}")
    except requests.RequestException as exc:
        # 通知失败不应阻断主流程
        print(f"[notify] 飞书消息发送失败: {exc}")


def build_card(
    title: str,
    content: str,
    image_url: str | None = None,
    actions: list[str] | None = None,
    at_all: bool = False,
) -> dict:
    """构建飞书交互卡片消息体。"""
    elements: list[dict] = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": content},
        }
    ]

    if image_url:
        # 自定义机器人无法直接渲染外部图片，提供"查看原图"按钮代替
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "🖼️ 查看原图"},
                        "url": image_url,
                        "type": "primary",
                    }
                ],
            }
        )

    if actions:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": a}}
                    for a in actions
                ],
            }
        )

    if at_all:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": '<at id="all"></at>'},
            }
        )

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "red" if at_all else "blue",
            },
            "elements": elements,
        },
    }


def send_schedule_review_card(draft: dict, image_url: str) -> None:
    """周程表审核通知。"""
    content = (
        f"**周期**：{draft['week_start']} ~ {draft['week_end']}\n"
        f"**事件数**：{sum(len(d['events']) for d in draft['days'])}\n"
        f"**识别时间**：{draft['_meta']['recognized_at']}\n"
        f"**置信度**：{draft['_meta'].get('confidence', 'N/A')}"
    )
    card = build_card(
        title="📋 新周程表待审核",
        content=content,
        image_url=image_url,
        actions=["✅ 确认发布", "❌ 驳回"],
    )
    _post(card)


def send_flash_review_card(draft: dict) -> None:
    """突击直播紧急审核通知（@所有人）。"""
    content = (
        f"**成员**：{draft['member']}\n"
        f"**标题**：{draft['title']}\n"
        f"**开播时间**：{draft['start_time']}\n"
        f"**来源**：{draft['source_url']}\n"
        f"**倒计时**：10:00（超时将自动发布）"
    )
    card = build_card(
        title="🔴 突击直播待审核（10分钟超时）",
        content=content,
        actions=["✅ 确认发布", "❌ 驳回"],
        at_all=True,
    )
    _post(card)


def send_alert(title: str, detail: str) -> None:
    """异常告警。"""
    card = build_card(title=f"🚨 {title}", content=detail)
    _post(card)
