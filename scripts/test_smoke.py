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


if __name__ == "__main__":
    test_rule_extraction()
    test_flash_event_id()
    test_timeout_auto_publish()
    test_not_timeout()
    test_cleanup_expired()
    test_validate()
    test_main_merge_draft()

    # 收尾清理
    FLASH_JSON.unlink(missing_ok=True)
    FLASH_DRAFT_JSON.unlink(missing_ok=True)
    print("\n🎉 全部冒烟测试通过")
