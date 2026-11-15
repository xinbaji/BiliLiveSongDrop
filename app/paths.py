"""路径管理。

所有用户数据落在 %LOCALAPPDATA%\\BiliLiveSongDrop，不写进项目目录，
避免升级 / 重装时把歌单和 cookie 弄丢。

旧版本用的是 %LOCALAPPDATA%\\BiliSinger，首次启动时整体迁移过来，
歌单、登录 cookie、缓存都不用重来。
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

from app import APP_NAME

# GUI 静态资源在项目里，跟着代码走
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 旧版数据目录名，仅用于一次性迁移
_LEGACY_DIR_NAME = "BiliSinger"


def project_root() -> Path:
    return _PROJECT_ROOT


def gui_dir() -> Path:
    """前端静态资源目录。

    PyInstaller 打包后资源解在 sys._MEIPASS 下的同名目录，
    源码运行时才是项目里的 gui/。
    """
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base) / "gui"
    return _PROJECT_ROOT / "gui"


def _local_appdata() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / ".local" / "share")
    return Path(base)


def _user_data_dir() -> Path:
    return _local_appdata() / APP_NAME


def migrate_legacy_data() -> str | None:
    """旧目录整体搬到新目录，只搬一次。

    数据目录改名后，第一次启动要把旧的 BiliSinger 整体搬过来，
    否则用户登录、歌单全没了。搬完留一个标记文件，旧目录也保留——
    万一要回滚旧版本，数据还在。
    """
    new_dir = _user_data_dir()
    old_dir = _local_appdata() / _LEGACY_DIR_NAME

    # 已存在说明迁移过或者本来就是新装，不做任何事
    if new_dir.exists() or not old_dir.exists():
        return None

    # 数据库可能被另一个实例占着，重试几次
    last_err: Exception | None = None
    for _ in range(3):
        try:
            shutil.move(str(old_dir), str(new_dir))
            marker = new_dir / "_migrated_from_BiliSinger.txt"
            marker.write_text(
                "本目录由旧版 BiliSinger 数据目录迁移而来，迁移时间："
                + time.strftime("%Y-%m-%d %H:%M:%S") + "\n"
                + "旧目录已随迁移移动，如需回滚旧版程序请反向移动。",
                encoding="utf-8",
            )
            return str(new_dir)
        except OSError as exc:
            last_err = exc
            time.sleep(1)
    print(f"数据目录迁移失败，将使用新的空目录：{last_err}", file=sys.stderr, flush=True)
    return None


DATA_DIR = _user_data_dir()
DB_PATH = DATA_DIR / "biliSinger.db"
CONFIG_PATH = DATA_DIR / "config.json"
CACHE_DIR = DATA_DIR / "cache"
AUDIO_CACHE_DIR = CACHE_DIR / "audio"
COVER_CACHE_DIR = CACHE_DIR / "cover"
LOG_DIR = DATA_DIR / "logs"
WEBVIEW_PROFILE_DIR = DATA_DIR / "webview_profile"


def ensure_dirs() -> None:
    for d in (DATA_DIR, CACHE_DIR, AUDIO_CACHE_DIR, COVER_CACHE_DIR, LOG_DIR, WEBVIEW_PROFILE_DIR):
        d.mkdir(parents=True, exist_ok=True)
