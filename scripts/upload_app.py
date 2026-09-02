"""发布新版 APK：上传安装包 + 生成并上传版本清单 app_version.json。

App 的「推送升级」依赖 OSS 上的两个对象：
- app/app-release-{version_name}.apk    （安装包本体，客户端下载用）
- app_version.json                       （版本清单，客户端启动时拉取比对）

用法::

    python scripts/upload_app.py \\
        --apk ../app/build/outputs/apk/release/app-release.apk \\
        --version-code 3 --version-name 1.2 \\
        --notes "新增：支持 Android 8.0+\n修复：..."

环境变量（与 sync_oss.py 一致）：
- OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / OSS_ENDPOINT / OSS_BUCKET
- OSS_PREFIX（可选，多环境隔离）

说明：
- 客户端固定的版本清单地址 = `https://{bucket}.{endpoint}/app_version.json`
- apk_url 写的是对象级公共读地址；需保证 bucket 允许该对象 public-read
- 发布后建议用浏览器/curl 打开 app_version.json 验证一次
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

APP_OBJECT_PREFIX = "app"  # OSS 上 APK 所在目录


def _public_url(bucket: str, endpoint: str, object_key: str) -> str:
    """构造 OSS 对象公共读 URL（去掉 endpoint 的协议前缀）。"""
    host = endpoint.split("://", 1)[-1].rstrip("/")
    return f"https://{bucket}.{host}/{object_key}"


def main() -> int:
    parser = argparse.ArgumentParser(description="发布新版 App APK 到 OSS")
    parser.add_argument("--apk", required=True, help="Release APK 本地路径")
    parser.add_argument("--version-code", type=int, required=True, help="versionCode（与 App 一致）")
    parser.add_argument("--version-name", required=True, help='versionName，如 "1.2"')
    parser.add_argument("--notes", default="", help="更新说明（\\n 分隔多行）")
    args = parser.parse_args()

    apk_path = Path(args.apk)
    if not apk_path.exists():
        print(f"[upload-app] APK 不存在: {apk_path}")
        return 1

    required = ("OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_ENDPOINT", "OSS_BUCKET")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print(f"[upload-app] 未配置环境变量: {', '.join(missing)}")
        return 1

    import oss2

    endpoint = os.environ["OSS_ENDPOINT"]
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "https://" + endpoint
    prefix = os.environ.get("OSS_PREFIX", "").strip("/")
    auth = oss2.Auth(os.environ["OSS_ACCESS_KEY_ID"], os.environ["OSS_ACCESS_KEY_SECRET"])
    bucket = oss2.Bucket(auth, endpoint, os.environ["OSS_BUCKET"])

    # OSS 对象键（支持 OSS_PREFIX 前缀）
    apk_object = f"{prefix}/{APP_OBJECT_PREFIX}/app-release-{args.version_name}.apk" if prefix \
        else f"{APP_OBJECT_PREFIX}/app-release-{args.version_name}.apk"
    version_object = f"{prefix}/app_version.json" if prefix else "app_version.json"

    # 版本清单：version_code / apk_url 为客户端核心字段
    manifest = {
        "version_code": args.version_code,
        "version_name": args.version_name,
        "apk_url": _public_url(bucket.bucket_name, endpoint, apk_object),
        "notes": args.notes,
        "publish_time": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2)

    uploads = [
        (apk_path, apk_object, "application/vnd.android.package-archive", "public, max-age=3600"),
        (None, version_object, "application/json; charset=utf-8", "public, no-cache"),
    ]

    for local_file, object_key, content_type, cache_control in uploads:
        headers = {
            "Content-Type": content_type,
            "Cache-Control": cache_control,
            "x-oss-object-acl": "public-read",
        }
        body = local_file.read_bytes() if local_file is not None else manifest_json.encode("utf-8")
        try:
            bucket.put_object(object_key, body, headers=headers)
        except Exception as exc:
            print(f"[upload-app] 上传 {object_key} 失败: {exc}")
            return 1
        print(f"[upload-app] 已上传 {object_key}")

    print("[upload-app] 发布完成，版本清单：")
    print(manifest_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
