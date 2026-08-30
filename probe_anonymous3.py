"""探测第三轮：隔离变量，钉死「匿名能否稳定拿到投稿列表」。

第二轮矛盾：本地嘉然 wbi投稿 code=0，Actions 嘉然 -352。
猜测是第二轮连续请求多接口（官号412 → 嘉然）触发会话级串联风控。

本轮做法：每次运行只发【一个】投稿请求，带完整浏览器指纹头，
通过命令行参数切换目标账号，互不干扰。

用法:
    python probe_anonymous3.py <mid>
"""
from __future__ import annotations

import hashlib
import sys
import time
import urllib.parse

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

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
            vlist = (d.get("list") or {}).get("vlist") or []
            info += f" vlist={len(vlist)}"
            if vlist:
                info += f" latest=[{vlist[0].get('bvid')}] {vlist[0].get('title','')[:20]}"
        return info
    except ValueError:
        return f"HTTP {resp.status_code} non-json"


def main() -> None:
    mid = sys.argv[1] if len(sys.argv) > 1 else "672328094"

    # 第一步：拿 buvid（独立请求）
    r = requests.get("https://api.bilibili.com/x/frontend/finger/spi",
                     headers={"User-Agent": UA}, timeout=10)
    d = r.json().get("data", {})
    buvid3, buvid4 = d.get("b_3", ""), d.get("b_4", "")

    # 第二步：完整浏览器指纹会话
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Referer": f"https://space.bilibili.com/{mid}/video",
        "Origin": "https://space.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Cookie": f"buvid3={buvid3}; buvid4={buvid4}",
    })

    # 第三步：拿 wbi key
    nav = s.get("https://api.bilibili.com/x/web-interface/nav", timeout=10).json()
    wbi = (nav.get("data") or {}).get("wbi_img") or {}
    img = wbi["img_url"].rsplit("/", 1)[-1].split(".")[0]
    sub = wbi["sub_url"].rsplit("/", 1)[-1].split(".")[0]

    time.sleep(1)

    # 第四步：唯一的目标请求（WBI 签名 + buvid3）
    mixin = "".join((img + sub)[i] for i in WBI_TAB)[:32]
    params = {
        "mid": mid, "ps": 10, "pn": 1, "tid": 0, "keyword": "",
        "order": "pubdate", "platform": "web", "web_location": 1550101,
        "order_avoided": "true", "wts": int(time.time()), "buvid3": buvid3,
    }
    params = dict(sorted(params.items()))
    params = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
    params["w_rid"] = hashlib.md5(
        (urllib.parse.urlencode(params) + mixin).encode()).hexdigest()

    resp = s.get("https://api.bilibili.com/x/space/wbi/arc/search",
                 params=params, timeout=10)
    print(f"[isolated mid={mid}] {short(resp)}")


if __name__ == "__main__":
    main()
