"""将正式数据文件同步到阿里云 OSS（国内客户端分发）。

GitHub 仓库始终是数据的唯一事实来源（可回滚、可审计）；
本脚本只把两个正式文件镜像到 OSS，供国内客户端就近拉取：

- data/latest.json  -> latest.json   （周程表）
- data/flash.json   -> flash.json    （突击直播）

草稿文件（draft.json / flash_draft.json）属于内部审核数据，不上传。
未配置 OSS 环境变量时静默跳过，不影响主流程（便于本地调试与灰度迁移）。
"""
from __future__ import annotations

import os
import sys

from common import FLASH_JSON, LATEST_JSON

# 本地文件 -> (OSS 对象键, Cache-Control)
# flash.json 客户端每 5 分钟轮询，缓存尽量短；
# latest.json 每周才更新，给 5 分钟缓存即可。
UPLOAD_MAP = {
    LATEST_JSON: ("latest.json", "public, max-age=300"),
    FLASH_JSON: ("flash.json", "public, max-age=60"),
}


def main() -> int:
    required = (
        "OSS_ACCESS_KEY_ID",
        "OSS_ACCESS_KEY_SECRET",
        "OSS_ENDPOINT",
        "OSS_BUCKET",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print(f"[oss] 未配置环境变量: {', '.join(missing)}，跳过同步")
        return 0

    # 放在检查之后导入：未配置 OSS 时本地无需安装 oss2
    import oss2

    endpoint = os.environ["OSS_ENDPOINT"]
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "https://" + endpoint

    prefix = os.environ.get("OSS_PREFIX", "").strip("/")
    auth = oss2.Auth(
        os.environ["OSS_ACCESS_KEY_ID"], os.environ["OSS_ACCESS_KEY_SECRET"]
    )
    bucket = oss2.Bucket(auth, endpoint, os.environ["OSS_BUCKET"])

    uploaded = 0
    failed = 0
    for local_file, (key, cache_control) in UPLOAD_MAP.items():
        if not local_file.exists():
            print(f"[oss] {local_file.name} 尚不存在，跳过")
            continue
        object_key = f"{prefix}/{key}" if prefix else key
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": cache_control,
            # 对象级公共读：bucket 本身可保持私有，只公开这两个数据文件
            "x-oss-object-acl": "public-read",
        }
        try:
            bucket.put_object(object_key, local_file.read_bytes(), headers=headers)
        except Exception as exc:  # 同步失败不阻断管道：仓库始终是事实来源
            failed += 1
            print(f"[oss] 上传 {object_key} 失败: {exc}")
            continue
        uploaded += 1
        print(f"[oss] 已上传 {object_key} (Cache-Control: {cache_control})")

    print(f"[oss] 同步完成，成功 {uploaded} 个，失败 {failed} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
