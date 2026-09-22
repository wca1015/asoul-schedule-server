"""一次性诊断脚本：复现「突击直播识别异常」告警（只读）。

背景：2026-09-22 12:46 / 14:21 的 flash 管道在"游标被污染导致 feed 全列表
重扫"时，对每一条动态都抛出了「突击直播识别异常」告警（每条动态一条飞书），
但日志里没有任何识别阶段的 print 输出。本脚本用与生产完全一致的请求
（web 版 feed 接口 + 生产 Cookie）拉取各成员动态，逐条跑 parse_dynamic_item
+ recognize_flash 并打印完整 traceback，用于定位异常根因。

本脚本只读：不写任何 data/ 文件、不更新游标、不发送任何通知。
（workflow 也不注入 FEISHU_WEBHOOK，双重保险。）
"""
from __future__ import annotations

import json
import sys
import traceback

import yaml

sys.path.insert(0, "scripts")

from bili_session import build_session, get_json  # noqa: E402
from flash_monitor import DYNAMIC_API, parse_dynamic_item  # noqa: E402
from flash_recognize import recognize_flash  # noqa: E402

TARGET_UIDS = [
    "3537115310721181",  # 心宜
    "672353429",         # 思诺
    "672328094",         # 贝拉
    "672342685",         # 嘉然
    "3537115310721781",  # 乃琳
    "3493085336046382",  # 官号
]


def main() -> None:
    with open("config/members.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    accounts = {str(m.get("uid")): m for m in config.get("members") or []}

    session = build_session()

    for uid in TARGET_UIDS:
        account = accounts.get(uid) or {"uid": uid, "member_key": "unknown"}
        print(f"\n##### uid={uid} name={account.get('name')!r} account={account!r}", flush=True)
        data = get_json(
            session,
            DYNAMIC_API,
            params={"host_mid": uid},
            referer=f"https://space.bilibili.com/{uid}/dynamic",
        )
        if data is None:
            print("  请求失败（None）", flush=True)
            continue
        if data.get("code") != 0:
            print(f"  接口异常 code={data.get('code')} msg={data.get('message')}", flush=True)
            continue

        items = (data.get("data") or {}).get("items") or []
        print(f"  items={len(items)}", flush=True)
        for it in items:
            did = it.get("id_str")
            modules = it.get("modules")
            print(f"  [{did}] modules 类型={type(modules).__name__}", flush=True)
            try:
                parsed = parse_dynamic_item(it, uid)
            except Exception:
                print(f"  [{did}] parse_dynamic_item 抛异常!!!", flush=True)
                traceback.print_exc()
                continue
            if parsed is None:
                print(f"  [{did}] parse 返回 None", flush=True)
                continue
            print(
                f"  [{did}] parsed: pub_ts={parsed.get('pub_ts')!r} "
                f"type={parsed.get('type')!r} "
                f"text={(parsed.get('text') or '')[:50]!r} "
                f"images={len(parsed.get('images') or [])}",
                flush=True,
            )
            try:
                event = recognize_flash(parsed, account, config)
                print(f"  [{did}] recognize_flash -> {'事件' if event else 'None'}", flush=True)
                if event:
                    print("      " + json.dumps(event, ensure_ascii=False)[:300], flush=True)
            except Exception:
                print(f"  [{did}] !!!!!! recognize_flash 抛异常 !!!!!!", flush=True)
                traceback.print_exc()
                sys.stdout.flush()

    print("\n[done]", flush=True)


if __name__ == "__main__":
    main()
