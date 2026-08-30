"""管道C：录播回填（直播结束后把回放 BV 号回填进 latest.json / 往日周归档）。

数据流：
  直播结束 → 成员/录播组把录像上传 B 站（可能滞后数小时至数天）
  → 本脚本定时扫描成员空间投稿（x/space/wbi/arc/search，WBI 签名）
  → 按「成员 + 时间窗」匹配到完整回放（视频时长过滤掉切片）
  → 回填 latest.json 与最近 N 周归档中对应 event 的 recording_bvid，版本号递增
  → 工作流提交 + 同步 OSS（含 week/{week}.json 往日周端点）
  → 客户端轮询 / 往日周拉取拿到新版本 → 已结束直播显示「录像」标签

匹配规则（均可在 config/members.yaml 的 recording 块配置）：
- 视频上传时间 ∈ [事件时间 - before_minutes, 事件时间 + window_hours]
- 视频时长 ≥ min_length_minutes（短于此视为切片而非完整回放）
- 单播：匹配成员本人投稿 + 专属录播号（accounts[].member_key）投稿；
  团播（member=unknown）：匹配团播录播号（accounts[].group_type）投稿
- 混合账号可按标题 title_map 关键词推断归属成员；
  候选排序：标题相关且日期吻合者优先，其次上传时间最接近开播者；
  视频标题中的日期与事件日期对不上且无标题关联时拒绝匹配（防错配），
  已选中的视频移出候选池（一段录播只归属一场直播）

防风控策略与管道 A/B 一致：真实 UA/Referer、串行请求 + 间隔、
412/403/风控码静默跳过，下一轮重试。

用法::

    python scripts/recording_backfill.py            # 正常运行（GitHub Actions cron）
    python scripts/recording_backfill.py --dry-run  # 只打印匹配结果，不写文件
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests
import yaml

from common import ARCHIVE_DIR, CST, LATEST_JSON, MEMBERS_YAML, ensure_dirs
from notify import send_alert
from publish import next_version

NAV_API = "https://api.bilibili.com/x/web-interface/nav"
SPACE_SEARCH_API = "https://api.bilibili.com/x/space/wbi/arc/search"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://space.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}

# B 站 WBI 签名固定混淆排列表（公开算法）
WBI_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# recording 配置默认值（members.yaml 的 recording 块可逐项覆盖）
DEFAULT_RECORDING_CONFIG = {
    "enabled": True,
    "window_hours": 72,        # 事件时间之后的时间窗（小时）
    "before_minutes": 30,      # 允许上传时间早于事件时间的分钟数（定时投稿）
    "min_length_minutes": 30,  # 短于此时长视为切片，不是完整回放
    "page_size": 30,           # 投稿列表每页条数（接口上限）
    "max_pages": 2,            # 每个账号最多扫描前 N 页
    "request_gap_seconds": 2,  # 账号之间的请求间隔
    "archive_weeks": 4,        # 回填范围另含最近 N 个往日周归档（录播晚传数天不丢失）
    "accounts": [],            # 额外录播组账号：[{uid, name, member_key=专属 | group_type=团播 | title_map=推断}]
}


# ---------------------------------------------------------------------------
# B 站接口（WBI 签名 + 防风控）
# ---------------------------------------------------------------------------

def _build_session() -> requests.Session:
    session = requests.Session()
    headers = dict(HEADERS)
    cookie = os.environ.get("BILIBILI_COOKIE")
    if cookie:
        headers["Cookie"] = cookie
    session.headers.update(headers)
    return session


def fetch_wbi_keys(session: requests.Session) -> tuple[str, str] | None:
    """从 nav 接口获取 WBI 签名密钥 (img_key, sub_key)。

    未登录时 nav 返回 code=-101，但 wbi_img 依然存在可用。
    风控/失败返回 None，调用方静默跳过本轮。
    """
    try:
        resp = session.get(NAV_API, timeout=10)
        if resp.status_code in (412, 403):
            print(f"[recording] nav 触发风控 (HTTP {resp.status_code})，本轮跳过")
            return None
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[recording] nav 请求失败: {exc}，本轮跳过")
        return None

    wbi_img = (data.get("data") or {}).get("wbi_img") or {}
    img_url, sub_url = wbi_img.get("img_url", ""), wbi_img.get("sub_url", "")
    if not img_url or not sub_url:
        print("[recording] nav 响应缺少 wbi_img，本轮跳过")
        return None
    img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
    return img_key, sub_key


def _mixin_key(orig: str) -> str:
    """按固定排列表重排密钥并截取 32 位。"""
    return "".join(orig[i] for i in WBI_MIXIN_KEY_ENC_TAB)[:32]


def sign_wbi_params(params: dict, img_key: str, sub_key: str) -> dict:
    """B 站 WBI 签名：追加 wts → 按字典序排序 → 过滤特殊字符 → md5 得 w_rid。"""
    mixin_key = _mixin_key(img_key + sub_key)
    params = dict(params)
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    params = {
        k: "".join(ch for ch in str(v) if ch not in "!'()*")
        for k, v in params.items()
    }
    query = urllib.parse.urlencode(params)
    params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return params


def fetch_space_videos(
    session: requests.Session,
    uid: str,
    img_key: str,
    sub_key: str,
    rec_cfg: dict,
) -> list[dict]:
    """拉取账号投稿视频列表前几页（按发布时间倒序），返回原始 vlist。

    风控/接口异常时提前终止，返回已拿到的部分（静默降级，下一轮重试）。
    """
    page_size = int(rec_cfg.get("page_size", 30))
    max_pages = int(rec_cfg.get("max_pages", 2))
    gap = rec_cfg.get("request_gap_seconds", 2)

    raw_videos: list[dict] = []
    for pn in range(1, max_pages + 1):
        params = sign_wbi_params(
            {
                "mid": uid,
                "ps": page_size,
                "pn": pn,
                "tid": 0,
                "keyword": "",
                "order": "pubdate",
                "platform": "web",
                "web_location": 1550101,
                "order_avoided": "true",
            },
            img_key,
            sub_key,
        )
        try:
            resp = session.get(SPACE_SEARCH_API, params=params, timeout=10)
            if resp.status_code in (412, 403):
                print(f"[recording] uid={uid} 触发风控 (HTTP {resp.status_code})，停止扫描该账号")
                break
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"[recording] uid={uid} 请求失败: {exc}，停止扫描该账号")
            break

        if data.get("code") != 0:
            print(
                f"[recording] uid={uid} 接口异常: "
                f"code={data.get('code')} message={data.get('message')}"
            )
            break

        vlist = ((data.get("data") or {}).get("list") or {}).get("vlist") or []
        raw_videos.extend(vlist)
        if len(vlist) < page_size:
            break
        time.sleep(gap)

    return raw_videos


# ---------------------------------------------------------------------------
# 匹配逻辑（纯函数，便于离线测试）
# ---------------------------------------------------------------------------

def parse_length_minutes(length) -> float:
    """B 站视频时长形如 "M:SS" / "H:MM:SS"，解析为分钟；无法解析返回 0。"""
    parts = str(length or "").strip().split(":")
    if not parts or not all(p.isdigit() for p in parts):
        return 0.0
    nums = [int(p) for p in parts]
    if len(nums) == 1:
        return float(nums[0])
    if len(nums) == 2:
        return nums[0] + nums[1] / 60.0
    if len(nums) == 3:
        return nums[0] * 60 + nums[1] + nums[2] / 60.0
    return 0.0


def normalize_videos(raw_list: list[dict], member_key: str | None) -> list[dict]:
    """把接口 vlist 规整为内部结构；member_key 为 None 时待调用方推断。"""
    videos = []
    for v in raw_list:
        bvid, created = v.get("bvid"), v.get("created")
        if not bvid or created is None:
            continue
        videos.append(
            {
                "member_key": member_key,
                "bvid": bvid,
                "created": int(created),
                "title": v.get("title", "") or "",
                "length_minutes": parse_length_minutes(v.get("length")),
            }
        )
    return videos


def parse_event_dt(date_str: str | None, time_str: str | None) -> datetime | None:
    """周程表事件的开播时间（CST）；格式非法返回 None。"""
    try:
        return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(
            tzinfo=CST
        )
    except (TypeError, ValueError):
        return None


def _title_hit(event: dict, video: dict) -> bool:
    """视频标题与直播标题是否相关（忽略空白与大小写的包含关系）。"""
    et = "".join(str(event.get("title", "")).split()).lower()
    vt = "".join(str(video.get("title", "")).split()).lower()
    return bool(et) and (et in vt or vt in et)


# 录播组标题中的日期常见写法：2026.08.24 / 2026.8.27 / 2026-08-24 / 2026年8月27日
_VIDEO_DATE_RE = re.compile(r"(\d{4})\s*[.\-/年]\s*(\d{1,2})\s*[.\-/月]\s*(\d{1,2})")


def video_title_date(title: str):
    """从视频标题提取直播日期（录播组标题通常注明场次日期）；无法解析返回 None。"""
    m = _VIDEO_DATE_RE.search(title or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=CST).date()
    except ValueError:
        return None


def _match_score(event: dict, event_dt: datetime, video: dict):
    """候选评分：返回 (rank, 时间差)，rank 越小越优先；应拒绝的候选返回 None。

    防错配规则：
    - 标题相关 + 日期一致（或标题无日期）：最优 / 次优
    - 标题相关 + 日期相差 1 天：允许（容忍周程表 OCR 或录播标注偏差一天）
    - 标题相关 + 日期相差 ≥2 天：拒绝（大概率是另一周的同名企划）
    - 标题无关 + 日期一致：兜底
    - 标题无关 + 标题无日期：最弱兜底（兼容无日期标题的投稿）
    - 标题无关 + 日期不一致：拒绝（防止仅凭时间就近把无关录播错配过来）
    """
    title_hit = _title_hit(event, video)
    vdate = video_title_date(video.get("title", ""))
    date_diff = None if vdate is None else abs((vdate - event_dt.date()).days)

    if title_hit:
        if date_diff is None:
            rank = 2
        elif date_diff <= 1:
            rank = date_diff  # 0=日期一致，1=相差一天
        else:
            return None
    else:
        if date_diff is None:
            rank = 4
        elif date_diff == 0:
            rank = 3
        else:
            return None
    return rank, abs(video["created"] - event_dt.timestamp())


def pending_members(latest: dict, now: datetime) -> set[str]:
    """仍存在「已结束且缺 recording_bvid」事件的成员集合。"""
    members: set[str] = set()
    for day in latest.get("days", []):
        for event in day.get("events", []):
            if event.get("recording_bvid") or event.get("tag") == "rest":
                continue
            dt = parse_event_dt(day.get("date"), event.get("time"))
            if dt is None or dt > now:
                continue
            member = event.get("member")
            if member and member != "unknown":
                members.add(member)
    return members


def pending_group_types(data: dict, now: datetime) -> set[str]:
    """仍存在「已结束且缺 recording_bvid」团播事件的 group_type 集合。

    团播事件 member=unknown，不走成员投稿匹配，由团播录播号（accounts[].group_type）回填。
    """
    groups: set[str] = set()
    for day in data.get("days", []):
        for event in day.get("events", []):
            if event.get("recording_bvid") or event.get("tag") == "rest":
                continue
            dt = parse_event_dt(day.get("date"), event.get("time"))
            if dt is None or dt > now:
                continue
            group_type = event.get("group_type") or "none"
            if group_type != "none":
                groups.add(group_type)
    return groups


def apply_backfill(
    latest: dict,
    videos: list[dict],
    rec_cfg: dict,
    now: datetime | None = None,
) -> tuple[bool, list[dict]]:
    """按「成员/团播 + 标题 + 日期」匹配并回填 recording_bvid（纯逻辑，无网络/文件）。

    videos 为 normalize_videos 的输出；仅处理已结束、未回填、非 rest 的事件。
    两轮匹配，且已选中的视频移出候选池（一段录播只归属一场直播）：
    1. 优先标题相关的候选（标题是最强证据，容忍日期相差一天）；
    2. 剩余事件再按「日期一致 + 时间就近」兜底。
    返回 (是否有变更, 回填明细列表)。
    """
    now = now or datetime.now(CST)
    window_hours = rec_cfg.get("window_hours", 72)
    before_minutes = rec_cfg.get("before_minutes", 30)
    min_length = rec_cfg.get("min_length_minutes", 30)

    # 收集待回填的已结束事件（保持文档内先后顺序，即时间顺序）
    pending: list[tuple[dict, dict, datetime]] = []
    for day in latest.get("days", []):
        for event in day.get("events", []):
            if event.get("recording_bvid") or event.get("tag") == "rest":
                continue
            dt = parse_event_dt(day.get("date"), event.get("time"))
            if dt is None or dt > now:
                continue
            pending.append((day, event, dt))
    if not pending:
        return False, []

    pool = list(videos)  # 可用候选池；分配即移除，防止同一录播分给多个事件（含跨轮次）
    filled: list[dict] = []

    # 已回填事件（含历史轮次回填的）占用的视频同样移出候选池，
    # 保证同一录播不会被分配给多场直播。
    used_bvids = {ev.get("recording_bvid") for d in latest.get("days", []) for ev in d.get("events", []) if ev.get("recording_bvid")}
    pool = [v for v in pool if v.get("bvid") not in used_bvids]

    def candidates_for(event: dict, dt: datetime) -> list[dict]:
        lo = dt.timestamp() - before_minutes * 60
        hi = dt.timestamp() + window_hours * 3600
        member = event.get("member")
        group_type = event.get("group_type") or "none"
        out = []
        for v in pool:
            if not (lo <= v["created"] <= hi) or v["length_minutes"] < min_length:
                continue
            if member and member != "unknown":
                # 单播：成员本人投稿 + 专属录播号投稿（均按 member_key 归属）
                if v.get("member_key") != member:
                    continue
            elif group_type != "none":
                # 团播（member=unknown）：匹配对应团播录播号投稿（按 group_type 归属）
                if v.get("group_type") != group_type:
                    continue
            else:
                continue  # 归属成员未知且非团播：无法匹配，跳过（下轮依旧重试）
            out.append(v)
        return out

    def assign(day: dict, event: dict, video: dict) -> None:
        event["recording_bvid"] = video["bvid"]
        pool.remove(video)
        filled.append(
            {
                "date": day.get("date"),
                "time": event.get("time"),
                "member": event.get("member"),
                "event_title": event.get("title", ""),
                "bvid": video["bvid"],
                "video_title": video["title"],
            }
        )

    # 第一轮：标题相关的候选优先（标题是最强证据）
    remaining: list[tuple[dict, dict, datetime]] = []
    for day, event, dt in pending:
        scored = [
            (score, v)
            for v in candidates_for(event, dt)
            if (score := _match_score(event, dt, v)) is not None and score[0] <= 2
        ]
        if scored:
            scored.sort(key=lambda item: item[0])
            assign(day, event, scored[0][1])
        else:
            remaining.append((day, event, dt))

    # 第二轮：剩余事件按「日期一致 + 时间就近」兜底（仍受候选池去重约束）
    for day, event, dt in remaining:
        scored = [
            (score, v)
            for v in candidates_for(event, dt)
            if (score := _match_score(event, dt, v)) is not None
        ]
        if scored:
            scored.sort(key=lambda item: item[0])
            assign(day, event, scored[0][1])

    return bool(filled), filled


def iter_recent_archives(rec_cfg: dict) -> list[tuple[Path, dict]]:
    """加载最近 archive_weeks 个往日周归档（按 week_start 倒序，新周在前）。

    归档文件名为 {week_start}.json；文件名无法解析/读取失败的文件跳过。
    """
    archive_weeks = int(rec_cfg.get("archive_weeks", 4))
    candidates: list[tuple[str, Path]] = []
    for path in ARCHIVE_DIR.glob("*.json"):
        try:
            datetime.strptime(path.stem, "%Y-%m-%d")
        except ValueError:
            continue
        candidates.append((path.stem, path))
    candidates.sort(reverse=True)

    result: list[tuple[Path, dict]] = []
    for _, path in candidates[:archive_weeks]:
        try:
            with open(path, encoding="utf-8") as f:
                result.append((path, json.load(f)))
        except (ValueError, OSError) as exc:
            print(f"[recording] 归档 {path.name} 读取失败: {exc}")
    return result


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(config: dict, dry_run: bool = False) -> int:
    rec_cfg = {**DEFAULT_RECORDING_CONFIG, **(config.get("recording") or {})}
    if not rec_cfg.get("enabled", True):
        print("[recording] recording.enabled = false，跳过")
        return 0

    if not LATEST_JSON.exists():
        print("[recording] latest.json 不存在，跳过")
        return 0
    try:
        with open(LATEST_JSON, encoding="utf-8") as f:
            latest = json.load(f)
    except (ValueError, OSError) as exc:
        print(f"[recording] latest.json 读取失败: {exc}")
        return 0

    now = datetime.now(CST)

    # 扫描对象：当前周 latest.json + 最近 N 个往日周归档。
    # 往日周录播常滞后数天才上传，只扫当前周会在周交替后永久失联，
    # 客户端往日周刷新也永远拿不到「录像」标签。
    docs: list[tuple[Path, dict]] = [(LATEST_JSON, latest)]
    for archive_path, archive_data in iter_recent_archives(rec_cfg):
        if archive_data.get("week_start") == latest.get("week_start"):
            continue  # 当前周归档与 latest.json 内容重叠，跳过
        docs.append((archive_path, archive_data))

    targets: set[str] = set()
    group_targets: set[str] = set()
    for _, data in docs:
        targets |= pending_members(data, now)
        group_targets |= pending_group_types(data, now)
    if not targets and not group_targets:
        print("[recording] 无待回填事件，本轮跳过")
        return 0
    print(f"[recording] 待回填录播的成员: {', '.join(sorted(targets)) or '(无)'}")
    if group_targets:
        print(f"[recording] 待回填录播的团播: {', '.join(sorted(group_targets))}")
    print(f"[recording] 本轮范围: latest.json + {len(docs) - 1} 个往日周归档")

    session = _build_session()
    keys = fetch_wbi_keys(session)
    if keys is None:
        return 0
    img_key, sub_key = keys

    uid_by_key = {
        m.get("member_key"): str(m["uid"])
        for m in config.get("members", [])
        if m.get("member_key") and m.get("uid")
    }
    name_by_key = {
        m.get("member_key"): m.get("name", m.get("member_key"))
        for m in config.get("members", [])
        if m.get("member_key")
    }
    gap = rec_cfg.get("request_gap_seconds", 2)

    videos: list[dict] = []

    # 成员空间投稿：只扫描有待回填事件的成员（省请求、降风控风险）
    for member_key in sorted(targets):
        uid = uid_by_key.get(member_key)
        if not uid:
            print(f"[recording] 成员 {member_key} 未配置 uid，跳过")
            continue
        raw = fetch_space_videos(session, uid, img_key, sub_key, rec_cfg)
        videos.extend(normalize_videos(raw, member_key))
        print(f"[recording] {name_by_key.get(member_key, member_key)}: 拉取 {len(raw)} 个投稿")
        time.sleep(gap)

    # 额外录播组账号（三种归属方式）：
    # - member_key：专属录播号，全部投稿归属该成员（如心宜/思诺录播组）
    # - group_type：团播录播号，全部投稿归属对应团播分组（如 A-SOUL 团播组）
    # - title_map：混合账号，按标题关键词推断归属成员
    for account in rec_cfg.get("accounts") or []:
        uid = str(account.get("uid") or "")
        title_map = account.get("title_map") or {}
        account_member = account.get("member_key")
        account_group = account.get("group_type")
        if not uid or not (title_map or account_member or account_group):
            continue
        # 只拉取与待回填事件相关的账号（省请求、降风控风险）；
        # 配了 title_map 的混合账号无法预判归属，照常拉取。
        if not title_map:
            if account_member and account_member not in targets:
                continue
            if account_group and account_group not in group_targets:
                continue
        raw = fetch_space_videos(session, uid, img_key, sub_key, rec_cfg)
        attributed = 0
        for video in normalize_videos(raw, None):
            if account_member:
                video["member_key"] = account_member
            elif account_group:
                video["group_type"] = account_group
            else:
                member_key = next(
                    (mk for kw, mk in title_map.items() if kw and kw in video["title"]),
                    None,
                )
                if not member_key:
                    continue
                video["member_key"] = member_key
            videos.append(video)
            attributed += 1
        print(
            f"[recording] 录播组 {account.get('name', uid)}: "
            f"拉取 {len(raw)} 个投稿，归属 {attributed} 个"
        )
        time.sleep(gap)

    # 逐文档匹配回填（当前周 + 各往日周归档）
    changed_docs: list[tuple[Path, dict]] = []
    filled: list[dict] = []
    for path, data in docs:
        doc_changed, doc_filled = apply_backfill(data, videos, rec_cfg, now=now)
        if doc_changed:
            changed_docs.append((path, data))
            filled.extend(doc_filled)
    if not changed_docs:
        print("[recording] 本轮未匹配到新录播")
        return 0

    lines = [
        f"{item['date']} {item['time']} "
        f"{name_by_key.get(item['member'], item['member'])}"
        f"「{item['event_title']}」"
        f"→ [{item['bvid']}](https://www.bilibili.com/video/{item['bvid']})"
        f"（视频：{item['video_title']}）"
        for item in filled
    ]

    if dry_run:
        print("[recording] dry-run 模式，以下事件将被回填：")
        for line in lines:
            print("  " + line)
        return 0

    ensure_dirs()
    for path, data in changed_docs:
        old_version = int(data.get("version") or 0)
        data["version"] = next_version(old_version, now)
        data["updated_at"] = now.isoformat()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if path == LATEST_JSON:
            week_start = data.get("week_start")
            if week_start:
                shutil.copy(LATEST_JSON, ARCHIVE_DIR / f"{week_start}.json")
        print(f"[recording] 已回填 {path.name}，版本 {old_version} → {data['version']}")

    print(f"[recording] 共回填 {len(filled)} 条录播")
    send_alert("✅ 录播回填完成", "\n".join(lines))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="管道C：录播回填")
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印匹配结果，不写文件"
    )
    args = parser.parse_args()

    if not MEMBERS_YAML.exists():
        print(f"[recording] 缺少配置文件: {MEMBERS_YAML}")
        return 1
    with open(MEMBERS_YAML, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    return run(config, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
