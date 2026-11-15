"""B 站直播间弹幕客户端。

直接连 `broadcastlv.chat.bilibili.com`，不依赖第三方库。
流程：getDanmuInfo 拿 token + host → WS 连接 → 发认证包 → 30 秒心跳 → 解包分发。

包结构（16 字节头）：
  0-3   总长度   4-5 头长度(=16)   6-7 协议版本   8-11 操作码   12-15 序号
协议版本 0 明文 / 1 心跳 / 2 zlib / 3 brotli。一个包解压后可能又是一串包，要递归拆。
"""
from __future__ import annotations

import asyncio
import json
import struct
import time
import zlib
from typing import Any, Awaitable, Callable

import aiohttp
import brotli

from app.sources.bilibili.client import REFERER, UA

HEADER = struct.Struct(">IHHII")
OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_MESSAGE = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8

PROTO_PLAIN = 0
PROTO_HEARTBEAT = 1
PROTO_ZLIB = 2
PROTO_BROTLI = 3

Handler = Callable[[dict[str, Any]], Awaitable[None] | None]


def _decode_body(proto: int, body: bytes) -> list[bytes]:
    """按协议版本解压。返回可能是一条或多条子包。"""
    if proto == PROTO_ZLIB:
        return [zlib.decompress(body)]
    if proto == PROTO_BROTLI:
        return [brotli.decompress(body)]
    return [body]


def _split_packets(raw: bytes) -> list[tuple[int, int, bytes]]:
    """把一段字节拆成 (操作码, 协议版本, body) 列表，递归处理嵌套压缩包。"""
    out: list[tuple[int, int, bytes]] = []
    offset = 0
    length = len(raw)
    while offset + 16 <= length:
        total, head_len, proto, op, _ = HEADER.unpack_from(raw, offset)
        if total < 16 or offset + total > length:
            break
        body = raw[offset + head_len:offset + total]
        if op == OP_MESSAGE and proto in (PROTO_ZLIB, PROTO_BROTLI):
            for inner in _decode_body(proto, body):
                out.extend(_split_packets(inner))
        else:
            out.append((op, proto, body))
        offset += total
    return out


def parse_danmaku(info: list[Any]) -> dict[str, Any]:
    """从 DANMU_MSG 的 info 数组里抠出需要的东西。结构是位置耦合的，只能按索引取。"""
    text = ""
    uid = 0
    uname = ""
    is_admin = False
    if len(info) > 1 and isinstance(info[1], str):
        text = info[1]
    if len(info) > 2 and isinstance(info[2], list):
        user = info[2]
        if len(user) > 0:
            uid = int(user[0] or 0)
        if len(user) > 1:
            uname = str(user[1] or "")
        if len(user) > 2:
            is_admin = bool(user[2])

    guard_level = 0
    if len(info) > 7:
        try:
            guard_level = int(info[7] or 0)
        except (TypeError, ValueError):
            guard_level = 0

    medal_level = 0
    medal_name = ""
    if len(info) > 3 and isinstance(info[3], list) and info[3]:
        try:
            medal_level = int(info[3][0] or 0)
        except (TypeError, ValueError):
            medal_level = 0
        if len(info[3]) > 1:
            medal_name = str(info[3][1] or "")

    return {
        "type": "danmaku",
        "uid": uid,
        "uname": uname,
        "text": text,
        "guard_level": guard_level,
        "is_admin": is_admin,
        "medal_level": medal_level,
        "medal_name": medal_name,
        "timestamp": int(time.time()),
    }


class DanmakuClient:
    """单房间弹幕连接，带自动重连。

    token 与节点列表由外部 provider 提供（需要 WBI 签名，放在 BilibiliClient 里做），
    这样弹幕模块只管 WS 生命周期，不与接口层耦合。
    """

    def __init__(
        self,
        room_id: int,
        info_provider: Callable[[], Awaitable[tuple[str, list[dict[str, Any]]]]],
        buvid: str = "",
        uid: int = 0,
    ) -> None:
        self.room_id = room_id
        self._info_provider = info_provider
        self.buvid = buvid
        self.uid = uid
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self.on_message: Handler | None = None
        self.on_status: Callable[[str, str], None] | None = None

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def start(self, on_message: Handler, on_status: Callable[[str, str], None] | None = None) -> None:
        if self._running:
            return
        self.on_message = on_message
        self.on_status = on_status
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _status(self, state: str, detail: str = "") -> None:
        if self.on_status:
            try:
                self.on_status(state, detail)
            except Exception:
                pass

    # ── 连接 ──────────────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                trust_env=True,
                timeout=aiohttp.ClientTimeout(total=None, connect=10),
                headers={"User-Agent": UA, "Referer": REFERER, "Origin": REFERER},
            )
        return self._session

    async def _danmu_info(self) -> tuple[str, list[dict[str, Any]]]:
        return await self._info_provider()

    async def _loop(self) -> None:
        backoff = 3
        while self._running:
            try:
                await self._connect_once()
                # 连接正常结束（服务端主动断开）也必须退避。
                # 不加这句的话，连上就断会变成不做间隔的重连风暴——
                # 实测能达到每秒数次，几秒钟就把 B 站风控打穿，
                # 之后 getDanmuInfo 一律返回 -352，而且越重连越出不来。
                # 状态已由 _connect_once 结尾报过，这里不再重复报一次
                if not self._running:
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._status("error", f"{type(exc).__name__}: {exc}")
                if not self._running:
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _connect_once(self) -> None:
        token, hosts = await self._danmu_info()
        # 优先选支持 wss 的节点
        hosts = sorted(hosts, key=lambda h: 0 if h.get("wss_port") else 1)
        host = hosts[0]
        url = f"wss://{host['host']}:{host.get('wss_port') or 443}/sub"

        session = await self._get_session()
        self._status("connecting", url)
        async with session.ws_connect(url, heartbeat=None, max_msg_size=0) as ws:
            auth = {
                # 必须用真实 uid。连接会话里带着 SESSDATA，却在 auth 包里声明
                # uid=0（匿名），服务端会认为身份不一致，直接关连接：
                # 表现就是 connected 之后立刻 disconnected，永远等不到 AUTH_REPLY。
                "uid": int(self.uid or 0),
                "roomid": self.room_id,
                "protover": 3,       # 3 = brotli，体积最小
                "platform": "web",
                "type": 2,
                "key": token,
            }
            if self.buvid:
                auth["buvid"] = self.buvid
            await ws.send_bytes(self._pack(OP_AUTH, json.dumps(auth).encode("utf-8")))
            self._status("connected", "")

            heartbeat = asyncio.create_task(self._heartbeat(ws))
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        await self._handle(raw=msg.data)
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle(raw=msg.data.encode("utf-8"))
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except (asyncio.CancelledError, Exception):
                    pass
        self._status("disconnected", "")

    async def _heartbeat(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while not ws.closed:
            try:
                await ws.send_bytes(self._pack(OP_HEARTBEAT, b"[object Object]"))
            except Exception:
                return
            await asyncio.sleep(30)

    @staticmethod
    def _pack(op: int, body: bytes) -> bytes:
        return HEADER.pack(16 + len(body), 16, PROTO_HEARTBEAT, op, 1) + body

    async def _handle(self, raw: bytes) -> None:
        for op, _proto, body in _split_packets(raw):
            if op == OP_AUTH_REPLY:
                self._status("authed", "")
                continue
            if op == OP_HEARTBEAT_REPLY:
                continue
            if op != OP_MESSAGE:
                continue
            try:
                payload = json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            cmd = str(payload.get("cmd") or "")
            if cmd.startswith("DANMU_MSG"):
                info = payload.get("info") or []
                if not isinstance(info, list):
                    continue
                event = parse_danmaku(info)
                if event["text"] and self.on_message:
                    result = self.on_message(event)
                    if asyncio.iscoroutine(result):
                        await result
