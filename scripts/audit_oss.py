"""审计 OSS 数据对象：列出桶内全部对象，标记过期归档与非预期残留。

用途：数据管道调整后核对 OSS 是否留有脏对象（历史测试文件、
超出回看窗口的归档等）。手动运行（需 OSS 四环境变量）：

    python scripts/audit_oss.py

预期对象（带可选 OSS_PREFIX 前缀时同样识别）：
- latest.json / flash.json / app_version.json
- week/{week}.json —— 仅「当前周 + 往前 sync_oss.RETENTION_WEEKS 周」窗口内
其余对象一律标记为「非预期」，由人工确认后清理（本脚本只读不删）。
"""
from __future__ import annotations

import os

from sync_oss import RETENTION_WEEKS, retention_floor

EXPECTED_ROOT = {"latest.json", "flash.json", "app_version.json"}


def main() -> int:
    required = (
        "OSS_ACCESS_KEY_ID",
        "OSS_ACCESS_KEY_SECRET",
        "OSS_ENDPOINT",
        "OSS_BUCKET",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print(f"[audit] 未配置环境变量: {', '.join(missing)}，无法审计")
        return 1

    import oss2

    endpoint = os.environ["OSS_ENDPOINT"]
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "https://" + endpoint
    prefix = os.environ.get("OSS_PREFIX", "").strip("/")
    auth = oss2.Auth(
        os.environ["OSS_ACCESS_KEY_ID"], os.environ["OSS_ACCESS_KEY_SECRET"]
    )
    bucket = oss2.Bucket(auth, endpoint, os.environ["OSS_BUCKET"])

    floor = retention_floor()
    total = 0
    expected = 0
    unexpected: list[tuple[str, str, int]] = []

    for obj in oss2.ObjectIterator(bucket, prefix=prefix or ""):
        total += 1
        key = obj.key
        rel = key[len(prefix) + 1 :] if prefix and key.startswith(prefix + "/") else key
        if rel in EXPECTED_ROOT:
            expected += 1
            continue
        if rel.startswith("week/") and rel.endswith(".json") and len(rel) == 20:
            week = rel[5:15]
            if week >= floor:
                expected += 1
                continue
            unexpected.append((key, f"过期归档（早于 {floor}）", obj.size))
            continue
        unexpected.append((key, "非预期对象", obj.size))

    print(f"[audit] 桶内共 {total} 个对象：预期 {expected} 个，可疑 {len(unexpected)} 个")
    print(f"[audit] 回看窗口：week >= {floor}（当前周 + 往前 {RETENTION_WEEKS} 周）")
    if not unexpected:
        print("[audit] ✅ 未发现过期归档或非预期对象")
        return 0
    print(f"[audit] ⚠️ 发现 {len(unexpected)} 个可疑对象（请人工确认后清理）：")
    for key, reason, size in sorted(unexpected):
        print(f"  - {key}  ({size} B)  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
