"""飞书通知模块（共享）。

使用自定义机器人 Webhook（环境变量 FEISHU_WEBHOOK），
发送交互卡片：周程表已发布 / 突击直播已发布 / 异常告警。

两条数据管道（周程表、突击直播）均为「校验通过即自动发布」，
飞书只做已发布通知，不再有审核按钮/人工审核流程。

若未配置 FEISHU_WEBHOOK，仅打印日志不报错，便于本地调试。
"""
from __future__ import annotations

import os
from datetime import datetime

import requests

from common import CST

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
    actions: list[dict] | None = None,
    at_all: bool = False,
) -> dict:
    """构建飞书交互卡片消息体。

    actions: URL 跳转按钮列表，每项形如
        {"text": "按钮文案", "url": "跳转地址", "type": "primary"/"default"/"danger"}
    自定义机器人卡片按钮无回调，所有交互都通过 url 跳转实现。
    """
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
                        "type": "default",
                    }
                ],
            }
        )

    if actions:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": a["text"]},
                        "url": a["url"],
                        "type": a.get("type", "default"),
                    }
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


def send_schedule_published_card(draft: dict, image_url: str) -> None:
    """周程表已自动发布通知（管道A：校验通过即发布，无需人工审核）。"""
    content = (
        f"**周期**：{draft['week_start']} ~ {draft['week_end']}\n"
        f"**事件数**：{sum(len(d['events']) for d in draft['days'])}\n"
        f"**发布时间**：{datetime.now(CST).isoformat()}\n"
        f"**说明**：周程表已校验通过并自动发布，客户端下一轮同步即可看到"
    )
    card = build_card(
        title="✅ 新周程表已自动发布",
        content=content,
        image_url=image_url,
    )
    _post(card)


def send_flash_published_card(draft: dict) -> None:
    """突击直播已自动发布通知（管道B：识别校验通过即发布，无需人工审核）。"""
    content = (
        f"**成员**：{draft['member']}\n"
        f"**标题**：{draft['title']}\n"
        f"**开播时间**：{draft['start_time']}\n"
        f"**来源**：{draft['source_url']}\n"
        f"**说明**：已校验通过并自动发布，客户端 5 分钟内可见"
    )
    card = build_card(
        title="⚡ 突击直播已自动发布",
        content=content,
    )
    _post(card)


def send_alert(title: str, detail: str) -> None:
    """异常告警。"""
    card = build_card(title=f"🚨 {title}", content=detail)
    _post(card)
