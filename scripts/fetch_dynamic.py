"""管道A：B站官号动态抓取。

抓取官号最新一条图文动态（周程表图片）。

防风控策略：
- 请求间隔 ≥ 30 分钟（由 GitHub Actions Cron 控制）
- 携带真实 User-Agent 和 Referer
- 单账号单IP，不做并发
- 遇 412/风控响应，静默跳过，下一轮重试
"""
from __future__ import annotations

import os

import requests

from common import DATA_DIR, LAST_DYNAMIC_ID_FILE, get_env

DYNAMIC_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://space.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}


def get_latest_draw_dynamic(uid: str | None = None) -> dict | None:
    """获取最新一条图文动态。

    返回结构：
    {
        "dynamic_id": str,
        "images": [url, ...],
        "pub_ts": int,
        "text": str,
    }
    无图文动态或接口异常时返回 None。
    """
    uid = uid or get_env("BILIBILI_UID")

    # 可选：携带 Cookie 进一步降低风控概率
    cookie = os.environ.get("BILIBILI_COOKIE")
    headers = dict(HEADERS)
    if cookie:
        headers["Cookie"] = cookie

    try:
        resp = requests.get(
            DYNAMIC_API,
            params={"host_mid": uid},
            headers=headers,
            timeout=10,
        )
        # 风控响应：静默跳过，下一轮重试
        if resp.status_code in (412, 403):
            print(f"[fetch] 触发风控 (HTTP {resp.status_code})，本轮跳过")
            return None
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[fetch] 请求失败: {exc}，本轮跳过")
        return None

    if data.get("code") != 0:
        print(f"[fetch] 接口返回异常: code={data.get('code')} message={data.get('message')}")
        return None

    for item in data.get("data", {}).get("items", []):
        if item.get("type") != "DYNAMIC_TYPE_DRAW":
            continue

        try:
            major = item["modules"]["module_dynamic"]["major"]
            images = [img["src"] for img in major["draw"]["items"]]
            desc_node = item["modules"]["module_dynamic"].get("desc") or {}
            return {
                "dynamic_id": item["id_str"],
                "images": images,
                "pub_ts": item["modules"]["module_author"]["pub_ts"],
                "text": desc_node.get("text", ""),
            }
        except (KeyError, TypeError) as exc:
            print(f"[fetch] 动态结构解析失败: {exc}")
            return None

    print("[fetch] 未找到图文动态")
    return None


def is_new_dynamic(dynamic: dict) -> bool:
    """判断是否为未处理过的新动态。"""
    if not LAST_DYNAMIC_ID_FILE.exists():
        return True
    return dynamic["dynamic_id"] != LAST_DYNAMIC_ID_FILE.read_text().strip()


def save_dynamic_id(dynamic_id: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LAST_DYNAMIC_ID_FILE.write_text(dynamic_id)
    print(f"[fetch] 已记录处理的动态ID: {dynamic_id}")
