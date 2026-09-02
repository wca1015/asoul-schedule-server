"""发布新版 App：更新 OSS 上的版本清单 app_version.json（APK 托管于 GitHub Releases）。

背景：阿里云 OSS 禁止通过默认域名（*.oss-cn-*.aliyuncs.com）公开分发 APK，
直接 GET 会返回 `ApkDownloadForbidden`（提示改用 CNAME 自定义域名）。
本项目没有备案域名，因此 APK 改由 **GitHub Releases** 托管，
OSS 上只保留客户端启动时拉取的版本清单 `app_version.json`。

发布流程（每次发版）：
1. 构建 Release APK：`gradlew assembleRelease`
2. 创建 GitHub Release 并上传 APK（资产名固定为 app-release-{version_name}.apk）::

       gh release create v1.3 path/to/app-release.apk#app-release-1.3.apk \\
           --repo wca1015/AsoulSchedule-APP --title "v1.3"

3. 运行本脚本：把 apk_url 指向上面的 Release 资产，上传 app_version.json 到 OSS::

       python scripts/upload_app.py --version-code 4 --version-name 1.3 --notes "..."

客户端固定的版本清单地址 = `https://{bucket}.{endpoint}/app_version.json`

环境变量：OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / OSS_ENDPOINT / OSS_BUCKET
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

DEFAULT_REPO = "wca1015/AsoulSchedule-APP"


def main() -> int:
    parser = argparse.ArgumentParser(description="更新 App 版本清单到 OSS")
    parser.add_argument("--version-code", type=int, required=True, help="versionCode（与 App 一致）")
    parser.add_argument("--version-name", required=True, help='versionName，如 "1.3"')
    parser.add_argument("--notes", default="", help="更新说明（\\n 分隔多行）")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="托管 APK 的 GitHub 仓库 owner/repo")
    args = parser.parse_args()

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

    # APK 下载地址 = GitHub Release 资产（需已用 gh release create 上传同名资产）
    apk_url = (
        f"https://github.com/{args.repo}/releases/download/"
        f"v{args.version_name}/app-release-{args.version_name}.apk"
    )

    manifest = {
        "version_code": args.version_code,
        "version_name": args.version_name,
        "apk_url": apk_url,
        "notes": args.notes,
        "publish_time": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2)

    object_key = f"{prefix}/app_version.json" if prefix else "app_version.json"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "public, no-cache",
        "x-oss-object-acl": "public-read",
    }
    try:
        bucket.put_object(object_key, manifest_json.encode("utf-8"), headers=headers)
    except Exception as exc:
        print(f"[upload-app] 上传 {object_key} 失败: {exc}")
        return 1

    print(f"[upload-app] 已上传 {object_key}")
    print("[upload-app] 发布完成，版本清单：")
    print(manifest_json)
    print("[upload-app] 若 Release 尚未创建，请先执行：")
    print(
        f"  gh release create v{args.version_name} <apk路径>#app-release-{args.version_name}.apk "
        f"--repo {args.repo} --title \"v{args.version_name}\""
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
