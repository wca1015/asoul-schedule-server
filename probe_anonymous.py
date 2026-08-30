"""一次性探测脚本：验证不带登录 Cookie 时各 B 站接口的可用性。

测试路线：
  A. 完全裸请求（无任何 Cookie）
  B. 匿名设备指纹：先 GET /x/frontend/finger/spi 拿 buvid3/buvid4，带在 Cookie 里
对每条路线分别探测：
  1. nav（取 WBI key，匿名时应返回 code=-101 但 wbi_img 可用）
  2. WBI 签名后的空间投稿列表（录播回填同款接口）
  3. 动态列表（周程表/突击直播同款接口）
  4. 直播间信息（live room）——直播状态/预约相关
"""
from __future__ import annotations

import hashlib
import time
import urllib.parse

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://space.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}

# 官方号（周程表发布源）：枝江娱乐 Official
UID = "3493085336046382"

WBI_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def get(session: requests.Session, url: str, params: dict | None = None) -> str:
    try:
        r = session.get(url, params=params, timeout=10)
    except requests.RequestException as exc:
        return f"EXC {exc}"
    try:
        data = r.json()
        code = data.get("code")
        msg = data.get("message", "")
        info = f"HTTP {r.status_code} code={code} msg={msg}"
        if code == 0 and isinstance(data.get("data"), dict):
            info += f" keys={list(data['data'].keys())[:6]}"
        return info
    except ValueError:
        return f"HTTP {r.status_code} non-json"


def get_buvid() -> dict[str, str]:
    """匿名设备指纹：无需登录，公开接口。"""
    r = requests.get(
        "https://api.bilibili.com/x/frontend/finger/spi",
        headers={"User-Agent": UA},
        timeout=10,
    )
    data = r.json().get("data", {})
    return {"buvid3": data.get("b_3", ""), "buvid4": data.get("b_4", "")}


def sign(params: dict, img_key: str, sub_key: str) -> dict:
    mixin = "".join((img_key + sub_key)[i] for i in WBI_TAB)[:32]
    params = dict(params)
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    params = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
    params["w_rid"] = hashlib.md5(
        (urllib.parse.urlencode(params) + mixin).encode()
    ).hexdigest()
    return params


def probe(route: str, cookie: str) -> None:
    print(f"\n===== 路线 {route} =====")
    s = requests.Session()
    s.headers.update(HEADERS)
    if cookie:
        s.headers["Cookie"] = cookie

    # 1. nav -> wbi keys
    print(f"[nav] {get(s, 'https://api.bilibili.com/x/web-interface/nav')}")
    img_key = sub_key = None
    try:
        d = s.get("https://api.bilibili.com/x/web-interface/nav", timeout=10).json()
        wbi = (d.get("data") or {}).get("wbi_img") or {}
        if wbi.get("img_url"):
            img_key = wbi["img_url"].rsplit("/", 1)[-1].split(".")[0]
            sub_key = wbi["sub_url"].rsplit("/", 1)[-1].split(".")[0]
    except Exception:
        pass

    # 2. WBI 投稿列表（录播回填接口）
    if img_key:
        params = sign(
            {"mid": UID, "ps": 10, "pn": 1, "tid": 0, "keyword": "", "order": "pubdate",
             "platform": "web", "web_location": 1550101, "order_avoided": "true"},
            img_key, sub_key,
        )
        print(f"[wbi投稿] {get(s, 'https://api.bilibili.com/x/space/wbi/arc/search', params)}")
    else:
        print("[wbi投稿] 无 wbi_img，跳过")

    # 3. 动态列表
    print(f"[动态] {get(s, 'https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space', {'host_mid': UID})}")

    # 4. 直播间播放信息（getRoomPlayInfo，通常最宽松；嘉然房间 22637261）
    print(f"[getRoomPlayInfo] {get(s, 'https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo', {'room_id': 22637261, 'protocol': '0,1', 'format': '0,1,2', 'codec': '0,1', 'qn': 0})}")

    # 5. 直播间基础信息（getInfoByRoom，含预约/开播状态字段）
    print(f"[getInfoByRoom] {get(s, 'https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom', {'room_id': 22637261})}")

    # 6. 用户卡片（匿名可见程度）
    print(f"[card] {get(s, 'https://api.bilibili.com/x/web-interface/card', {'mid': UID})}")


def main() -> None:
    print("拿 buvid ...")
    buvid = get_buvid()
    print(f"buvid3={buvid['buvid3'][:16]}... buvid4={buvid['buvid4'][:16]}...")

    probe("A 完全裸请求", "")
    time.sleep(1)
    probe("B 仅 buvid 匿名指纹", f"buvid3={buvid['buvid3']}; buvid4={buvid['buvid4']}")


if __name__ == "__main__":
    main()
