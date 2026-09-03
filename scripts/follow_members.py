"""让 BILIBILI_COOKIE 账号关注全部监控成员（一次性工具）。

背景：部分成员动态设为「仅粉丝可见」（如乃琳），抓取账号未关注时
feed/space 返回空列表，导致其突击直播/直播预约被静默漏抓。
本脚本用 BILIBILI_COOKIE 对应的账号批量关注 members.yaml 里的所有成员。

用法（先设置 BILIBILI_COOKIE 环境变量）::

    $env:BILIBILI_COOKIE = "SESSDATA=...; bili_jct=...; DedeUserID=..."
    python scripts/follow_members.py

也可手动：用该小号登录 B 站，搜索并关注下面 6 个账号即可，效果相同。

注意：本脚本会真实修改该 B 站账号的「关注」列表，请用小号执行。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

COOKIE = os.environ.get("BILIBILI_COOKIE", "")
if not COOKIE:
    print("缺少环境变量 BILIBILI_COOKIE（需 SESSDATA / bili_jct / DedeUserID）")
    sys.exit(1)


def csrf_from_cookie() -> str:
    for part in COOKIE.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "bili_jct":
            return v
    return ""


def main() -> int:
    config_path = Path(__file__).resolve().parent.parent / "config" / "members.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    members = config.get("members") or []
    if not members:
        print("members.yaml 无成员，退出")
        return 1

    csrf = csrf_from_cookie()
    if not csrf:
        print("Cookie 中缺少 bili_jct（无法关注），请重新复制完整 Cookie")
        return 1

    headers = {"User-Agent": "Mozilla/5.0", "Cookie": COOKIE, "Referer": "https://space.bilibili.com/"}

    ok = fail = 0
    for m in members:
        uid = str(m.get("uid", ""))
        name = m.get("name", uid)
        if not uid:
            continue
        # 先查关系状态：attribute 2=已关注 6=互关
        try:
            r = requests.get(
                "https://api.bilibili.com/x/relation",
                params={"fid": uid},
                headers=headers,
                timeout=10,
            )
            attr = ((r.json().get("data") or {}).get("relation") or {}).get("attribute", 0)
        except Exception as exc:
            print(f"[follow] {name} 查询关系失败: {exc}")
            fail += 1
            continue
        if attr in (2, 6):
            print(f"[follow] {name} ({uid}) 已关注，跳过")
            continue
        # 执行关注 act=1
        try:
            r = requests.post(
                "https://api.bilibili.com/x/relation/modify",
                data={"fid": uid, "act": 1, "re_src": 11, "csrf": csrf},
                headers=headers,
                timeout=10,
            )
            j = r.json()
            if j.get("code") == 0:
                print(f"[follow] 已关注 {name} ({uid})")
                ok += 1
            else:
                print(f"[follow] 关注 {name} 失败: code={j.get('code')} {j.get('message')}")
                fail += 1
        except Exception as exc:
            print(f"[follow] 关注 {name} 异常: {exc}")
            fail += 1

    print(f"[follow] 完成：成功 {ok}，失败 {fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
