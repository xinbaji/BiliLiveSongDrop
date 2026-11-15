"""统一日志。

控制台给人看（短、带耗时），文件给排查用（完整、带线程名）。
关键路径统一用 Timer 包一下，任何一步变慢都能立刻看出是哪一段。
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
import time
from typing import Any

from app.paths import LOG_DIR

_READY = False
_FILE_HANDLER: logging.Handler | None = None


class _ShortFormatter(logging.Formatter):
    """控制台格式：20:25:31.123 [engine] 消息"""

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        ms = int(record.msecs)
        return f"{ts}.{ms:03d} [{record.name}] {record.getMessage()}"


class _FileFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        ms = int(record.msecs)
        tid = record.threadName or "-"
        return (
            f"{ts}.{ms:03d} [{record.levelname:7}] [{tid:16}] "
            f"{record.name}: {record.getMessage()}"
        )


def setup(level: int = logging.INFO, to_file: bool = True) -> None:
    global _READY, _FILE_HANDLER
    if _READY:
        return

    root = logging.getLogger("bili")
    root.setLevel(logging.DEBUG)
    root.propagate = False
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(_ShortFormatter())
    root.addHandler(console)

    if to_file:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                LOG_DIR / "app.log",
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(_FileFormatter())
            root.addHandler(fh)
            _FILE_HANDLER = fh
        except OSError as exc:
            root.warning("日志文件不可用：%s", exc)

    # aiohttp 的访问日志太吵，只在文件里留 warning 以上
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    _READY = True


def get(name: str) -> logging.Logger:
    """按模块取 logger，例如 get('engine') → bili.engine。"""
    if not _READY:
        setup()
    return logging.getLogger(f"bili.{name}")


class Timer:
    """with Timer(log, '解析音频'): ... 结束时打印毫秒数。"""

    __slots__ = ("_log", "_label", "_start", "_extra", "_level")

    def __init__(self, logger: logging.Logger, label: str,
                 level: int = logging.DEBUG, **extra: Any) -> None:
        self._log = logger
        self._label = label
        self._start = 0.0
        self._extra = extra
        self._level = level

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000

    def __exit__(self, exc_type, exc, tb) -> bool:
        ms = self.elapsed_ms
        suffix = "".join(f" {k}={v}" for k, v in self._extra.items())
        if exc is not None:
            self._log.log(self._level, "%s 失败 %.0fms%s err=%s",
                          self._label, ms, suffix, exc)
        elif ms >= 300:
            self._log.info("%s 完成 %.0fms%s", self._label, ms, suffix)
        else:
            self._log.log(self._level, "%s 完成 %.0fms%s", self._label, ms, suffix)
        return False
