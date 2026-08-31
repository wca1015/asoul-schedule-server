"""管道A：B站官号动态抓取。

抓取官号最新一条图文动态（周程表图片）。

防风控策略（统一封装在 bili_session）：
- 携带真实浏览器指纹头；配置 BILIBILI_COOKIE 则使用登录 Cookie
- 可选 BILI_PROXY_URL：请求改道 Cloudflare Worker 反代（换出口 IP）
- 请求间隔 ≥ 30 分钟（由 GitHub Actions Cron 控制）
- 遇 412/风控响应，静默跳过，下一轮重试
- Cookie 失效（code=-101）自动飞书告警
"""
from __future__ import annotations

from bili_session import build_session, get_json
from common import DATA_DIR, LAST_DYNAMIC_ID_FILE, get_env

DYNAMIC_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"


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

    session = build_session()
    data = get_json(
        session,
        DYNAMIC_API,
        params={"host_mid": uid},
        referer=f"https://space.bilibili.com/{uid}/dynamic",
    )
    if data is None:
        # 网络失败 / 风控响应：静默跳过，下一轮重试
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
