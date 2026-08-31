"""离线冒烟测试：不请求网络、不调用 AI，验证各管道核心逻辑。

运行：python scripts/test_smoke.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ARCHIVE_DIR, CST, DATA_DIR, DRAFT_JSON, FLASH_DRAFT_JSON, FLASH_JSON, LATEST_JSON, ensure_dirs  # noqa: E402


def test_rule_extraction() -> None:
    """Stage 2 规则提取：验证各种时间写法。"""
    from flash_recognize import extract_by_rules

    cases = [
        ("突击！今晚 8 点唱歌直播，速来", True),
        ("今晚8:30开播，不见不散", True),
        ("今天19点30分直播", True),
        ("晚上18点开始直播", True),
        ("今天天气不错，出去玩", False),
        ("明天20:00直播哦", False),  # 无"今晚/今天"前缀，规则不提取（交给AI）
    ]
    for text, should_match in cases:
        result = extract_by_rules(text, "jiaran")
        matched = result is not None
        assert matched == should_match, f"规则提取失败: {text!r} → {result}"
        if matched:
            assert result["member"] == "jiaran"
            assert result["start_time"].endswith("+08:00")

    # 专门验证"8点半"→ 30分
    r = extract_by_rules("今晚8点半直播", "bella")
    assert r and ":30:" in r["start_time"], f"8点半解析失败: {r}"
    print("✅ test_rule_extraction 通过")


def test_flash_event_id() -> None:
    """事件ID生成：规范时间与异常时间都不应崩溃。"""
    from datetime import datetime as _dt

    from flash_recognize import recognize_flash

    # 构造一个会命中规则提取的动态（不调用AI）
    dynamic = {"dynamic_id": "12345", "text": "突击！今晚 19:00 唱歌直播", "images": []}
    account = {"uid": "1", "name": "嘉然", "member_key": "jiaran"}
    config = {"keywords": {"include": ["突击"], "exclude": []}}

    event = recognize_flash(dynamic, account, config)
    assert event is not None, "命中关键词的动态应产出事件"
    assert event["id"].startswith("flash_")
    assert event["source_dynamic_id"] == "12345"
    assert event["source_url"] == "https://t.bilibili.com/12345"
    assert event["auto_published"] is False
    print(f"✅ test_flash_event_id 通过 (id={event['id']})")


def test_flash_auto_publish() -> None:
    """突击直播自动发布：识别校验通过即直接发布（无草稿/人工审核/超时机制）。"""
    ensure_dirs()
    FLASH_JSON.unlink(missing_ok=True)
    FLASH_DRAFT_JSON.unlink(missing_ok=True)

    now = datetime.now(CST)
    draft = {
        "_meta": {"recognized_at": now.isoformat()},
        "events": [
            {
                "id": "flash_test_1",
                "member": "jiaran",
                "title": "突击唱歌",
                "start_time": now.replace(microsecond=0).isoformat(),
                "end_time": None,
                "source_dynamic_id": "test_auto_001",
                "source_url": "https://t.bilibili.com/test_auto_001",
                "status": "upcoming",
                "auto_published": False,
            },
            {
                "id": "flash_test_2",
                "member": "bella",
                "title": "突击游戏",
                "start_time": now.replace(microsecond=0).isoformat(),
                "end_time": None,
                "source_dynamic_id": "test_auto_002",
                "source_url": "https://t.bilibili.com/test_auto_002",
                "status": "upcoming",
                "auto_published": False,
            },
        ],
    }

    from publish import publish_flash

    assert publish_flash(draft) is True, "新事件应直接发布"
    # 自动发布不应产生待审核草稿
    assert not FLASH_DRAFT_JSON.exists(), "自动发布不应残留 flash_draft.json"

    data = json.loads(FLASH_JSON.read_text(encoding="utf-8"))
    assert len(data["events"]) == 2

    # 幂等去重：同一 dynamic 再次发布不产生新事件
    assert publish_flash(draft) is False, "重复事件应被去重"
    data = json.loads(FLASH_JSON.read_text(encoding="utf-8"))
    assert len(data["events"]) == 2

    # 事件不带"超时未审核"标记（自动发布为设计内行为，客户端正常展示）
    assert all(ev["auto_published"] is False for ev in data["events"])

    print("✅ test_flash_auto_publish 通过")


def test_cleanup_expired() -> None:
    """过期事件清理：48小时前的被删，未来的保留。"""
    from flash_manager import cleanup_expired, load_flash_data, save_flash_data

    now = datetime.now(CST)
    data = {
        "version": 1,
        "updated_at": now.isoformat(),
        "events": [
            {"id": "old", "start_time": (now - timedelta(hours=60)).isoformat()},
            {"id": "recent", "start_time": now.isoformat()},
            {"id": "bad_time"},  # 无 start_time，视为过期
        ],
    }
    save_flash_data(data)

    removed = cleanup_expired()
    assert removed == 2, f"应清理2条，实际 {removed}"
    remaining = load_flash_data()["events"]
    assert len(remaining) == 1 and remaining[0]["id"] == "recent"
    print("✅ test_cleanup_expired 通过")


def test_validate() -> None:
    """校验模块：非法数据必须被拦截。"""
    from validate import validate_flash_event, validate_schedule

    good = {
        "member": "jiaran",
        "title": "测试",
        "start_time": datetime.now(CST).isoformat(),
        "source_dynamic_id": "123",
    }
    assert validate_flash_event(good) == []

    bad = dict(good, member="神秘人", start_time="not-a-time", source_dynamic_id="")
    errors = validate_flash_event(bad)
    assert len(errors) >= 3, f"应至少3个错误，实际: {errors}"

    # 使用相对日期（本周一）：年份合理性校验窗口随时间滑动，硬编码日期会过期失效
    now = datetime.now(CST)
    monday = (now - timedelta(days=now.weekday())).date()
    week_days = [monday + timedelta(days=i) for i in range(7)]
    good_schedule = {
        "week_start": week_days[0].isoformat(),
        "week_end": week_days[6].isoformat(),
        "days": [
            {
                "date": d.isoformat(),
                "weekday": "星期一",
                "events": [] if i != 1 else [
                    {"time": "19:00", "member": "jiaran", "title": "直播", "tag": "live"}
                ],
            }
            for i, d in enumerate(week_days)
        ],
    }
    assert validate_schedule(good_schedule) == []

    # 年份识别错误（海报不印年份，VLM 可能把 2026 认成 2023）必须被拦截
    bad_year = json.loads(json.dumps(good_schedule))
    bad_year["week_start"] = "2023-08-31"
    bad_year["week_end"] = "2023-09-06"
    for i, d in enumerate(bad_year["days"]):
        d["date"] = (datetime(2023, 8, 31).date() + timedelta(days=i)).isoformat()
    assert any("年份" in e for e in validate_schedule(bad_year)), "年份异常应被拦截"

    # 新增字段（团播分组/直播形式）：合法值通过，非法值拦截，缺省不报错（发布时兜底）
    tagged = json.loads(json.dumps(good_schedule))
    tagged["days"][1]["events"][0].update({"group_type": "asoul", "format": "game_room"})
    assert validate_schedule(tagged) == []
    bad_tagged = json.loads(json.dumps(good_schedule))
    bad_tagged["days"][1]["events"][0].update({"group_type": "mystery_group", "format": "karaoke"})
    assert len(validate_schedule(bad_tagged)) >= 2

    bad_schedule = dict(good_schedule, days=good_schedule["days"][:6])
    assert validate_schedule(bad_schedule) != []
    print("✅ test_validate 通过")


def test_publish_schedule_normalization() -> None:
    """周程表发布：缺失的 group_type / format 兜底补全，已有值保留。"""
    from publish import publish_schedule

    ensure_dirs()
    # 相对日期（本周一/二）：保证任意时间运行测试都在年份合理性窗口内
    now = datetime.now(CST)
    monday = (now - timedelta(days=now.weekday())).date()
    draft = {
        "week_start": monday.isoformat(),
        "week_end": (monday + timedelta(days=6)).isoformat(),
        "days": [
            {"date": monday.isoformat(), "weekday": "星期一",
             "events": [{"time": "19:00", "member": "jiaran", "title": "直播", "tag": "live"}]},
            {"date": (monday + timedelta(days=1)).isoformat(), "weekday": "星期二",
             "events": [{"time": "20:00", "member": "unknown", "title": "游戏室", "tag": "show",
                          "group_type": "asoul", "format": "game_room"}]},
        ],
    }
    DRAFT_JSON.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    LATEST_JSON.unlink(missing_ok=True)

    # publish_schedule 附带归档副作用（archive/{week_start}.json）：
    # 测试前备份原文件、结束后还原，避免测试草稿污染真实归档
    # （此前本测试曾在 archive/ 残留含测试数据的 2026-08-17.json）
    archive_file = ARCHIVE_DIR / f"{draft['week_start']}.json"
    archive_backup = archive_file.read_bytes() if archive_file.exists() else None

    try:
        publish_schedule()

        published = json.loads(LATEST_JSON.read_text(encoding="utf-8"))
        ev0 = published["days"][0]["events"][0]
        ev1 = published["days"][1]["events"][0]
        assert ev0["group_type"] == "none" and ev0["format"] == "normal", f"缺省兜底失败: {ev0}"
        assert ev1["group_type"] == "asoul" and ev1["format"] == "game_room", f"已有值被覆盖: {ev1}"
        assert published["version"] > 0 and published["source"] == "auto"
    finally:
        # 清理：测试草稿 / latest，并还原归档
        DRAFT_JSON.unlink(missing_ok=True)
        LATEST_JSON.unlink(missing_ok=True)
        if archive_backup is None:
            archive_file.unlink(missing_ok=True)
        else:
            archive_file.write_bytes(archive_backup)
    print("✅ test_publish_schedule_normalization 通过")


def test_next_version() -> None:
    """版本号递增：严格单调，同小时内多次发布不回退。"""
    from publish import next_version

    now = datetime(2026, 8, 27, 12, 0, tzinfo=CST)
    stamp = 2026082712
    assert next_version(0, now) == stamp
    assert next_version(stamp, now) == stamp + 1
    assert next_version(stamp + 5, now) == stamp + 6
    print("✅ test_next_version 通过")


def test_flash_version_monotonic() -> None:
    """flash.json 版本号：同分钟内多次写入必须严格递增（统一走 next_version）。

    旧实现直接用分钟戳 %Y%m%d%H%M：同一分钟内两次发布版本号持平，
    客户端按版本号比较会漏拉第二次的新事件。
    """
    from publish import publish_flash

    now = datetime.now(CST)
    minute_stamp = int(now.strftime("%Y%m%d%H%M"))

    # 预置一个偏高的版本号（模拟同一分钟内已多次写入）
    seed_version = minute_stamp + 5
    FLASH_JSON.write_text(json.dumps({
        "version": seed_version,
        "updated_at": now.isoformat(),
        "events": [],
    }, ensure_ascii=False), encoding="utf-8")

    def make_draft(dyn_id: str) -> dict:
        return {"events": [{
            "id": f"flash_test_{dyn_id}",
            "member": "jiaran",
            "title": "测试",
            "start_time": now.isoformat(),
            "source_dynamic_id": dyn_id,
            "auto_published": False,
        }]}

    assert publish_flash(make_draft("mono_001")) is True
    v1 = json.loads(FLASH_JSON.read_text(encoding="utf-8"))["version"]
    assert v1 > seed_version, f"版本号未递增反而持平/回退: {seed_version} → {v1}"

    # 紧接着再发布一条（大概率同分钟），版本号仍须严格递增
    assert publish_flash(make_draft("mono_002")) is True
    v2 = json.loads(FLASH_JSON.read_text(encoding="utf-8"))["version"]
    assert v2 > v1, f"同分钟二次写入版本号未严格递增: {v1} → {v2}"

    print("✅ test_flash_version_monotonic 通过")


def test_archive_scan() -> None:
    """往日周归档扫描：只读最近 N 周、文件名非法跳过、按周倒序。"""
    from recording_backfill import iter_recent_archives

    ensure_dirs()
    archives = {
        "2026-08-03.json": {"week_start": "2026-08-03", "version": 1, "days": []},
        "2026-08-10.json": {"week_start": "2026-08-10", "version": 2, "days": []},
        "2026-08-17.json": {"week_start": "2026-08-17", "version": 3, "days": []},
        "2026-08-24.json": {"week_start": "2026-08-24", "version": 4, "days": []},
        "not-a-date.json": {"week_start": "bad", "version": 0, "days": []},
    }
    # 先备份并清空真实归档（测试期间目录只含测试文件，断言不依赖仓库实际内容），
    # 结束后还原，避免污染仓库中的真实往日周数据。
    archive_backup = {
        p.name: p.read_bytes()
        for p in ARCHIVE_DIR.glob("*.json")
    }
    for p in ARCHIVE_DIR.glob("*.json"):
        p.unlink()
    for name, content in archives.items():
        (ARCHIVE_DIR / name).write_text(
            json.dumps(content, ensure_ascii=False), encoding="utf-8"
        )
    try:
        # 默认 archive_weeks=4：取最近 4 个合法归档，非法文件名跳过，新周在前
        docs = iter_recent_archives({})
        names = [p.name for p, _ in docs]
        assert names == [
            "2026-08-24.json", "2026-08-17.json",
            "2026-08-10.json", "2026-08-03.json",
        ], f"归档排序/过滤错误: {names}"

        # archive_weeks=2：只取最近 2 周
        docs = iter_recent_archives({"archive_weeks": 2})
        assert [p.name for p, _ in docs] == ["2026-08-24.json", "2026-08-17.json"]

        # 内容与版本正确加载（往日周录播回填的数据源）
        data_by_name = {p.name: d for p, d in iter_recent_archives({})}
        assert data_by_name["2026-08-17.json"]["version"] == 3
    finally:
        # 清理测试文件 + 还原真实归档（含被覆盖的同名文件）
        for name in archives:
            (ARCHIVE_DIR / name).unlink(missing_ok=True)
        for name, content in archive_backup.items():
            (ARCHIVE_DIR / name).write_bytes(content)
    print("✅ test_archive_scan 通过")


def test_backfill_accuracy() -> None:
    """回填防错配：一段录播只归属一场、标题日期校验、同名不同场不误分。"""
    from datetime import date as _date

    from recording_backfill import apply_backfill, parse_event_dt, video_title_date

    now = datetime.now(CST)
    day1 = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    day2 = now.strftime("%Y-%m-%d")

    # 标题日期提取：常见写法都能解析，无日期返回 None
    assert video_title_date("【思诺】2026.08.24 鸣潮1.3主线～【2D直播录像】") == _date(2026, 8, 24)
    assert video_title_date("【A-SOUL双播】2026.8.27 拆弹专家！") == _date(2026, 8, 27)
    assert video_title_date("2026-08-24 回放") == _date(2026, 8, 24)
    assert video_title_date("直播录像回放") is None

    def _doc(events: list[dict]) -> dict:
        return {"days": [{"date": day1, "events": events}]}

    ts = parse_event_dt(day1, "17:00").timestamp()

    # 1. 同成员同晚两场直播只有一个录播：只归属第一场，不得重复分配。
    doc = _doc([
        {"time": "17:00", "member": "sinuo", "title": "思诺直播", "tag": "live"},
        {"time": "21:00", "member": "sinuo", "title": "听筒回信", "tag": "live"},
    ])
    vids = [{"member_key": "sinuo", "bvid": "BV1only", "created": int(ts + 3600),
             "title": f"【思诺】{day1} 鸣潮主线【2D直播录像】", "length_minutes": 90.0}]
    changed, filled = apply_backfill(doc, vids, {}, now=now)
    assert changed and len(filled) == 1, f"只应回填一场，实际: {filled}"
    evs = doc["days"][0]["events"]
    assert evs[0]["recording_bvid"] == "BV1only" and evs[1].get("recording_bvid") is None
    # 全部回填结果中不允许同一 bvid 出现两次
    assert len({f["bvid"] for f in filled}) == len(filled)

    # 2. 标题无关且标题日期与事件日期不一致：拒绝匹配（防止仅凭时间就近错配）。
    doc2 = _doc([{"time": "17:00", "member": "sinuo", "title": "思诺直播", "tag": "live"}])
    tomorrow_title = (now + timedelta(days=1)).strftime("%Y.%m.%d")
    vids2 = [{"member_key": "sinuo", "bvid": "BV1wrongday", "created": int(ts + 7200),
              "title": f"【思诺】{tomorrow_title} 八月爱七日【2D直播录像】",
              "length_minutes": 90.0}]
    assert apply_backfill(doc2, vids2, {}, now=now)[0] is False

    # 3. 标题命中但日期相差一天：容忍（周程表识别/录播标注偏差一天）。
    doc3 = _doc([{"time": "19:30", "member": "xinyi", "title": "宁静的夏夜", "tag": "live"}])
    ts3 = parse_event_dt(day1, "19:30").timestamp()
    vids3 = [{"member_key": "xinyi", "bvid": "BV1off1", "created": int(ts3 + 3600),
              "title": f"【心宜】{day2} 宁静的夏夜【录播回放】", "length_minutes": 120.0}]
    _, filled3 = apply_backfill(doc3, vids3, {}, now=now)
    assert len(filled3) == 1 and filled3[0]["bvid"] == "BV1off1"

    # 4. 标题命中但日期相差两天以上：拒绝（大概率是另一周的同名企划）。
    doc4 = _doc([{"time": "20:00", "member": "nailin", "title": "幻梦登影", "tag": "special"}])
    far_title = (now + timedelta(days=3)).strftime("%Y.%m.%d")
    vids4 = [{"member_key": "nailin", "bvid": "BV1far", "created": int(ts + 3600),
              "title": f"【乃琳】{far_title} 幻梦登影【录播回放】", "length_minutes": 120.0}]
    assert apply_backfill(doc4, vids4, {}, now=now)[0] is False

    # 5. 标题命中优于日期兜底：即使兜底候选时间更近，也先给标题相关事件。
    doc5 = _doc([
        {"time": "19:00", "member": "jiaran", "title": "歌回", "tag": "live"},
        {"time": "20:00", "member": "jiaran", "title": "其他直播", "tag": "live"},
    ])
    ts5a = parse_event_dt(day1, "19:00").timestamp()
    ts5b = parse_event_dt(day1, "20:00").timestamp()
    vids5 = [
        {"member_key": "jiaran", "bvid": "BV1title", "created": int(ts5a + 3600),
         "title": f"嘉然 {day1} 歌回回放", "length_minutes": 90.0},
        {"member_key": "jiaran", "bvid": "BV1near", "created": int(ts5b - 600),
         "title": "录播", "length_minutes": 90.0},
    ]
    _, filled5 = apply_backfill(doc5, vids5, {}, now=now)
    by_title = {f["event_title"]: f["bvid"] for f in filled5}
    assert by_title.get("歌回") == "BV1title", f"标题命中应优先: {by_title}"
    assert by_title.get("其他直播") == "BV1near"  # 兜底拿到剩余候选，且不重复
    assert len({f["bvid"] for f in filled5}) == 2

    print("✅ test_backfill_accuracy 通过")


def test_recording_backfill() -> None:
    """管道C 录播回填：成员+时间窗匹配、切片过滤、标题优先、幂等。"""
    import copy

    from recording_backfill import (
        apply_backfill,
        normalize_videos,
        parse_event_dt,
        parse_length_minutes,
        pending_group_types,
        pending_members,
    )

    # 时长解析
    assert parse_length_minutes("1:23:45") == 83.75
    assert abs(parse_length_minutes("45:10") - (45 + 10 / 60)) < 1e-9
    assert parse_length_minutes("abc") == 0.0

    now = datetime.now(CST)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    latest = {
        "version": 1,
        "week_start": yesterday,
        "week_end": tomorrow,
        "days": [
            {
                "date": yesterday,
                "weekday": "星期X",
                "events": [
                    {"time": "20:00", "member": "jiaran", "title": "唱歌直播", "tag": "live"},
                    {"time": "21:00", "member": "bella", "title": "休息", "tag": "rest"},
                ],
            },
            {
                "date": tomorrow,
                "weekday": "星期X",
                "events": [
                    {"time": "19:00", "member": "jiaran", "title": "未来直播", "tag": "live"},
                ],
            },
        ],
    }

    # 只有"已结束且缺 bvid"的成员进入扫描目标
    assert pending_members(latest, now) == {"jiaran"}

    event_ts = parse_event_dt(yesterday, "20:00").timestamp()
    raw = [
        # 正确候选：开播后 2 小时上传，时长 60 分钟
        {"bvid": "BV1good", "created": int(event_ts + 2 * 3600),
         "title": "嘉然 唱歌直播回放", "length": "1:00:00"},
        # 切片：仅 5 分钟，应被时长过滤
        {"bvid": "BV1clip", "created": int(event_ts + 2 * 3600),
         "title": "唱歌直播切片", "length": "5:00"},
        # 超出时间窗：开播后第 5 天才上传
        {"bvid": "BV1late", "created": int(event_ts + 5 * 86400),
         "title": "唱歌直播回放", "length": "1:00:00"},
    ]
    videos = normalize_videos(raw, "jiaran")
    # 成员不匹配的候选
    videos += normalize_videos(
        [{"bvid": "BV1wrong", "created": int(event_ts + 3600),
          "title": "唱歌直播", "length": "1:00:00"}],
        "bella",
    )

    # 时间窗可配置：窗口缩到 0.5 小时则 2 小时后上传的候选不命中
    assert apply_backfill(copy.deepcopy(latest), videos,
                          {"window_hours": 0.5}, now=now)[0] is False

    changed, filled = apply_backfill(latest, videos, {}, now=now)
    assert changed is True and len(filled) == 1
    assert filled[0]["bvid"] == "BV1good"
    assert latest["days"][0]["events"][0]["recording_bvid"] == "BV1good"
    assert latest["days"][0]["events"][1].get("recording_bvid") is None  # rest 不回填
    assert latest["days"][1]["events"][0].get("recording_bvid") is None  # 未来不回填

    # 幂等：已回填的事件不会重复匹配
    assert apply_backfill(latest, videos, {}, now=now) == (False, [])

    # 标题优先：标题相关但上传更晚 优于 上传更近但标题无关
    ts2 = parse_event_dt(yesterday, "19:00").timestamp()
    latest2 = {
        "days": [{"date": yesterday, "events": [
            {"time": "19:00", "member": "nailin", "title": "夜谈", "tag": "live"}]}]
    }
    vids2 = [
        {"member_key": "nailin", "bvid": "BV1near", "created": int(ts2 + 3600),
         "title": "录播", "length_minutes": 60.0},
        {"member_key": "nailin", "bvid": "BV1title", "created": int(ts2 + 3 * 3600),
         "title": "乃琳夜谈回放", "length_minutes": 60.0},
    ]
    _, filled2 = apply_backfill(latest2, vids2, {}, now=now)
    assert filled2[0]["bvid"] == "BV1title"

    # 团播回填：member=unknown 的团播事件按 group_type 匹配团播录播号投稿
    latest_group = {
        "days": [{"date": yesterday, "events": [
            {"time": "20:00", "member": "unknown", "title": "游戏室",
             "tag": "show", "group_type": "asoul"}]}]
    }
    assert pending_group_types(latest_group, now) == {"asoul"}
    assert pending_members(latest_group, now) == set()  # 团播不计入成员目标
    ts_group = parse_event_dt(yesterday, "20:00").timestamp()
    vids_group = [
        # 团播录播号投稿：归属 group_type=asoul，应命中
        {"member_key": None, "group_type": "asoul", "bvid": "BV1group",
         "created": int(ts_group + 3600), "title": "游戏室回放", "length_minutes": 90.0},
        # 其他团播分组：不应命中 asoul 事件
        {"member_key": None, "group_type": "xinyi_sinuo", "bvid": "BV1other",
         "created": int(ts_group + 3600), "title": "游戏室回放", "length_minutes": 90.0},
    ]
    changed_g, filled_g = apply_backfill(copy.deepcopy(latest_group), vids_group, {}, now=now)
    assert changed_g is True and filled_g[0]["bvid"] == "BV1group"

    # 非团播且 member=unknown：无法归属，跳过不崩溃（下轮重试）
    latest_unknown = {
        "days": [{"date": yesterday, "events": [
            {"time": "21:00", "member": "unknown", "title": "神秘企划", "tag": "show"}]}]
    }
    assert apply_backfill(latest_unknown, vids_group, {}, now=now)[0] is False

    # before_minutes：定时投稿（略早于开播）也可匹配
    latest3 = {
        "days": [{"date": yesterday, "events": [
            {"time": "20:00", "member": "xinyi", "title": "直播", "tag": "live"}]}]
    }
    vids3 = [{"member_key": "xinyi", "bvid": "BV1early",
              "created": int(parse_event_dt(yesterday, "20:00").timestamp() - 600),
              "title": "回放", "length_minutes": 45.0}]
    _, filled3 = apply_backfill(latest3, vids3, {}, now=now)
    assert filled3[0]["bvid"] == "BV1early"

    print("✅ test_recording_backfill 通过")


def test_joint_video_exclusion() -> None:
    """联合直播不归属个人单播：心宜/思诺双人企划不得被识别成个人直播。

    心球仪周报等专属录播号同时会上传「思诺&心宜」双人录像，
    这类视频只能匹配团播/双人企划事件，绝不允许回填到 member=xinyi/sinuo
    的个人直播事件上。
    """
    from recording_backfill import apply_backfill, is_joint_video, parse_event_dt

    # 联动标记识别
    assert is_joint_video("【思诺&心宜】2026.08.30 假期保卫战【直播录像】")
    assert is_joint_video("【心宜&思诺】 2026.08.23 现在一起想想想~【录播回放】")
    assert is_joint_video("【A-SOUL双播】嘉然&贝拉 2026.8.27 拆弹专家！【直播录像】")
    assert not is_joint_video("【心宜2D】2026.08.29 三角洲行动！【录播回放】")
    assert not is_joint_video("【思诺】2026.08.24 鸣潮1.3主线～【2D直播录像】")

    now = datetime.now(CST)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    ts = parse_event_dt(yesterday, "19:30").timestamp()

    # 场景1：xinyi 个人直播事件 + 「思诺&心宜」双人录像（即使时长足够长）
    # → 不得回填（防止双人企划被识别成个人直播）
    doc = {"days": [{"date": yesterday, "events": [
        {"time": "19:30", "member": "xinyi", "title": "虫鸣梦幻夜", "tag": "live"}]}]}
    joint_vids = [{"member_key": "xinyi", "bvid": "BV1joint",
                   "created": int(ts + 3600),
                   "title": f"【思诺&心宜】 {yesterday} 虫鸣梦幻夜【录播回放】",
                   "length_minutes": 249.0}]
    assert apply_backfill(doc, joint_vids, {}, now=now)[0] is False

    # 场景2：同池中同时有双人录像与个人单播录像 → 只回填个人单播录像
    doc2 = {"days": [{"date": yesterday, "events": [
        {"time": "19:30", "member": "xinyi", "title": "宁静的夏夜", "tag": "live"}]}]}
    mixed = [
        {"member_key": "xinyi", "bvid": "BV1joint2",
         "created": int(ts + 3600),
         "title": f"【心宜&思诺】 {yesterday} 宁静的夏夜【录播回放】",
         "length_minutes": 249.0},
        {"member_key": "xinyi", "bvid": "BV1solo",
         "created": int(ts + 7200),
         "title": f"【心宜】 {yesterday} 宁静的夏夜【录播回放】",
         "length_minutes": 169.0},
    ]
    _, filled2 = apply_backfill(doc2, mixed, {}, now=now)
    assert [f["bvid"] for f in filled2] == ["BV1solo"], f"联合录像不得顶替个人单播: {filled2}"

    # 场景3：团播事件（group_type=xinyi_sinuo）走团播录播号，不受影响
    doc3 = {"days": [{"date": yesterday, "events": [
        {"time": "20:00", "member": "unknown", "title": "假期保卫战",
         "tag": "show", "group_type": "xinyi_sinuo"}]}]}
    group_vids = [{"member_key": None, "group_type": "xinyi_sinuo",
                   "bvid": "BV1group2", "created": int(ts + 3600),
                   "title": f"【思诺&心宜】 {yesterday} 假期保卫战【直播录像】",
                   "length_minutes": 291.0}]
    changed3, filled3 = apply_backfill(doc3, group_vids, {}, now=now)
    assert changed3 and filled3[0]["bvid"] == "BV1group2"

    print("✅ test_joint_video_exclusion 通过")


if __name__ == "__main__":
    # 冒烟测试会改写/删除 FLASH_JSON、FLASH_DRAFT_JSON 等真实文件，
    # 先备份真实数据文件、结束后恢复，避免测试污染线上数据
    _protected = (FLASH_JSON, FLASH_DRAFT_JSON, DRAFT_JSON, LATEST_JSON)
    # 归档目录：test_archive_scan 会写入临时归档，运行前记录快照，结束后清理新增文件
    _backup: dict[Path, bytes | None] = {
        p: (p.read_bytes() if p.exists() else None) for p in _protected
    }

    try:
        test_rule_extraction()
        test_flash_event_id()
        test_flash_auto_publish()
        test_cleanup_expired()
        test_validate()
        test_publish_schedule_normalization()
        test_next_version()
        test_flash_version_monotonic()
        test_archive_scan()
        test_backfill_accuracy()
        test_recording_backfill()
        test_joint_video_exclusion()
    finally:
        # 恢复被测试触碰的文件：原本不存在则删除，否则还原内容
        for p, content in _backup.items():
            if content is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(content)

    print("\n🎉 全部冒烟测试通过")
