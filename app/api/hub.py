"""WebSocket 广播中心。

GUI 主界面和 OBS 叠加层都订阅同一条广播流，谁连上都能拿到完整状态。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import web


class WSHub:
    def __init__(self) -> None:
        self._clients: set[web.WebSocketResponse] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def register(self, ws: web.WebSocketResponse) -> None:
        self._clients.add(ws)

    async def unregister(self, ws: web.WebSocketResponse) -> None:
        self._clients.discard(ws)

    @property
    def count(self) -> int:
        return len(self._clients)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        if not self._clients:
            return
        text = json.dumps(payload, ensure_ascii=False)
        dead: list[web.WebSocketResponse] = []
        for ws in list(self._clients):
            try:
                await ws.send_str(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    def emit(self, payload: dict[str, Any]) -> None:
        """同步接口，给引擎的回调直接用。"""
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self.broadcast(payload))
            )
        except RuntimeError:
            pass

    async def close_all(self) -> None:
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()
