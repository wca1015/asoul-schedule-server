"""探测第二轮：带上匿名 buvid 后，验证「无登录 Cookie」能走通哪些接口。

第一轮结论（本机 + Actions）：
- getRoomPlayInfo / card：匿名可用
- 动态接口：412
- WBI 投稿接口：-352 风控校验失败（疑似签名缺 buvid3 参数）

本轮重点：
  C. WBI 签名追加 buvid3 参数 + Cookie 带 buvid3/buvid4（新版签名规则）
  D. 旧版接口（dynamic_svr / space/arc/search 无 WBI）
"""
from __future__ import annotations

import hashlib
import json
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

UID = "3493085336046382"      # 枝江娱乐 Official
MEMBER_UID = "672328094"      # 嘉然 Diana

WBI_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def short(resp) -> str:
    try:
        data = resp.json()
        info = f"HTTP {resp.status_code} code={data.get('code')} msg={data.get('message','')}"
        d = data.get("data")
        if data.get("code") == 0 and isinstance(d, dict):
            info += f" keys={list(d.keys())[:8]}"
        return info
    except ValueError:
        return f"HTTP {resp.status_code} non-json"


def get_buvid() -> tuple[str, str]:
    r = requests.get("https://api.bilibili.com/x/frontend/finger/spi",
                     headers={"User-Agent": UA}, timeout=10)
    d = r.json().get("data", {})
    return d.get("b_3", ""), d.get("b_4", "")


def wbi_keys(s: requests.Session) -> tuple[str, str]:
    d = s.get("https://api.bilibili.com/x/web-interface/nav", timeout=10).json()
    wbi = (d.get("data") or {}).get("wbi_img") or {}
    img = wbi["img_url"].rsplit("/", 1)[-1].split(".")[0]
    sub = wbi["sub_url"].rsplit("/", 1)[-1].split(".")[0]
    return img, sub


def sign(params: dict, img_key: str, sub_key: str, buvid3: str = "") -> dict:
    mixin = "".join((img_key + sub_key)[i] for i in WBI_TAB)[:32]
    params = dict(params)
    params["wts"] = int(time.time())
    if buvid3:
        params["buvid3"] = buvid3  # 新版签名：buvid3 参与排序签名
    params = dict(sorted(params.items()))
    params = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
    params["w_rid"] = hashlib.md5(
        (urllib.parse.urlencode(params) + mixin).encode()).hexdigest()
    return params


def main() -> None:
    buvid3, buvid4 = get_buvid()
    print(f"buvid3={buvid3[:16]}... buvid4={buvid4[:16]}...")

    s = requests.Session()
    s.headers.update(HEADERS)
    s.headers["Cookie"] = f"buvid3={buvid3}; buvid4={buvid4}"

    img_key, sub_key = wbi_keys(s)
    print(f"wbi keys ok: {img_key[:8]}.../{sub_key[:8]}...")

    # C1. WBI 投稿 + buvid3 参与签名（官方号）
    p = sign({"mid": UID, "ps": 10, "pn": 1, "tid": 0, "keyword": "", "order": "pubdate",
              "platform": "web", "web_location": 1550101, "order_avoided": "true"},
             img_key, sub_key, buvid3)
    print("[C1 wbi投稿+buvid3签名 官号]", short(s.get(
        "https://api.bilibili.com/x/space/wbi/arc/search", params=p, timeout=10)))
    time.sleep(1)

    # C2. 同上，换成员号（嘉然）
    p = sign({"mid": MEMBER_UID, "ps": 10, "pn": 1, "tid": 0, "keyword": "", "order": "pubdate",
              "platform": "web", "web_location": 1550101, "order_avoided": "true"},
             img_key, sub_key, buvid3)
    print("[C2 wbi投稿+buvid3签名 嘉然]", short(s.get(
        "https://api.bilibili.com/x/space/wbi/arc/search", params=p, timeout=10)))
    time.sleep(1)

    # C3. 动态接口（带 buvid，看是否从 412 变为 -352 或 0）
    print("[C3 动态+buvid 官号]", short(s.get(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
        params={"host_mid": UID}, timeout=10)))
    time.sleep(1)

    # D1. 旧版动态接口（dynamic_svr）
    print("[D1 dynamic_svr 官号]", short(s.get(
        "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/space_history",
        params={"host_uid": UID, "offset_dynamic_id": 0, "need_top": 1}, timeout=10)))
    time.sleep(1)

    # D2. 旧版投稿接口（无 WBI）
    print("[D2 space/arc/search 官号]", short(s.get(
        "https://api.bilibili.com/x/space/arc/search",
        params={"mid": UID, "ps": 10, "pn": 1, "order": "pubdate"}, timeout=10)))
    time.sleep(1)

    # E. getRoomPlayInfo 结构确认（匿名直播状态源）
    r = s.get("https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo",
              params={"room_id": 22637261, "protocol": "0,1", "format": "0,1,2",
                      "codec": "0,1", "qn": 0}, timeout=10)
    d = r.json().get("data", {})
    print(f"[E getRoomPlayInfo] live_status={d.get('live_status')} "
          f"title={d.get('title')} live_time={d.get('live_time')} "
          f"room_id={d.get('room_id')}")

    # F. up 开播/预约状态聚合接口（web-interface 侧）
    print("[F xlive room-info uid]", short(s.get(
        "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByUser",
        params={"uid": MEMBER_UID}, timeout=10)))


if __name__ == "__main__":
    main()
