"""周内已播突击回填 —— 追回漏抓的直播预约动态。

背景：成员动态 feed 在数据中心 IP 下 412 时通时断 + 个别成员动态设为
「仅粉丝可见」（如乃琳，需抓取账号已关注），预约动态常在发布当下漏抓；
而 flash 管道游标只处理「新动态」，漏掉的历史动态永远不会重扫 →
「已播突击」进不了周程表日历（2026-09-06 发现：乃琳 9/2 发布的
「接着聊聊今年生日会 09-04 22:30」预约动态从未被系统抓到）。

本脚本**不看游标**，定期扫描成员近期动态里的直播预约卡片
（MAJOR_TYPE_LIVE，自带确定开播时间），把满足以下条件的已播突击
并入当前周周程表（复用 schedule_flash 的判定/写入，幂等）：
- 开播时间落在当前周 latest.json 覆盖日期内
- 已开播（start_time <= 当前时间）
- 日历上同 (日期, 成员, 时间 ±10 分钟) 无已有事件（日程内直播不重复）
- source_dynamic_id 未在 data/merged_flash_ids.txt（去重）

对 feed 可达性 best-effort：feed 通就跑通、不通静默跳过（下轮再试）。
前置：BILIBILI_COOKIE 账号需已关注全部成员（scripts/follow_members.py），
否则「仅粉丝可见」成员（乃琳）的预约动态仍扫不到。

用法::

    python scripts/backfill_flash.py            # 正常（随录播回填 30min 管道）
    python scripts/backfill_flash.py --dry-run  # 只打印候选与将并入项，不写文件
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bili_session import build_session, get_json  # noqa: E402
from common import CST, MEMBERS_YAML  # noqa: E402
from flash_monitor import (  # noqa: E402
    DYNAMIC_API,
    REQUEST_GAP_SECONDS,
    parse_dynamic_item,
)
from flash_recognize import _LIVE_PLAN_RE  # noqa: E402

# 只回扫这些秒以内的动态（覆盖当前周并留周一边界余量）
SCAN_DAYS = 10
# 每个成员最多翻页数（feed/space 单页约 20 条）
MAX_PAGES = 2


def fetch_recent_dynamics(
    session, uid: str, scan_days: int = SCAN_DAYS, max_pages: int = MAX_PAGES
) -> list[dict]:
    """拉取某账号近期动态（不过滤游标，从新到旧），返回 parse 后的列表。

    feed 风控/为空时返回 []（静默，下一轮自动重试）。
    """
    out: list[dict] = []
    offset = ""
    for _ in range(max_pages):
        params = {"host_mid": uid}
        if offset:
            params["offset"] = offset
        data = get_json(
            session,
            DYNAMIC_API,
            params=params,
            referer=f"https://space.bilibili.com/{uid}/dynamic",
        )
        if data is None or data.get("code") != 0:
            if out:
                break  # 已有部分数据
            print(f"[backfill-flash] uid={uid} 动态接口不可达（风控/异常），跳过")
            return []
        items = ((data.get("data") or {}).get("items")) or []
        if not items:
            break
        for item in items:
            parsed = parse_dynamic_item(item, uid)
            if parsed is not None:
                out.append(parsed)
        oldest_ts = int(out[-1].get("pub_ts") or 0) if out else 0
        if oldest_ts and (time.time() - oldest_ts) > scan_days * 86400:
            break  # 已覆盖足够时间窗
        offset = str(items[-1].get("id_str") or "")
        if not offset:
            break
    return out


def _first_content_line(text: str) -> str:
    """取正文第一行作为标题候选（跳过注入的「直播预约时间」标记行）。"""
    for line in str(text or "").splitlines():
        if not line.startswith("直播预约时间"):
            return line.strip()
    return ""


def build_candidates(account: dict, dynamics: list[dict]) -> list[dict]:
    """从近期动态里挑出「直播预约卡片」候选（flash 事件形态，含确定性时间）。

    只认 MAJOR_TYPE_LIVE 且能解析出确定开播时间（live_plan_info.start_time
    或 live_start_time，parse_dynamic_item 已注入「直播预约时间: ...」标记）的；
    纯文本「今晚x点」类无法回溯归属到过去的日期，不在此处理。
    """
    member_key = account.get("member_key")
    if not member_key:
        return []
    events: list[dict] = []
    for d in dynamics:
        if d.get("type") != "MAJOR_TYPE_LIVE":
            continue
        m = _LIVE_PLAN_RE.search(d.get("text") or "")
        if not m:
            continue
        try:
            dt = datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)), tzinfo=CST,
            )
        except ValueError:
            continue
        title = re.sub(
            r"^突击[！!]?\s*", "", _first_content_line(d.get("text") or "")
        ).strip() or "突击直播"
        events.append(
            {
                "source_dynamic_id": str(d.get("dynamic_id") or ""),
                "member": member_key,
                "title": title[:50],
                "start_time": dt.isoformat(),
                "desc": (d.get("text") or "")[:200],
            }
        )
    return events


def run(dry_run: bool = False) -> int:
    """扫描全部成员近期预约动态并并入周程表；返回并入数量。"""
    if not MEMBERS_YAML.exists():
        print(f"[backfill-flash] 缺少配置文件: {MEMBERS_YAML}")
        return 0
    config = yaml.safe_load(MEMBERS_YAML.read_text(encoding="utf-8")) or {}
    members = [
        m for m in (config.get("members") or []) if m.get("member_key")
    ]
    if not members:
        print("[backfill-flash] members.yaml 无带 member_key 的成员，退出")
        return 0

    session = build_session()
    candidates: list[dict] = []
    for i, account in enumerate(members):
        uid = str(account.get("uid") or "")
        name = account.get("name", uid)
        dynamics = fetch_recent_dynamics(session, uid)
        events = build_candidates(account, dynamics)
        if events:
            for ev in events:
                print(
                    f"[backfill-flash] {name} 候选: {ev['start_time']} "
                    f"{ev['title']!r} sid={ev['source_dynamic_id']}"
                )
            candidates.extend(events)
        else:
            print(
                f"[backfill-flash] {name} 无直播预约卡片候选"
                f"（动态 {len(dynamics)} 条）"
            )
        if i < len(members) - 1:
            time.sleep(REQUEST_GAP_SECONDS)

    if not candidates:
        print("[backfill-flash] 无可并入候选")
        return 0

    if dry_run:
        from schedule_flash import _load_merged_ids, collect_inserts

        try:
            import json

            latest = json.loads(
                Path(MEMBERS_YAML).parent.parent.joinpath("data", "latest.json")
                .read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            print(f"[backfill-flash] 读取 latest.json 失败: {exc}")
            return 0
        inserts = collect_inserts(
            latest, candidates, _load_merged_ids(), now=datetime.now(CST)
        )
        if inserts:
            for date_s, ev, sid in inserts:
                print(
                    f"[backfill-flash][dry-run] 将并入 {date_s} "
                    f"{ev['time']} {ev['member']} {ev['title']!r} ({sid})"
                )
        else:
            print("[backfill-flash][dry-run] 无可并入项（已在日历/未开播/重复）")
        return len(inserts)

    from schedule_flash import merge_aired_flash_into_schedule

    merged = merge_aired_flash_into_schedule(candidates)
    print(f"[backfill-flash] 本轮并入 {merged} 场已播突击")
    return merged


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="周内已播突击回填（追回漏抓）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
