"""临时脚本：为过去 4 个往日周构造归档（含真实录播）。

背景：服务端只有本周（08-24）真实数据，往前的周要么空白、要么是早期手工
构造的演示数据（日期/标题对不上）。本脚本从各录播组账号抓取真实投稿，
按视频标题中的场次日期筛选出落在过去 4 个往日周的录播，据此构造
日期与标题都准确对应的周程表归档。

与 recording_backfill 共用抓取/匹配函数（WBI 签名、防风控、时长过滤）。
仅构造「有真实录播」的事件；无录播的天留空，保证展示数据全部可溯源。

用法（GitHub Actions，本地网络会被风控）::

    python scripts/build_past_weeks.py
    python scripts/build_past_weeks.py --dry-run   # 只打印，不写文件
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

from common import ARCHIVE_DIR, CST, MEMBERS_YAML, ensure_dirs
from publish import next_version
from recording_backfill import (
    _build_session,
    fetch_space_videos,
    fetch_wbi_keys,
    normalize_videos,
    video_title_date,
)

# 过去 4 个往日周的周一日期（客户端可回看范围）
PAST_WEEKS = ["2026-07-27", "2026-08-03", "2026-08-10", "2026-08-17"]

WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# member key -> 展示名
MEMBER_NAME = {
    "bella": "贝拉", "jiaran": "嘉然", "nailin": "乃琳",
    "xinyi": "心宜", "sinuo": "思诺",
}


def week_start_of(d) -> str:
    """返回日期所在周的周一（YYYY-MM-DD）。"""
    from datetime import timedelta
    monday = d - timedelta(days=d.weekday())
    return monday.strftime("%Y-%m-%d")


def clean_title(raw: str) -> str:
    """从录播标题中提取直播标题主体。

    形如「【A-SOUL】嘉然 2026.8.27 然来唱然来跳【直播录像】」，
    去掉成员前缀、日期、【…】包裹后取主体；失败则回退到原标题。
    """
    import re
    t = raw.strip()
    # 去掉首尾的【…】块
    t = re.sub(r"^【[^】]*】\s*", "", t)
    t = re.sub(r"\s*【[^】]*】$", "", t)
    # 去掉开头的成员名
    for name in list(MEMBER_NAME.values()):
        t = re.sub(rf"^{name}\s*", "", t)
    # 去掉日期（含 2026.8.27 / 2026-08-27 / 8.27 等）
    t = re.sub(r"\d{4}\s*[./\-]\s*\d{1,2}\s*[./\-]\s*\d{1,2}", "", t)
    t = re.sub(r"\b\d{1,2}\.\d{1,2}\b", "", t)
    # 清理多余空白
    t = re.sub(r"\s+", " ", t).strip(" ~～-—·")
    return t or raw.strip()


def guess_member_from_title(raw: str) -> str | None:
    """从录播标题推断单人直播成员（团播组账号也代传单人录播）。

    标题形如「【A-SOUL】嘉然 2026.8.1 …」，成员名通常在第一个【】块之后。
    命中返回 member_key；团播/多人企划/无法判断返回 None。
    """
    for key, name in MEMBER_NAME.items():
        if name in raw:
            return key
    return None


def run(dry_run: bool = False) -> int:
    if not MEMBERS_YAML.exists():
        print(f"[past-weeks] 缺少配置文件: {MEMBERS_YAML}")
        return 1
    with open(MEMBERS_YAML, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    rec_cfg = config.get("recording") or {}

    session = _build_session()
    keys = fetch_wbi_keys(session)
    if keys is None:
        print("[past-weeks] 无法获取 WBI 签名，退出")
        return 1
    img_key, sub_key = keys

    # 录播组账号 -> 归属（member_key 或 group_type）
    accounts = []
    for account in rec_cfg.get("accounts") or []:
        uid = str(account.get("uid") or "")
        if not uid:
            continue
        accounts.append({
            "uid": uid,
            "name": account.get("name", uid),
            "member_key": account.get("member_key"),
            "group_type": account.get("group_type"),
        })

    # 抓取全部投稿（风控间歇性：空结果时等待后重试，最多 3 次）
    videos: list[dict] = []
    for account in accounts:
        raw: list[dict] = []
        for attempt in range(1, 4):
            raw = fetch_space_videos(session, account["uid"], img_key, sub_key, rec_cfg)
            if raw:
                break
            wait = 30 * attempt
            print(f"[past-weeks] {account['name']} 第 {attempt} 次拉取为空（疑似风控），等待 {wait}s 重试")
            import time as _time
            _time.sleep(wait)
        for video in normalize_videos(raw, None):
            if account["member_key"]:
                video["member_key"] = account["member_key"]
            elif account["group_type"]:
                video["group_type"] = account["group_type"]
            videos.append(video)
        print(f"[past-weeks] {account['name']}: 拉取 {len(raw)} 个投稿")
        import time as _time
        _time.sleep(3)

    print(f"[past-weeks] 共 {len(videos)} 个投稿")

    # 按场次日期分桶到 4 个往日周
    by_week: dict[str, list[dict]] = {w: [] for w in PAST_WEEKS}
    for video in videos:
        vdate = video_title_date(video.get("title", ""))
        if vdate is None:
            continue
        ws = week_start_of(vdate)
        if ws in by_week:
            video["_event_date"] = vdate
            by_week[ws].append(video)

    for w in PAST_WEEKS:
        print(f"[past-weeks] {w}: 命中 {len(by_week[w])} 条录播")

    if dry_run:
        for w in PAST_WEEKS:
            for v in by_week[w]:
                print(f"  {w} {v['_event_date']} {v.get('member_key') or v.get('group_type')} {v['bvid']} {v['title']}")
        return 0

    # 构造归档
    ensure_dirs()
    now = datetime.now(CST)
    for w in PAST_WEEKS:
        # 本周未抓到任何录播（疑似风控）：保留已有归档，不用空数据覆盖
        if not by_week[w]:
            print(f"[past-weeks] {w}: 本轮无命中，保留已有归档（若存在）")
            continue
        events_by_day: dict[str, list[dict]] = {}
        for video in sorted(by_week[w], key=lambda v: (v["_event_date"], v["created"])):
            date_str = video["_event_date"].strftime("%Y-%m-%d")
            # 成员归属优先级：专属录播号 > 标题中的单人成员名 > 团播分组。
            # 团播组账号也代传单人录播（如「【A-SOUL】嘉然 2026.8.1 …」），
            # 标题能识别出单人成员时归为单播，否则才归团播。
            member = video.get("member_key") or guess_member_from_title(video.get("title", ""))
            group = video.get("group_type")
            # 估算开播时间：录播发布时间 - 时长 - 30 分钟
            from datetime import timedelta
            est = datetime.fromtimestamp(video["created"] - int(video["length_minutes"] * 60) - 1800, tz=CST)
            # 若估算跨天，用场次日期当天修正
            time_str = est.strftime("%H:%M")
            if est.date() != video["_event_date"]:
                time_str = "20:00"
            title = clean_title(video["title"])
            if member:
                event = {
                    "time": time_str,
                    "member": member,
                    "title": title,
                    "desc": "",
                    "tag": "live",
                    "group_type": "none",
                    "format": "normal",
                    "recording_bvid": video["bvid"],
                }
            else:
                event = {
                    "time": time_str,
                    "member": "unknown",
                    "title": title,
                    "desc": "",
                    "tag": "show",
                    "group_type": group or "asoul",
                    "format": "normal",
                    "recording_bvid": video["bvid"],
                }
            events_by_day.setdefault(date_str, []).append(event)

        # 组装 7 天
        from datetime import date, timedelta as td
        monday = date.fromisoformat(w)
        days = []
        for i in range(7):
            d = monday + td(days=i)
            ds = d.strftime("%Y-%m-%d")
            days.append({
                "date": ds,
                "weekday": WEEKDAY_CN[d.weekday()],
                "events": sorted(events_by_day.get(ds, []), key=lambda e: e["time"]),
            })

        archive = {
            "week_start": w,
            "week_end": (monday + td(days=6)).strftime("%Y-%m-%d"),
            "days": days,
            "version": next_version(0, now),
            "updated_at": now.isoformat(),
            "source": "manual",
        }
        out = ARCHIVE_DIR / f"{w}.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(archive, f, ensure_ascii=False, indent=2)
        n_events = sum(len(d["events"]) for d in days)
        print(f"[past-weeks] 已写入 {out.name}: {n_events} 条事件")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="构造过去4周往日周归档")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = parser.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
