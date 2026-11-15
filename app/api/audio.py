"""音频代理。

为什么必须代理而不能让前端直接播放：
- B 站音频带 `Referer` 校验，<audio src> 无法设置请求头；
- 直链有时效（约 2 小时），必须在播放前现取；
- 本地缓存命中时直接给文件，彻底绕开上面两条。

Range 支持是硬要求：没有 206，进度条拖动和 seek 全部失效。
"""
from __future__ import annotations

import asyncio
import mimetypes
import re
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from app.player import cache
from app.store.models import Song

CHUNK = 64 * 1024
_RANGE_FROM_START = re.compile(r"bytes=0-\s*")


def _expected_total(headers: dict) -> int:
    """从 Content-Range 或 Content-Length 推断文件总长。"""
    cr = headers.get("Content-Range", "")
    if "/" in cr:
        try:
            return int(cr.rsplit("/", 1)[1])
        except (ValueError, IndexError):
            pass
    try:
        return int(headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        return 0
PASS_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Content-Encoding")


# 按后缀硬编码：Python 的 mimetypes 在 Windows 上对 .m4a 常常给不出结果，
# 一旦落到 application/octet-stream，<audio> 会直接拒绝解码，表现就是「没声音」。
_MIME_BY_SUFFIX = {
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".ec3": "audio/eac3",
}


def mime_by_suffix(path: Path) -> str:
    return _MIME_BY_SUFFIX.get(path.suffix.lower(), "audio/mp4")


def _guess_mime(path: Path | None, upstream: str = "") -> str:
    if upstream and upstream.startswith("audio/"):
        return upstream
    if path:
        guessed, _ = mimetypes.guess_type(str(path))
        if guessed:
            return guessed
    return "audio/mp4"


def detect_mime(head: bytes, upstream_type: str = "") -> str:
    """按魔数判断真实音频类型。

    网易云对 mp3 会返回 `application/octet-stream`，浏览器拿到这个类型
    可能直接拒绝播放，所以必须自己嗅探后覆盖。
    """
    up = (upstream_type or "").split(";")[0].strip().lower()
    if up.startswith("audio/"):
        return up

    if head[:3] == b"ID3":
        return "audio/mpeg"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "audio/mpeg"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "audio/mp4"
    if head[:4] == b"fLaC":
        return "audio/flac"
    if head[:4] == b"OggS":
        return "audio/ogg"
    if up and up != "application/octet-stream":
        return up
    return "audio/mpeg"


class AudioProxy:
    def __init__(self, engine, resolver) -> None:
        self.engine = engine
        self.resolver = resolver
        self._session: aiohttp.ClientSession | None = None
        self._writing: set[str] = set()  # 正在写缓存的 key，防止并发重复写

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                trust_env=True,
                timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_read=60),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        from app.log import get

        log = get("audio")
        key = request.match_info.get("key", "")
        if not key:
            raise web.HTTPBadRequest(text="missing key")

        rng = request.headers.get("Range", "")

        # 1) 缓存命中：直接给文件，FileResponse 自带 Range 支持
        local = cache.find_existing(key)
        if local:
            cache.touch(local)
            mime = mime_by_suffix(local)
            log.debug("命中缓存 %s range=%s type=%s", key, rng or "-", mime)
            return web.FileResponse(
                path=local,
                headers={"Cache-Control": "no-cache", "Content-Type": mime},
                status=200,
            )

        # 2) 查已解析结果，避免重复请求上游
        resolved = self.engine.get_resolved(key)
        song: Song | None = None

        if resolved is None:
            song = self._song_from_key(key)
            if song is None:
                log.warning("未知音源 key=%s", key)
                raise web.HTTPNotFound(text="unknown track")

            t0 = time.perf_counter()
            resolved = await self.resolver.resolve(song)
            cost = (time.perf_counter() - t0) * 1000

            if not resolved.ok:
                log.warning("解析失败 %s %.0fms note=%s", key, cost, resolved.note)
                raise web.HTTPBadGateway(text=resolved.note or "audio unavailable")

            log.info("解析 %s %.0fms 质量=%s 换源=%s",
                     key, cost, resolved.quality or "-", resolved.fallback)
            self.engine.put_resolved(key, resolved)
        else:
            log.debug("复用解析结果 %s", key)

        return await self._stream_upstream(request, resolved, cache_key=key)

    def _song_from_key(self, key: str) -> Song | None:
        from app.store import repo

        if key.startswith("netease_"):
            source, rest = "netease", key[len("netease_"):]
        elif key.startswith("bilibili_"):
            source, rest = "bilibili", key[len("bilibili_"):]
        else:
            return None
        if "_" in rest:
            sid, extra = rest.split("_", 1)
        else:
            sid, extra = rest, ""
        found = repo.find_song(source, sid, extra)
        if found:
            return found
        # 换源产生的临时音源可能还没入库，现造一个
        return Song(source=source, source_id=sid, extra=extra, title="")

    async def _stream_upstream(self, request: web.Request, resolved,
                               cache_key: str = "") -> web.StreamResponse:
        from app.log import get

        log = get("audio")
        upstream_headers = dict(resolved.headers or {})
        rng = request.headers.get("Range")
        if rng:
            upstream_headers["Range"] = rng

        session = await self._get_session()
        async with session.get(resolved.url, headers=upstream_headers) as upstream:
            if upstream.status not in (200, 206):
                raise web.HTTPBadGateway(text=f"upstream {upstream.status}")

            # 先读一点点判断真实格式，否则 Content-Type 是 octet-stream 时 <audio> 拒播
            head = await upstream.content.read(16)

            resp_headers = {}
            for name in PASS_HEADERS:
                value = upstream.headers.get(name)
                if value:
                    resp_headers[name] = value
            resp_headers["Content-Type"] = detect_mime(
                head, upstream.headers.get("Content-Type", "")
            )
            if "Accept-Ranges" not in resp_headers:
                resp_headers["Accept-Ranges"] = "bytes"
            resp_headers.setdefault("Cache-Control", "no-cache")

            # 边转发边落盘：只有「从头开始的完整流」才值得缓存，
            # 中途 seek 产生的片段写进去会得到残缺文件。
            sink = self._open_cache_sink(cache_key, rng, resp_headers)

            resp = web.StreamResponse(status=upstream.status, headers=resp_headers)
            await resp.prepare(request)
            written = 0
            try:
                if head:
                    await resp.write(head)
                    written += self._sink_write(sink, head)
                async for chunk in upstream.content.iter_chunked(CHUNK):
                    await resp.write(chunk)
                    written += self._sink_write(sink, chunk)
            except (aiohttp.ClientError, asyncio.CancelledError, OSError):
                # 浏览器切歌/暂停会直接断开连接，ConnectionError 是内置 OSError 的子类，
                # 不是 aiohttp.ClientError，漏掉它会每切一次歌就打一份 traceback
                pass
            finally:
                self._close_cache_sink(sink, written, log, cache_key)
            try:
                await resp.write_eof()
            except (aiohttp.ClientError, OSError):
                pass
            return resp

    # ── 播放即缓存 ────────────────────────────────────────────────────

    def _open_cache_sink(self, key: str, rng: str | None, headers: dict):
        # 只有「从头开始的开放范围」能缓存完整文件；bytes=0-65535 这种是片段，写进去会残缺
        if not key:
            return None
        if rng and not _RANGE_FROM_START.fullmatch(rng.strip()):
            return None
        if cache.find_existing(key):
            return None
        if key in self._writing:
            return None
        path = cache.audio_path(key)
        part = path.with_suffix(path.suffix + ".part")
        try:
            fp = open(part, "wb")
        except OSError:
            return None
        self._writing.add(key)
        return {"key": key, "file": fp, "part": part, "final": path,
                "expect": _expected_total(headers)}

    @staticmethod
    def _sink_write(sink, chunk: bytes) -> int:
        if not sink:
            return 0
        try:
            sink["file"].write(chunk)
            return len(chunk)
        except OSError:
            return 0

    def _close_cache_sink(self, sink, written: int, log, key: str) -> None:
        if not sink:
            return
        self._writing.discard(sink["key"])
        try:
            sink["file"].close()
        except OSError:
            pass
        # 必须字节数吻合才算完整，否则用户中途切歌会留下半截文件
        expect = sink.get("expect") or 0
        complete = written > 64 * 1024 and (expect == 0 or written >= expect)
        if complete:
            try:
                sink["part"].replace(sink["final"])
                log.info("播放即缓存 %s %.1fMB", key, written / 1024 / 1024)
                return
            except OSError:
                pass
        try:
            sink["part"].unlink()
        except OSError:
            pass
        log.debug("放弃缓存 %s 收到 %d / 期望 %d 字节", key, written, sink.get("expect") or 0)
