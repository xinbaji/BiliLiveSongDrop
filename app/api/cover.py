"""封面图代理。

前端直接用 <img src="https://i0.hdslb.com/..."> 在某些网络环境下会拿不到
（WebView2 的代理/证书链和 Python 侧不一致），表现是 naturalWidth=0 但也不报错。

统一走本地代理：后端带正确的 Referer 取图、落盘缓存、回正确的 image/* 类型。
同时把请求收敛到白名单域名，避免被当成开放代理。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

from app.paths import COVER_CACHE_DIR
from app.sources.bilibili.client import REFERER as BILI_REFERER, UA

# 只允许这些域名的图片走代理
_ALLOWED_HOSTS = (
    "hdslb.com",          # B 站图床（含 i0/i1/i2 等子域）
    "bilibili.com",
    "music.126.net",      # 网易云封面
    "126.net",
)

_PASS_HEADERS = ("Content-Type", "Content-Length", "ETag", "Last-Modified")


def is_allowed(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    return any(host == h or host.endswith("." + h) or host.endswith(h) for h in _ALLOWED_HOSTS)


def _cache_path(url: str) -> Path:
    return COVER_CACHE_DIR / (hashlib.md5(url.encode("utf-8")).hexdigest() + ".img")


def _mime_from_magic(head: bytes) -> str:
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


class CoverProxy:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                trust_env=True,
                timeout=aiohttp.ClientTimeout(total=15, connect=8),
                headers={
                    "User-Agent": UA,
                    "Referer": BILI_REFERER,
                    "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        from app.log import get

        log = get("cover")
        raw = request.query.get("u", "")
        if not raw:
            raise web.HTTPBadRequest(text="missing u")
        if not is_allowed(raw):
            log.warning("拒绝非白名单封面域名 %s", raw[:80])
            raise web.HTTPForbidden(text="host not allowed")

        cached = _cache_path(raw)
        if cached.exists() and cached.stat().st_size > 512:
            with open(cached, "rb") as f:
                head = f.read(16)
            return web.FileResponse(
                path=cached,
                headers={
                    "Content-Type": _mime_from_magic(head),
                    "Cache-Control": "public, max-age=86400",
                },
            )

        try:
            session = await self._get_session()
            async with session.get(raw) as upstream:
                if upstream.status != 200:
                    log.warning("封面上游 %s 返回 %s", raw[:70], upstream.status)
                    raise web.HTTPBadGateway(text=f"upstream {upstream.status}")
                body = await upstream.read()
                upstream_type = upstream.headers.get("Content-Type", "")
        except (aiohttp.ClientError, OSError) as exc:
            log.warning("封面拉取失败 %s err=%s", raw[:70], exc)
            raise web.HTTPBadGateway(text="fetch failed") from exc

        if len(body) < 512:
            raise web.HTTPBadGateway(text="empty image")

        mime = _mime_from_magic(body[:16]) if not upstream_type.startswith("image/") else upstream_type

        tmp = cached.with_suffix(".tmp")
        try:
            tmp.write_bytes(body)
            tmp.replace(cached)
        except OSError:
            pass

        log.debug("封面代理 %s %d bytes %s", raw[-46:], len(body), mime)
        return web.Response(
            body=body,
            headers={"Content-Type": mime, "Cache-Control": "public, max-age=86400"},
        )
