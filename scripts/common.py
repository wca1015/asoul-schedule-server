"""公共路径与环境配置。

所有脚本统一通过此模块获取项目路径、数据目录与配置，
避免硬编码相对路径导致在 GitHub Actions / 本地运行行为不一致。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Windows 控制台默认使用 GBK 编码，输出中文/emoji 会报
# UnicodeEncodeError。这里统一把 stdout/stderr 重配置为 UTF-8，
# errors="replace" 保证极端情况下也不会中断脚本。
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            # reconfigure 需要 Python>=3.7 且流为可写的；失败则忽略
            pass

# 项目根目录（本文件位于 scripts/ 下，根目录为其父目录）
ROOT_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = ROOT_DIR / "data"
CONFIG_DIR = ROOT_DIR / "config"
ARCHIVE_DIR = ROOT_DIR / "archive"

# 周程表相关文件
LATEST_JSON = DATA_DIR / "latest.json"
DRAFT_JSON = DATA_DIR / "draft.json"
LAST_DYNAMIC_ID_FILE = DATA_DIR / "last_dynamic_id.txt"

# 突击直播相关文件
FLASH_JSON = DATA_DIR / "flash.json"
FLASH_DRAFT_JSON = DATA_DIR / "flash_draft.json"
MEMBERS_YAML = CONFIG_DIR / "members.yaml"

# 时区：所有时间统一使用东八区（北京时间）
from datetime import timezone, timedelta

CST = timezone(timedelta(hours=8))


def ensure_dirs() -> None:
    """确保必要目录存在。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


def get_env(name: str, default: str | None = None) -> str:
    """读取环境变量，缺失且无默认值时抛出友好错误。"""
    value = os.environ.get(name, default)
    if value is None:
        raise EnvironmentError(f"缺少环境变量: {name}，请在 Secrets 或本地环境中配置")
    return value
