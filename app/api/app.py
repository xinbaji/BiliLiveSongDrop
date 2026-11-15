"""aiohttp 应用装配。"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from aiohttp import web

from app.api.audio import AudioProxy
from app.api.cover import CoverProxy
from app.api.hub import WSHub
from app.api.routes import Api
from app.danmaku_reply import DanmakuReplier
from app.paths import gui_dir


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """允许本地文件页面（Origin 为 null 或 file://）跨域访问。

    WebView2 下 file:// 页面的 Origin 可能是 "file://" 而不是 "null"，
    直接把 file:// 回显回去是非法值，浏览器会整条拒掉，所以统一归一到 null。
    """
    if request.method == "OPTIONS":
        resp: web.StreamResponse = web.Response(status=204)
    else:
        resp = await handler(request)

    origin = request.headers.get("Origin")
    if origin is None:
        allow = "*"
    elif origin == "null" or origin.startswith("file:"):
        allow = "null"
    else:
        allow = origin

    resp.headers["Access-Control-Allow-Origin"] = allow
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Max-Age"] = "600"
    if allow != "*":
        resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers["Vary"] = "Origin"
    return resp


@web.middleware
async def method_log_middleware(request: web.Request, handler):
    """方法不对时把路径打出来，否则 405 完全无法定位。"""
    try:
        return await handler(request)
    except web.HTTPMethodNotAllowed as exc:
        print(f"[405] {request.method} {request.path} → 允许: {exc.allowed_methods}", flush=True)
        raise
    except web.HTTPNotFound:
        if not request.path.startswith(("/static", "/css", "/js", "/favicon")):
            print(f"[404] {request.method} {request.path}", flush=True)
        raise


_SKIP_TIMING = ("/api/audio", "/static", "/css", "/js", "/ws", "/favicon")


@web.middleware
async def timing_middleware(request: web.Request, handler):
    """记录请求耗时。音频流和静态资源跳过，否则日志会被刷屏。"""
    from app.log import get

    t0 = time.perf_counter()
    try:
        resp = await handler(request)
        return resp
    finally:
        path = request.path
        if not path.startswith(_SKIP_TIMING):
            ms = (time.perf_counter() - t0) * 1000
            log = get("http")
            if ms >= 200:
                log.warning("慢请求 %s %s %.0fms", request.method, path, ms)
            else:
                log.debug("%s %s %.0fms", request.method, path, ms)


def create_app(ctx) -> web.Application:
    """ctx 需要提供：engine / resolver / netease / bilibili / config / port"""
    app = web.Application(
        middlewares=[cors_middleware, timing_middleware, method_log_middleware],
        client_max_size=8 * 1024 * 1024,
    )
    hub = WSHub()
    hub.bind_loop(asyncio.get_running_loop())

    api = Api(ctx.engine, ctx.resolver, ctx.netease, ctx.bilibili, hub, ctx.config)
    api.web_login_cb = getattr(ctx, "web_login", None)
    api.restart_cb = getattr(ctx, "restart", None)
    api.register(app)

    # 引擎状态变化 → 推给所有 WS 客户端
    ctx.engine.subscribe(hub.emit)

    # 弹幕提示：串行发送器，弹幕服务、入队、切歌三处共用同一个实例
    replier = DanmakuReplier(ctx, hub)
    app["replier"] = replier
    api.replier = replier

    proxy = AudioProxy(ctx.engine, ctx.resolver)
    cover = CoverProxy()
    app["proxy"] = proxy
    app["cover"] = cover
    app["hub"] = hub
    app["api"] = api

    from app.danmaku_service import DanmakuService

    danmaku = DanmakuService(ctx, hub, api)
    danmaku.replier = replier
    app["danmaku"] = danmaku

    if getattr(ctx, "debug", False):
        async def debug_danmaku(request: web.Request) -> web.Response:
            body = await request.json()
            await danmaku.simulate({
                "uid": int(body.get("uid") or 99999),
                "uname": str(body.get("uname") or "调试观众"),
                "text": str(body.get("text") or ""),
                "guard_level": int(body.get("guard_level") or 0),
                "is_admin": bool(body.get("is_admin") or False),
                "medal_level": 0,
                "medal_name": "",
                "timestamp": 0,
            })
            return web.json_response({"ok": True})

        app.router.add_post("/api/_debug/danmaku", debug_danmaku)

    announce_state = {"booted": False}

    def _announce_playing(payload: dict) -> None:
        """切歌时播报当前曲目。只处理 track_change，状态变化不重复播。"""
        if payload.get("event") != "track_change":
            return

        # 启动时引擎会主动起播一首，那一次不算「切歌」。
        # 不跳过的话，每次重启程序都会往直播间刷一条「正在播放」。
        if not announce_state["booted"]:
            announce_state["booted"] = True
            return
        track = payload.get("track") or {}
        title = track.get("title") or ""
        if not title or not replier.cfg.announce_playing:
            return
        text = replier.fmt(
            replier.cfg.playing_template,
            title=title, artist=track.get("artists") or "",
            source="网易云" if track.get("source") == "netease" else "B站",
        )
        replier.say(text)

    ctx.engine.subscribe(_announce_playing)

    async def _startup(app_: web.Application) -> None:
        await replier.start()
        await danmaku.start()

    async def _cleanup(app_: web.Application) -> None:
        await danmaku.stop()
        await replier.stop()
        await proxy.close()
        await cover.close()

    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=0)
        await ws.prepare(request)
        await hub.register(ws)
        # 首帧给完整状态，让叠加层和主界面立刻对齐
        await ws.send_str(json.dumps(
            {"event": "state", **ctx.engine.state.to_dict()}, ensure_ascii=False
        ))
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") == "position":
                        ctx.engine.report_position(
                            int(data.get("position") or 0),
                            int(data.get("duration") or 0),
                        )
                elif msg.type == web.WSMsgType.ERROR:
                    break
        finally:
            await hub.unregister(ws)
        return ws

    async def audio_handler(request: web.Request) -> web.StreamResponse:
        return await proxy.handle(request)

    async def cover_handler(request: web.Request) -> web.StreamResponse:
        return await cover.handle(request)

    async def index(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(gui_dir() / "index.html")

    async def overlay(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(gui_dir() / "overlay.html")

    async def favicon(request: web.Request) -> web.Response:
        return web.Response(status=204)

    async def healthz(request: web.Request) -> web.Response:
        # app 字段用于启动时识别「这个端口是不是自己的旧实例」
        return web.json_response({"ok": True, "app": "bili-singer", "port": ctx.port})

    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/audio/{key}", audio_handler)
    app.router.add_get("/api/cover", cover_handler)
    app.router.add_get("/", index)
    app.router.add_get("/overlay", overlay)
    app.router.add_get("/overlay.html", overlay)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/favicon.ico", favicon)

    # HTML 里用的是相对路径（css/app.css、js/core.js），所以这两个目录必须挂在根上。
    # 只挂 /static 的话，从 http:// 打开时会 404，界面直接变成裸 HTML。
    app.router.add_static("/css", gui_dir() / "css", show_index=False)
    app.router.add_static("/js", gui_dir() / "js", show_index=False)
    app.router.add_static("/static", gui_dir(), show_index=False, follow_symlinks=False)

    # 通配 OPTIONS 必须最后注册：跨域 PUT/DELETE 的预检要能落到这里，
    # 否则 aiohttp 在路由解析阶段就抛 405，CORS 中间件根本没机会执行。
    async def options_handler(request: web.Request) -> web.Response:
        return web.Response(status=204)

    app.router.add_route("OPTIONS", "/{tail:.*}", options_handler)

    return app
