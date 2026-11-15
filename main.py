"""B 站点歌机入口。

线程模型：
- 主线程跑 pywebview（WinForms 消息循环，必须占主线程）；
- 后台线程跑一条独立的 asyncio 事件循环，承载 aiohttp 服务、弹幕 WS、播放引擎。
两边通过 HTTP + WebSocket 通信，不共享状态对象。
"""
from __future__ import annotations

import asyncio
import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from app import __version__
from app.api.app import create_app
from app.config import AppConfig, load_config, save_config
from app.net import resolve_port
from app.paths import DATA_DIR, ensure_dirs, gui_dir, migrate_legacy_data
from app.player.engine import PlayerEngine
from app.player.resolver import AudioResolver
from app.sources.bilibili.client import BilibiliClient
from app.sources.netease.client import NeteaseClient
from app.store import repo
from app.store.db import close_db, init_db

LOGIN_URLS = {
    "netease": "https://music.163.com/#/login",
    "bilibili": "https://passport.bilibili.com/login",
}
LOGIN_COOKIE_KEYS = {
    "netease": "MUSIC_U",
    "bilibili": "SESSDATA",
}


def _spawn_restart(ctx: "AppContext") -> None:
    """重启：拉起一个新进程，然后退出自己。

    端口只能在进程级别更换，热改没有意义。新进程启动时会检测端口占用，
    认出是自己的旧实例就接管；这里先保证新进程能起来，再退出，避免两个都死。
    """
    import subprocess

    from app.log import get

    log = get("main")
    script = Path(__file__).resolve()
    argv = [sys.executable, str(script)]
    if ctx.port:
        argv += ["--port", str(ctx.port)]
    if getattr(ctx, "debug", False):
        argv.append("--debug")
    if getattr(ctx, "no_window", False):
        argv.append("--no-window")

    log.info("重启服务：%s", " ".join(argv[1:]))
    try:
        subprocess.Popen(
            argv,
            cwd=str(script.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：脱离当前控制台，
            # 否则父进程退出会把子进程一起带走
            creationflags=0x00000008 | 0x00000200,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("重启失败：%s", exc)
        return

    os._exit(0)


class AppContext:
    def __init__(self, config: AppConfig, port: int) -> None:
        self.config = config
        self.port = port
        self.loop: asyncio.AbstractEventLoop | None = None

        ne = repo.auth_get("netease")
        bl = repo.auth_get("bilibili")
        self.netease = NeteaseClient(ne["cookie"] if ne else "")
        self.bilibili = BilibiliClient(bl["cookie"] if bl else "", uid=config.bili_uid)
        self.resolver = AudioResolver(
            self.netease, self.bilibili,
            quality=config.audio_quality,
            fallback_to_bili=config.fallback_to_bili,
        )
        self.engine = PlayerEngine(config, self.resolver)
        self.web_login = None


class NativeBridge:
    """暴露给前端 window.pywebview.api 的原生能力。

    业务数据全走 HTTP，这里只做浏览器做不到的事：弹真实登录窗口。
    """

    def __init__(self) -> None:
        self.ctx: AppContext | None = None

    def version(self) -> str:
        return __version__

    def port(self) -> int:
        """页面是 file:// 加载的，必须问后端要端口才能拼 HTTP 地址。"""
        return self.ctx.port if self.ctx else 0

    def data_dir(self) -> str:
        return str(DATA_DIR)

    def overlay_url(self) -> str:
        return f"http://127.0.0.1:{self.ctx.port}/overlay" if self.ctx else ""

    def open_url(self, url: str) -> bool:
        """用系统默认浏览器打开链接（叠加层预览）。"""
        import webbrowser

        try:
            return bool(webbrowser.open(url))
        except Exception:  # noqa: BLE001
            return False

    def web_login(self, provider: str) -> str:
        """打开官方登录页，登录成功后返回完整 cookie 字符串。"""
        import webview

        if provider not in LOGIN_URLS:
            return ""
        key = LOGIN_COOKIE_KEYS[provider]
        window = webview.create_window(
            f"登录 {provider}",
            LOGIN_URLS[provider],
            width=980,
            height=720,
            resizable=True,
        )
        deadline = time.time() + 300
        cookie = ""
        while time.time() < deadline:
            time.sleep(1.2)
            try:
                cookies = window.get_cookies()
            except Exception:
                break
            jar: dict[str, str] = {}
            for simple in cookies:
                try:
                    for name, morsel in simple.items():
                        if morsel.value:
                            jar[name] = morsel.value
                except Exception:
                    continue
            if jar.get(key):
                cookie = "; ".join(f"{k}={v}" for k, v in jar.items())
                break
        try:
            window.destroy()
        except Exception:
            pass
        return cookie


def _serve(ctx: AppContext, ready: threading.Event) -> None:
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except AttributeError:
            pass

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ctx.loop = loop

    async def _boot():
        app = create_app(ctx)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", ctx.port)
        await site.start()
        # 首次选曲要解析音源，可能耗时，扔到后台避免拖慢启动
        asyncio.create_task(ctx.engine.start())
        return runner

    try:
        runner = loop.run_until_complete(_boot())
    except Exception as exc:  # noqa: BLE001
        print(f"服务启动失败：{exc}", file=sys.stderr)
        ready.set()
        return

    ready.set()

    try:
        loop.run_forever()
    finally:
        try:
            loop.run_until_complete(runner.cleanup())
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="B 站直播间点歌机")
    parser.add_argument("--port", type=int, default=0, help="HTTP 端口，默认读配置")
    parser.add_argument("--no-window", action="store_true", help="只起服务，不开窗口")
    parser.add_argument("--debug", action="store_true", help="打开 WebView 调试")
    parser.add_argument("--log-level", default="INFO", help="DEBUG / INFO / WARNING")
    args = parser.parse_args()

    import logging

    from app.log import Timer, get, setup

    setup(level=getattr(logging, args.log_level.upper(), logging.INFO))
    log = get("main")
    boot_t0 = time.perf_counter()

    with Timer(log, "初始化目录与数据库", level=logging.INFO):
        migrated = migrate_legacy_data()
        if migrated:
            log.info("数据目录已从 BiliSinger 迁移到 %s", migrated)
        ensure_dirs()
        init_db()

    with Timer(log, "读取配置", level=logging.INFO):
        config = load_config()

    wanted = args.port or config.port or 9527
    with Timer(log, "端口检查", level=logging.INFO, want=wanted):
        port, status, detail = resolve_port(wanted)
    if status == "conflict":
        print(detail, file=sys.stderr, flush=True)
        log.error("端口冲突：%s", detail)
        return 1
    if detail:
        print(detail, flush=True)
        log.info("端口：%s", detail)
    if port != config.port:
        config.port = port
        save_config(config)

    ctx = AppContext(config, port)
    ctx.debug = bool(args.debug)
    ctx.no_window = bool(args.no_window)
    ctx.restart = lambda: _spawn_restart(ctx)
    bridge = NativeBridge()
    bridge.ctx = ctx

    ready = threading.Event()
    thread = threading.Thread(target=_serve, args=(ctx, ready), daemon=True, name="bili-singer-server")
    thread.start()
    if not ready.wait(timeout=20):
        print("服务启动超时", file=sys.stderr, flush=True)
        log.error("服务启动超时")
        return 1

    base_url = f"http://127.0.0.1:{port}"
    log.info("后端就绪 %.0fms 端口 %d", (time.perf_counter() - boot_t0) * 1000, port)
    print(f"点歌机已启动：{base_url}", flush=True)
    print(f"OBS 叠加层：{base_url}/overlay", flush=True)
    print(f"数据目录：{DATA_DIR}", flush=True)

    if args.no_window:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0

    import webview

    # 关于自动播放：pywebview 在创建 WebView2 环境时会把
    # AdditionalBrowserArguments 整条覆写掉，环境变量注入的 autoplay 参数不会生效。
    # 与其去改第三方源码，不如接受「首播放需要点一下播放键」这个前提。
    # 音频能否出声跟 autoplay 无关，关键是 Content-Type 必须是 audio/*。

    # 必须加载后端自己的地址，不能传本地文件路径。
    # 传文件路径时 pywebview 会另起一个 Bottle 服务器托管目录（随机端口如 42001），
    # 窗口页面的 location.origin 就变成那个端口，前端会把所有 API 请求打到 Bottle 上，
    # 结果整站 404。直接加载 http 地址则全程单源，也免掉 CORS 麻烦。
    webview.create_window(
        f"B 站点歌机 v{__version__}",
        url=base_url,
        js_api=bridge,
        width=1280,
        height=820,
        min_size=(980, 640),
        text_select=False,
        background_color="#F7F8FA",
    )
    webview.start(
        debug=args.debug,
        private_mode=False,
        storage_path=str(DATA_DIR / "webview_profile"),
        gui="edgechromium",
    )

    close_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
