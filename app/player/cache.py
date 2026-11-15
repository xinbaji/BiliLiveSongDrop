"""音频磁盘缓存。

B 站直链 2 小时就过期，网易云的链接也不稳定，所以播过一遍就落盘，
下次直接读本地文件。淘汰按访问时间做 LRU。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from app.paths import AUDIO_CACHE_DIR

_EXT_BY_CODEC = {
    "m4a": "m4a",
    "aac": "m4a",
    "mp4a.40.2": "m4a",
    "flac": "flac",
    "ec3": "ec3",
}


def _safe_name(key: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in key)


def audio_path(key: str, ext: str = "m4a") -> Path:
    ext = _EXT_BY_CODEC.get(ext.lower(), ext.lower() or "m4a")
    return AUDIO_CACHE_DIR / f"{_safe_name(key)}.{ext}"


def find_existing(key: str) -> Path | None:
    """不关心扩展名，找到就算命中。"""
    base = _safe_name(key)
    for p in AUDIO_CACHE_DIR.glob(f"{base}.*"):
        if p.is_file() and p.stat().st_size > 1024:
            return p
    return None


def touch(path: Path) -> None:
    try:
        os.utime(path, (time.time(), time.time()))
    except OSError:
        pass


def total_size() -> int:
    total = 0
    for p in AUDIO_CACHE_DIR.glob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def evict(limit_bytes: int) -> int:
    """超出上限时按最久未访问淘汰，返回删除的字节数。"""
    files: list[tuple[float, int, Path]] = []
    for p in AUDIO_CACHE_DIR.glob("*"):
        try:
            if not p.is_file():
                continue
            st = p.stat()
            files.append((st.st_atime, st.st_size, p))
        except OSError:
            continue

    total = sum(f[1] for f in files)
    if total <= limit_bytes:
        return 0

    files.sort(key=lambda x: x[0])
    removed = 0
    for _atime, size, path in files:
        if total <= limit_bytes:
            break
        try:
            path.unlink()
            total -= size
            removed += size
        except OSError:
            continue
    return removed


def clear_all() -> int:
    removed = 0
    for p in AUDIO_CACHE_DIR.glob("*"):
        try:
            if p.is_file():
                removed += p.stat().st_size
                p.unlink()
        except OSError:
            continue
    return removed
