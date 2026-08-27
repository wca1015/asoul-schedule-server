"""管道B：多账号动态抓取。

轮询 6 个账号（官号 + 5 个成员号）的动态，
只返回上次处理位置之后的新动态，用于突击直播检测。

防风控策略与管道A一致：真实 UA/Referer、串行请求、风控静默跳过。
"""
from __future__ import annotations

import os
import time

import requests

from common import DATA_DIR

DYNAMIC_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://space.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}

# 单次抓取之间的间隔（秒），串行 + 小延迟降低风控风险
REQUEST_GAP_SECONDS = 2


def _state_file(uid: str):
    return DATA_DIR / f"last_flash_{uid}.txt"


def _load_last_id(uid: str) -> str | None:
    f = _state_file(uid)
    return f.read_text().strip() if f.exists() else None


def _save_last_id(uid: str, dynamic_id: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _state_file(uid).write_text(dynamic_id)


def fetch_new_dynamics(uid: str) -> list[dict]:
    """获取指定账号比上次记录更新的动态列表（按发布时间从旧到新）。

    返回结构：
    [
        {
            "dynamic_id": str,
            "text": str,          # 动态正文（含图片描述若有）
            "images": [url, ...], # 附带图片
            "pub_ts": int,
        },
        ...
    ]
    接口异常/风控时返回空列表，静默跳过。

    首次运行（无游标）时不返回任何动态：只记录最新一条作为基线，
    避免把历史几十条动态全部送进识别流水线（浪费 API 费用）。
    """
    headers = dict(HEADERS)
    cookie = os.environ.get("BILIBILI_COOKIE")
    if cookie:
        headers["Cookie"] = cookie

    try:
        resp = requests.get(
            DYNAMIC_API,
            params={"host_mid": uid},
            headers=headers,
            timeout=10,
        )
        if resp.status_code in (412, 403):
            print(f"[flash-fetch] uid={uid} 触发风控 (HTTP {resp.status_code})，跳过")
            return []
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[flash-fetch] uid={uid} 请求失败: {exc}，跳过")
        return []

    if data.get("code") != 0:
        print(f"[flash-fetch] uid={uid} 接口异常: {data.get('message')}")
        return []

    items = data.get("data", {}).get("items", [])
    last_id = _load_last_id(uid)

    if not last_id:
        # 首次运行：仅记录基线，不处理任何历史动态
        if items:
            baseline = items[0].get("id_str")
            if baseline:
                _save_last_id(uid, baseline)
                print(f"[flash-fetch] uid={uid} 首次运行，基线设为 {baseline}")
        return []

    new_items: list[dict] = []

    for item in items:
        try:
            dynamic_id = item["id_str"]
            if last_id and dynamic_id == last_id:
                break  # 到达上次处理位置，其后（更新）的已收集完

            modules = item.get("modules", {})
            major = modules.get("module_dynamic", {}).get("major") or {}
            desc_node = modules.get("module_dynamic", {}).get("desc") or {}

            text = desc_node.get("text", "")
            images: list[str] = []
            if major.get("type") == "MAJOR_TYPE_DRAW":
                images = [img["src"] for img in major["draw"]["items"]]
                # 图片 alt 文本也可能含有信息
            elif major.get("type") == "MAJOR_TYPE_OPUS":
                # 新版 opus 结构：正文在 summary 中
                summary = major.get("opus", {}).get("summary", {})
                text = text or summary.get("text", "")
                pics = major.get("opus", {}).get("pics", [])
                images = [p.get("src", "") for p in pics if p.get("src")]

            new_items.append(
                {
                    "dynamic_id": dynamic_id,
                    "text": text,
                    "images": images,
                    "pub_ts": modules.get("module_author", {}).get("pub_ts", 0),
                }
            )
        except (KeyError, TypeError) as exc:
            print(f"[flash-fetch] uid={uid} 动态解析失败: {exc}")
            continue

    new_items.reverse()  # 转为从旧到新，便于顺序处理
    return new_items


def update_cursor(uid: str, dynamic_id: str) -> None:
    """更新某账号的处理游标。"""
    _save_last_id(uid, dynamic_id)


def fetch_all(members: list[dict]) -> list[tuple[dict, dict]]:
    """串行抓取所有账号的新动态。

    参数：members 为 members.yaml 中的账号列表
    返回：[(account, dynamic), ...]
    """
    results: list[tuple[dict, dict]] = []
    for i, account in enumerate(members):
        uid = str(account["uid"])
        for dynamic in fetch_new_dynamics(uid):
            results.append((account, dynamic))
        if i < len(members) - 1:
            time.sleep(REQUEST_GAP_SECONDS)
    return results
