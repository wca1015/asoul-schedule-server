"""验证 Cloudflare 反代 Worker 是否可用（在 GitHub Actions 海外环境执行）。

对比「直连 B 站」 vs 「走 BILI_PROXY_URL 反代」访问 nav / 动态接口，
确认 BILI_PROXY_URL 配置的 Worker 是否真正转发成功。
"""
from __future__ import annotations

import os

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

proxy = os.environ.get("BILI_PROXY_URL", "").strip().rstrip("/")
if not proxy:
    print("[proxy] 未配置 BILI_PROXY_URL，跳过")
    raise SystemExit(0)

# 只打印 host（secret 值不完整外泄）
host = proxy.split("//", 1)[-1]
print(f"[proxy] BILI_PROXY_URL host = {host}")


def fetch(label: str, url: str, headers: dict) -> None:
    try:
        r = requests.get(url, headers=headers, timeout=20)
        ct = r.headers.get("Content-Type", "")
        code = r.json().get("code") if "json" in ct else None
        print(
            f"[proxy] {label}: HTTP {r.status_code} code={code} "
            f"len={len(r.content)} [{ct[:30]}]"
        )
        if r.status_code >= 400:
            print(f"        body={r.text[:200]}")
    except Exception as e:
        print(f"[proxy] {label}: FAIL {type(e).__name__} {str(e)[:120]}")


H = {"User-Agent": UA}
# 直连基线
fetch("直连 nav", "https://api.bilibili.com/x/web-interface/nav", H)
# 走反代（worker 按 X-Bili-Upstream 转发，缺省 api.bilibili.com）
fetch("反代 nav", proxy + "/x/web-interface/nav",
      dict(H, **{"X-Bili-Upstream": "api.bilibili.com"}))
fetch("反代 动态(心宜)", proxy + "/x/polymer/web-dynamic/v1/feed/space?host_mid=3537115310721181",
      dict(H, Referer="https://space.bilibili.com/", **{"X-Bili-Upstream": "api.bilibili.com"}))
