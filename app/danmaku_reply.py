"""往直播间发弹幕的提示服务。

为什么不直接调一次发一次：
B 站发弹幕有频率风控，连发会被静默拒绝，拒绝后短时间内很难恢复。
所以所有发送都进一个串行队列，由单个 worker 按最小间隔逐条发出，
失败后指数退避，避免连续失败把账号打进小黑屋。
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from app.log import get


class DanmakuReplier:
    # 队列短一点：直播场景里过期的提示没有发送价值，宁可丢掉
    MAX_QUEUE = 16

    def __init__(self, ctx, hub) -> None:
        self.ctx = ctx
        self.hub = hub
        self._queue: asyncio.Queue[str] | None = None
        self._task: asyncio.Task | None = None
        self._last_sent = 0.0
        self._fails = 0
        self._blocked_until = 0.0
        self.sent = 0
        self.dropped = 0
        self.last_error = ""

    # ── 可用性 ────────────────────────────────────────────────────────

    @property
    def cfg(self):
        return self.ctx.config.danmaku_reply

    def available(self) -> bool:
        """总开关、房间号、B 站登录态三者齐备才发得出去。"""
        return bool(
            self.cfg.enabled
            and int(self.ctx.config.room_id or 0)
            and self.ctx.bilibili.is_logged_in()
        )

    def unavailable_reason(self) -> str:
        if not self.cfg.enabled:
            return "弹幕提示未开启"
        if not int(self.ctx.config.room_id or 0):
            return "还没设置直播间房间号"
        if not self.ctx.bilibili.is_logged_in():
            return "B 站未登录"
        return ""

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def start(self) -> None:
        self._queue = asyncio.Queue(maxsize=self.MAX_QUEUE)
        self._task = asyncio.create_task(self._worker(), name="danmaku-replier")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    # ── 发送 ──────────────────────────────────────────────────────────

    def say(self, text: str) -> bool:
        """非阻塞投递。队列满了就丢，不阻塞弹幕处理线程。"""
        msg = (text or "").strip()
        if not msg:
            return False
        if not self.available():
            return False
        if self._queue is None:
            return False
        try:
            self._queue.put_nowait(msg)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            get("reply").debug("弹幕队列已满，丢弃 %r", msg[:20])
            return False

    def fmt(self, template: str, **kw: Any) -> str:
        """填充模板。模板写错（比如 {title 少了括号）不能让整个播报挂掉。"""
        try:
            return str(template or "").format(**kw)
        except (KeyError, IndexError, ValueError):
            return str(template or "")

    async def _worker(self) -> None:
        log = get("reply")
        assert self._queue is not None
        while True:
            text = await self._queue.get()
            try:
                await self._send_one(text, log)
            finally:
                self._queue.task_done()

    async def _send_one(self, text: str, log) -> None:
        room = int(self.ctx.config.room_id or 0)
        if not room:
            return

        # 退避期还没过，直接等
        wait = self._blocked_until - time.time()
        if wait > 0:
            log.debug("退避中，等待 %.1fs", wait)
            await asyncio.sleep(wait)

        # 两条之间保持最小间隔
        gap = float(self.cfg.min_interval or 0) - (time.time() - self._last_sent)
        if gap > 0:
            await asyncio.sleep(gap)

        try:
            await self.ctx.bilibili.send_danmaku(room, text)
        except Exception as exc:  # noqa: BLE001
            self._fails += 1
            self.last_error = str(exc)
            backoff = min(60.0, 5.0 * (2 ** max(0, self._fails - 1)))
            self._blocked_until = time.time() + backoff
            log.warning("发弹幕失败（第 %d 次，退避 %.0fs）：%s | %r",
                        self._fails, backoff, exc, text[:24])
            self.hub.emit({"event": "log", "entry": {
                "user": "弹幕提示", "text": f"发送失败：{exc}", "kind": "fail",
            }})
            return

        self._fails = 0
        self._last_sent = time.time()
        self.sent += 1
        log.info("已发弹幕 %r", text)
        self.hub.emit({"event": "log", "entry": {
            "user": "弹幕提示", "text": text, "kind": "ok",
        }})

    # ── 匹配 ──────────────────────────────────────────────────────────

    def match_rule(self, text: str) -> str:
        """按关键词命中自动回复。返回要发的内容，没命中返回空串。"""
        raw = (text or "").strip()
        if not raw:
            return ""
        for rule in self.cfg.rules or []:
            kw = str(rule.get("keyword") or "").strip()
            if kw and kw in raw:
                return str(rule.get("reply") or "").strip()
        return ""

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.cfg.enabled),
            "available": self.available(),
            "reason": self.unavailable_reason(),
            "sent": self.sent,
            "dropped": self.dropped,
            "queued": self._queue.qsize() if self._queue else 0,
            "last_error": self.last_error,
        }
