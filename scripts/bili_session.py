"""B 站会话统一封装（防风控），供三条数据管道共用。

集中处理五件事：
1. 浏览器指纹请求头（UA / Referer / Accept / Sec-Fetch 等）
2. 匿名设备指纹：自动生成 buvid3/buvid4；配置了 BILIBILI_COOKIE 则改用登录态
3. 可选 Cloudflare Worker 反代（BILI_PROXY_URL）：把请求改道到 Worker，
   用 Worker 的出口 IP 规避 GitHub Actions 共享 IP 触发的高频风控（412）
4. WBI 签名密钥（文件缓存 + TTL，减少 nav 请求次数）
5. Cookie 失效检测：配置了 BILIBILI_COOKIE 却返回 code=-101（未登录）时，
   自动飞书告警（文件节流，避免每轮刷屏）

环境变量：
- BILIBILI_COOKIE  登录 Cookie（推荐配置；缺失则走匿名 buvid 路线）
- BILI_PROXY_URL   Cloudflare Worker 反代地址（可选，如
                   https://bili-proxy.xxx.workers.dev）
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse

import requests

from common import DATA_DIR
from notify import send_alert

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

SPI_API = "https://api.bilibili.com/x/frontend/finger/spi"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"

# 允许走反代的上游主机（白名单，防止 Worker 被当开放代理滥用）
PROXY_ALLOWED_HOSTS = (
    "api.bilibili.com",
    "api.live.bilibili.com",
    "api.vc.bilibili.com",
)

# B 站 WBI 签名固定混淆排列表（公开算法）
WBI_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# WBI key 文件缓存（跨 Actions 轮次复用，减少 nav 请求）
WBI_CACHE_FILE = DATA_DIR / "wbi_keys.json"
WBI_CACHE_TTL_SECONDS = 3600  # 1 小时

# Cookie 失效 / 风控告警节流
COOKIE_ALERT_FILE = DATA_DIR / "alert_cookie_last.txt"
ALERT_MIN_GAP_SECONDS = 6 * 3600  # 6 小时内最多告警一次


def _proxy_url() -> str | None:
    return os.environ.get("BILI_PROXY_URL") or None


def _cookie() -> str | None:
    return os.environ.get("BILIBILI_COOKIE") or None


def get_buvid() -> tuple[str, str]:
    """匿名设备指纹：spi 接口拿 buvid3/buvid4（无需登录）。"""
    try:
        r = requests.get(SPI_API, headers={"User-Agent": UA}, timeout=10)
        d = r.json().get("data", {})
        return d.get("b_3", ""), d.get("b_4", "")
    except (requests.RequestException, ValueError):
        return "", ""


def build_session() -> requests.Session:
    """构建带浏览器指纹的会话。

    - 配置了 BILIBILI_COOKIE：直接用登录 Cookie（最稳）
    - 否则：匿名 + 自动生成 buvid3/buvid4（降低裸请求风控概率）
    会话上挂 buvid3 属性，供 WBI 签名使用（新版签名规则）。
    """
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    s.headers["Referer"] = "https://www.bilibili.com/"
    cookie = _cookie()
    if cookie:
        s.headers["Cookie"] = cookie
        s.buvid3 = ""
    else:
        buvid3, buvid4 = get_buvid()
        s.buvid3 = buvid3
        if buvid3:
            s.headers["Cookie"] = f"buvid3={buvid3}; buvid4={buvid4}"
    return s


def _proxied(url: str) -> tuple[str, str | None]:
    """按 BILI_PROXY_URL 改写 URL；返回 (目标URL, 上游Host 或 None)。

    仅白名单内的 api.* 主机走反代，其余 URL 原样返回。
    """
    proxy = _proxy_url()
    if not proxy:
        return url, None
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.lower()
    if host not in PROXY_ALLOWED_HOSTS:
        return url, None
    target = f"{proxy}{parsed.path}"
    if parsed.query:
        target += f"?{parsed.query}"
    return target, host


def get_json(
    session: requests.Session,
    url: str,
    params: dict | None = None,
    referer: str | None = None,
    timeout: int = 10,
) -> dict | None:
    """GET 一个 B 站 JSON 接口（统一处理风控/代理/Cookie 失效）。

    返回解析后的 JSON dict；网络失败 / 非 JSON / HTTP 412/403 返回 None，
    调用方静默跳过本轮即可（与既有「风控静默跳过」策略一致）。
    """
    target, upstream = _proxied(url)
    headers: dict[str, str] = {}
    if upstream:
        headers["X-Bili-Upstream"] = upstream
    if referer:
        headers["Referer"] = referer
    try:
        resp = session.get(target, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        print(f"[bili] 请求失败 {url}: {exc}")
        return None
    if resp.status_code in (412, 403):
        print(f"[bili] 触发风控 (HTTP {resp.status_code})：{url}")
        _maybe_alert_http_risk(resp.status_code)
        return None
    try:
        data = resp.json()
    except ValueError:
        print(f"[bili] 非 JSON 响应 (HTTP {resp.status_code})：{url}")
        return None
    _maybe_alert_cookie_expired(data)
    return data


def _maybe_alert_cookie_expired(data: dict) -> None:
    """配置了 Cookie 却返回 -101（未登录）：Cookie 失效，飞书告警（节流）。"""
    if not _cookie():
        return
    if data.get("code") == -101:
        if _throttle_ok(COOKIE_ALERT_FILE, ALERT_MIN_GAP_SECONDS):
            send_alert(
                "BILIBILI_COOKIE 可能已失效",
                "接口返回 code=-101（未登录），说明配置的 BILIBILI_COOKIE 已过期。\n"
                "请重新登录 B 站复制 Cookie，更新 GitHub Actions Secret；\n"
                "否则将回退到匿名请求（易触发风控）。",
            )


def _maybe_alert_http_risk(status_code: int) -> None:
    """HTTP 412/403：若配置了 Cookie 仍被风控，说明出口 IP 异常，提示换反代。"""
    if not _cookie():
        return  # 匿名态 412 属预期，不打扰
    if _throttle_ok(COOKIE_ALERT_FILE, ALERT_MIN_GAP_SECONDS):
        send_alert(
            f"B 站接口被风控（HTTP {status_code}）",
            "即使配置了 BILIBILI_COOKIE 仍被 412/403 拦截，"
            "多半是出口 IP 信誉差（如 GitHub Actions 共享 IP）。\n"
            "建议部署 Cloudflare Worker 反代并设置 BILI_PROXY_URL。",
        )


def _throttle_ok(marker_file, min_gap: int) -> bool:
    """文件节流：距上次告警超过 min_gap 秒才允许再次告警（跨进程生效）。"""
    try:
        if marker_file.exists():
            last = float(marker_file.read_text().strip() or "0")
            if time.time() - last < min_gap:
                return False
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        marker_file.write_text(str(int(time.time())))
        return True
    except (OSError, ValueError):
        return True  # 文件异常不阻断告警


def alert_once(
    key: str,
    title: str,
    detail: str,
    min_gap: int = ALERT_MIN_GAP_SECONDS,
) -> None:
    """按 key 节流的飞书告警（同一 key 在 min_gap 秒内最多一次）。

    key 用于写节流标记文件（如 ``feed_empty_672342685``），跨 Actions 轮次生效，
    避免同一问题每轮刷屏。
    """
    if _throttle_ok(DATA_DIR / f"alert_{key}.txt", min_gap):
        send_alert(title, detail)


# ---------------------------------------------------------------------------
# WBI 签名
# ---------------------------------------------------------------------------

def _mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in WBI_MIXIN_KEY_ENC_TAB)[:32]


def sign_wbi(params: dict, img_key: str, sub_key: str, buvid3: str = "") -> dict:
    """B 站 WBI 签名：追加 wts → 字典序排序 → 过滤特殊字符 → md5 得 w_rid。

    buvid3 可选：新版签名规则要求 buvid3 参与排序签名（匿名态更稳，
    与 probe3 验证过的一致）。
    """
    mixin = _mixin_key(img_key + sub_key)
    params = dict(params)
    params["wts"] = int(time.time())
    if buvid3:
        params["buvid3"] = buvid3
    params = dict(sorted(params.items()))
    params = {
        k: "".join(ch for ch in str(v) if ch not in "!'()*")
        for k, v in params.items()
    }
    params["w_rid"] = hashlib.md5(
        (urllib.parse.urlencode(params) + mixin).encode()
    ).hexdigest()
    return params


def fetch_wbi_keys(
    session: requests.Session, force: bool = False
) -> tuple[str, str] | None:
    """从 nav 接口获取 WBI 签名密钥 (img_key, sub_key)，带文件缓存。

    未登录时 nav 返回 code=-101，但 wbi_img 依然可用。
    风控/失败返回 None，调用方静默跳过本轮。
    """
    cached = _load_wbi_cache()
    if cached and not force and time.time() - cached["ts"] < WBI_CACHE_TTL_SECONDS:
        return cached["img"], cached["sub"]

    data = get_json(session, NAV_API)
    if data is None:
        return None
    wbi_img = (data.get("data") or {}).get("wbi_img") or {}
    img_url, sub_url = wbi_img.get("img_url", ""), wbi_img.get("sub_url", "")
    if not img_url or not sub_url:
        print("[bili] nav 响应缺少 wbi_img")
        return None
    img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
    _save_wbi_cache(img_key, sub_key)
    return img_key, sub_key


def invalidate_wbi_cache() -> None:
    """WBI 签名返回 -352 时调用，强制下一轮重取 key（key 可能已轮换）。"""
    try:
        WBI_CACHE_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _load_wbi_cache() -> dict | None:
    try:
        if WBI_CACHE_FILE.exists():
            return json.loads(WBI_CACHE_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        pass
    return None


def _save_wbi_cache(img_key: str, sub_key: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        WBI_CACHE_FILE.write_text(
            json.dumps({"ts": int(time.time()), "img": img_key, "sub": sub_key}),
            encoding="utf-8",
        )
    except OSError:
        pass
