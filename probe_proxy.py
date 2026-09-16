"""反代探测：B 站接口「直连 vs Cloudflare Worker 出口」对比。

用途：判断 B 站是否拦截/放行 **Cloudflare 出口 IP**（典型表现：直连 200 的
匿名接口，经 Worker 反代后变成 412 风控页）。

必须在 GitHub Actions（海外 runner）上运行——与生产管道同环境；
本机（国内）无法验证 workers.dev（域名被墙）：

    BILI_PROXY_URL=https://xxx.workers.dev [BILI_PROXY_KEY=...] python probe_proxy.py

输出对比表：每次请求的状态码 / 耗时 / Server 头 / 是否风控 HTML。
"""
from __future__ import annotations

import os
import time
import urllib.parse

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 探测目标：从「匿名基本可用」到「最易风控」排序
TARGETS: list[tuple[str, str, dict[str, str]]] = [
    (
        "nav 登录态",
        "https://api.bilibili.com/x/web-interface/nav",
        {"Referer": "https://www.bilibili.com/"},
    ),
    (
        "spi 指纹",
        "https://api.bilibili.com/x/frontend/finger/spi",
        {},
    ),
    (
        "直播间状态 getRoomPlayInfo",
        "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
        "?room_id=22625027&protocol=0,1&format=0,1,2&codec=0,1&qn=0",
        {},
    ),
    (
        "动态 feed（最易风控）",
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?host_mid=672342685",
        {"Referer": "https://space.bilibili.com/672342685/dynamic"},
    ),
]


def probe(
    url: str,
    headers: dict[str, str],
    proxy_url: str | None = None,
    upstream: str | None = None,
    proxy_key: str | None = None,
    timeout: int = 15,
) -> dict:
    """请求一次；proxy_url + upstream 均存在时走 Worker 反代。"""
    h = {"User-Agent": UA, **headers}
    target = url
    if proxy_url and upstream:
        parsed = urllib.parse.urlsplit(url)
        target = f"{proxy_url}{parsed.path}"
        if parsed.query:
            target += f"?{parsed.query}"
        h["X-Bili-Upstream"] = upstream
        if proxy_key:
            h["X-Bili-Key"] = proxy_key

    started = time.time()
    try:
        resp = requests.get(target, headers=h, timeout=timeout, allow_redirects=False)
    except requests.RequestException as exc:
        return {"status": None, "ms": int((time.time() - started) * 1000), "error": str(exc)[:100]}

    body = resp.text[:150].replace("\n", " ")
    stripped = body.lstrip().lower()
    return {
        "status": resp.status_code,
        "ms": int((time.time() - started) * 1000),
        "server": resp.headers.get("Server", "-"),
        "cf_ray": resp.headers.get("CF-RAY", "-"),
        "html": stripped.startswith(("<!doctype", "<html")),
        "body": body,
    }


def main() -> int:
    proxy_url = (os.environ.get("BILI_PROXY_URL") or "").rstrip("/") or None
    proxy_key = os.environ.get("BILI_PROXY_KEY") or None

    # runner 直连出口 IP（便于记录对比环境）
    try:
        ip = requests.get("https://api.ipify.org", timeout=10).text.strip()
    except requests.RequestException:
        ip = "?"
    print(f"[probe] runner 直连出口 IP: {ip}")
    print(f"[probe] BILI_PROXY_URL: {'已配置' if proxy_url else '未配置（跳过反代对比）'}")
    print()

    for name, url, headers in TARGETS:
        upstream = urllib.parse.urlsplit(url).netloc.lower()
        direct = probe(url, headers)
        via = probe(url, headers, proxy_url, upstream, proxy_key) if proxy_url else None

        print(f"===== {name} =====")
        print(f"  直连: {fmt(direct)}")
        if via:
            print(f"  反代: {fmt(via)}")
            if direct.get("status") == 200 and via.get("status") != 200:
                print("  ⚠️ 直连正常但反代异常 → 该 Cloudflare 出口可能被 B 站风控")
        print()

    print("[probe] 说明：反代请求经 Cloudflare 网络转发，其出口 IP 属 AS13335；")
    print("         若多个接口在反代下集体 412/HTML，则 B 站对 Cloudflare 出口存在拦截。")
    return 0


def fmt(r: dict) -> str:
    if r.get("status") is None:
        return f"请求失败 {r.get('error', '')} ({r['ms']}ms)"
    kind = "HTML风控页" if r.get("html") else "JSON/其他"
    return (
        f"HTTP {r['status']} ({r['ms']}ms) Server={r.get('server')} "
        f"CF-RAY={r.get('cf_ray')} {kind} | {r.get('body', '')[:80]}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
