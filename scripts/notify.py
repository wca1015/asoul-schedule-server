"""飞书通知模块（共享）。

使用自定义机器人 Webhook（环境变量 FEISHU_WEBHOOK），
发送交互卡片：周程表审核 / 突击直播紧急审核 / 异常告警。

注意：自定义机器人的卡片按钮【没有回调能力】，点了不会有任何响应。
因此审核按钮统一做成 URL 跳转：点击后打开 GitHub「人工审核」
工作流（manual_review.yml）页面，管理员选择 target / action
（publish=发布，reject=驳回）后点 Run workflow 即完成审核，
实际动作由 scripts/review_action.py 在工作流中执行。

若未配置 FEISHU_WEBHOOK，仅打印日志不报错，便于本地调试。
"""
from __future__ import annotations

import os

import requests

REQUEST_TIMEOUT = 10

# 默认仓库地址（本地调试用）；在 GitHub Actions 中运行时
# 会自动使用 GITHUB_REPOSITORY 环境变量，fork 后无需改代码。
DEFAULT_REPO = "wca1015/asoul-schedule-server"


def _webhook() -> str | None:
    return os.environ.get("FEISHU_WEBHOOK")


def review_page_url() -> str:
    """GitHub「人工审核」工作流（manual_review.yml）页面地址。

    点击飞书卡片按钮后跳转到该页面，管理员在手机上选择
    target（schedule/flash）与 action（publish/reject），
    点 Run workflow 即完成审核。
    优先级：REVIEW_PAGE_URL 环境变量 > GITHUB_REPOSITORY 拼接 > 默认仓库。
    """
    override = os.environ.get("REVIEW_PAGE_URL")
    if override:
        return override
    repo = os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO)
    return f"https://github.com/{repo}/actions/workflows/manual_review.yml"


def _review_hint(target: str) -> str:
    """卡片正文中的审核操作指引（飞书 lark_md 格式）。"""
    return (
        "\n---\n"
        f"👇 点击下方按钮打开 GitHub 审核页 → target 选 **{target}** → "
        "action 选 **publish**（发布）或 **reject**（驳回）→ 点 **Run workflow**"
    )


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


def _review_buttons() -> list[dict]:
    """审核按钮：跳转到 GitHub「人工审核」工作流页面。

    飞书自定义机器人按钮无回调，两个按钮都打开同一审核页，
    管理员在页面上选择 publish（发布）或 reject（驳回）后运行。
    """
    url = review_page_url()
    return [
        {"text": "✅ 前往审核发布", "url": url, "type": "primary"},
        {"text": "❌ 前往审核驳回", "url": url, "type": "danger"},
    ]


def send_schedule_review_card(draft: dict, image_url: str) -> None:
    """周程表审核通知。"""
    content = (
        f"**周期**：{draft['week_start']} ~ {draft['week_end']}\n"
        f"**事件数**：{sum(len(d['events']) for d in draft['days'])}\n"
        f"**识别时间**：{draft['_meta']['recognized_at']}\n"
        f"**置信度**：{draft['_meta'].get('confidence', 'N/A')}"
    )
    content += _review_hint("schedule")
    card = build_card(
        title="📋 新周程表待审核",
        content=content,
        image_url=image_url,
        actions=_review_buttons(),
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
    content += _review_hint("flash")
    card = build_card(
        title="🔴 突击直播待审核（10分钟超时）",
        content=content,
        actions=_review_buttons(),
        at_all=True,
    )
    _post(card)


def send_alert(title: str, detail: str) -> None:
    """异常告警。"""
    card = build_card(title=f"🚨 {title}", content=detail)
    _post(card)
