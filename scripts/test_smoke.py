"""离线冒烟测试：不请求网络、不调用 AI，验证各管道核心逻辑。

运行：python scripts/test_smoke.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import CST, DATA_DIR, FLASH_DRAFT_JSON, FLASH_JSON, ensure_dirs  # noqa: E402


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


def test_timeout_auto_publish() -> None:
    """超时自动发布：过期草稿 → 事件带 auto_published=true 进入 flash.json。"""
    ensure_dirs()
    # 清理环境
    FLASH_JSON.unlink(missing_ok=True)

    old_time = (datetime.now(CST) - timedelta(minutes=15)).isoformat()
    draft = {
        "_meta": {"status": "pending_review", "recognized_at": old_time},
        "events": [
            {
                "id": "flash_test",
                "member": "jiaran",
                "title": "测试突击",
                "start_time": datetime.now(CST).replace(microsecond=0).isoformat(),
                "end_time": None,
                "source_dynamic_id": "test_001",
                "source_url": "https://t.bilibili.com/test_001",
                "status": "upcoming",
                "auto_published": False,
                "recognized_at": old_time,
            }
        ],
    }
    FLASH_DRAFT_JSON.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    from auto_publish_timeout import check_and_auto_publish

    assert check_and_auto_publish() is True, "超时草稿应触发自动发布"
    assert not FLASH_DRAFT_JSON.exists(), "发布后草稿应被清理"

    data = json.loads(FLASH_JSON.read_text(encoding="utf-8"))
    assert len(data["events"]) == 1
    ev = data["events"][0]
    assert ev["auto_published"] is True, "auto_published 必须标记在事件上"
    assert "10分钟" in ev["review_note"]

    # 重复调用不应重复发布
    assert check_and_auto_publish() is False
    print("✅ test_timeout_auto_publish 通过")


def test_not_timeout() -> None:
    """未超时的草稿不应被发布。"""
    FLASH_JSON.unlink(missing_ok=True)
    fresh_time = (datetime.now(CST) - timedelta(minutes=2)).isoformat()
    draft = {
        "_meta": {"status": "pending_review", "recognized_at": fresh_time},
        "events": [
            {
                "member": "bella",
                "title": "新鲜草稿",
                "start_time": datetime.now(CST).replace(microsecond=0).isoformat(),
                "source_dynamic_id": "test_002",
                "auto_published": False,
            }
        ],
    }
    FLASH_DRAFT_JSON.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    from auto_publish_timeout import check_and_auto_publish

    assert check_and_auto_publish() is False, "未超时草稿不应发布"
    assert FLASH_DRAFT_JSON.exists(), "未超时草稿应保留"
    print("✅ test_not_timeout 通过")


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

    good_schedule = {
        "week_start": "2026-08-17",
        "week_end": "2026-08-23",
        "days": [
            {
                "date": f"2026-08-{17 + i}",
                "weekday": "星期一",
                "events": [] if i != 1 else [
                    {"time": "19:00", "member": "jiaran", "title": "直播", "tag": "live"}
                ],
            }
            for i in range(7)
        ],
    }
    assert validate_schedule(good_schedule) == []

    bad_schedule = dict(good_schedule, days=good_schedule["days"][:6])
    assert validate_schedule(bad_schedule) != []
    print("✅ test_validate 通过")


def test_main_merge_draft() -> None:
    """main.py 草稿合并逻辑：已有 pending 草稿时追加事件，不重置倒计时。"""
    FLASH_DRAFT_JSON.unlink(missing_ok=True)

    from main import _read_pending_flash_draft, _write_flash_draft

    meta = {"status": "pending_review", "recognized_at": "2026-01-01T00:00:00+08:00"}
    _write_flash_draft([{"id": "a"}], meta)

    events, m = _read_pending_flash_draft()
    assert len(events) == 1 and m["recognized_at"] == meta["recognized_at"]

    # 追加
    merged = events + [{"id": "b"}]
    _write_flash_draft(merged, dict(m))
    events2, _ = _read_pending_flash_draft()
    assert len(events2) == 2

    # 清理
    FLASH_DRAFT_JSON.unlink()
    print("✅ test_main_merge_draft 通过")


def test_next_version() -> None:
    """版本号递增：严格单调，同小时内多次发布不回退。"""
    from publish import next_version

    now = datetime(2026, 8, 27, 12, 0, tzinfo=CST)
    stamp = 2026082712
    assert next_version(0, now) == stamp
    assert next_version(stamp, now) == stamp + 1
    assert next_version(stamp + 5, now) == stamp + 6
    print("✅ test_next_version 通过")


def test_recording_backfill() -> None:
    """管道C 录播回填：成员+时间窗匹配、切片过滤、标题优先、幂等。"""
    import copy

    from recording_backfill import (
        apply_backfill,
        normalize_videos,
        parse_event_dt,
        parse_length_minutes,
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


if __name__ == "__main__":
    test_rule_extraction()
    test_flash_event_id()
    test_timeout_auto_publish()
    test_not_timeout()
    test_cleanup_expired()
    test_validate()
    test_main_merge_draft()
    test_next_version()
    test_recording_backfill()

    # 收尾清理
    FLASH_JSON.unlink(missing_ok=True)
    FLASH_DRAFT_JSON.unlink(missing_ok=True)
    print("\n🎉 全部冒烟测试通过")
