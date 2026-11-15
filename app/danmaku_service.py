"""弹幕指令服务。

把「连弹幕」和「点歌」缝在一起：
- 宽容匹配点歌前缀（全角半角、冒号、空格、大小写、无分隔符都认）
- 按配置的权限矩阵判断谁能执行哪条指令
- 点歌成功/失败都写一条日志给 GUI，观众侧不发弹幕（避免风控）
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable

from app.config import AppConfig, PERMISSION_LEVELS
from app.sources.bilibili.danmaku import DanmakuClient

# 前缀与正文之间可能出现这些分隔符
_SEPARATORS = " \u3000:：,，、-—－|｜"
# 前缀前可能带的直播指令符号
_SIGILS = ("!", "！", "#", "＃", "／", "/")

COMMANDS = {
    "request": ("点歌", "求歌", "点唱", "点播"),
    "skip": ("切歌", "跳过", "下一首"),
    "undo": ("取消点歌", "撤销点歌", "取消"),
    "top": ("置顶",),
    "queue": ("查队列", "看队列", "队列"),
}


def _normalize_prefixes(config: AppConfig) -> dict[str, list[str]]:
    """把配置里的前缀扩展成全角/半角/带符号的全部变体。"""
    base = [config.command_prefix or "点歌"] + list(config.command_aliases or [])
    out: dict[str, list[str]] = {k: list(v) for k, v in COMMANDS.items()}

    request_variants: list[str] = []
    for word in base:
        word = word.strip()
        if not word:
            continue
        request_variants.append(word)
        for sigil in _SIGILS:
            request_variants.append(sigil + word)
    # 长的排前面，避免「取消点歌」被「点歌」先匹配走
    out["request"] = sorted(set(request_variants), key=len, reverse=True)
    return out


def parse_command(text: str, prefixes: dict[str, list[str]]) -> tuple[str, str]:
    """返回 (指令名, 参数)。识别不了就返回 ("", "")。

    注意匹配顺序：先长后短，否则「取消点歌」会先命中「点歌」，
    变成点一首叫「消点歌」的歌。
    """
    raw = (text or "").strip()
    if not raw:
        return "", ""

    ordered = sorted(
        [(cmd, word) for cmd, words in prefixes.items() for word in words],
        key=lambda x: len(x[1]),
        reverse=True,
    )

    for cmd, word in ordered:
        if not raw.startswith(word):
            continue
        rest = raw[len(word):]
        # 前缀后面必须紧跟分隔符、结尾，或者直接接内容（点歌晴天）
        stripped = rest.lstrip(_SEPARATORS)
        if cmd == "request":
            arg = stripped.strip()
            if arg:
                return "request", arg
            # 「点歌」后面什么都没跟，当成无效而不是点一首空歌
            continue
        return cmd, stripped.strip()

    return "", ""


def has_permission(event: dict[str, Any], required: str) -> bool:
    """按权限等级判断。数值越小权限越高。"""
    if required == "all":
        return True
    if event.get("is_admin"):
        return True
    guard = int(event.get("guard_level") or 0)
    if required in ("guard1", "guard2", "guard3"):
        # 1 总督 / 2 提督 / 3 舰长，只要是大航海就算通过
        return guard in (1, 2, 3)
    if required == "admin":
        return False
    if required == "streamer":
        return False
    return False


class DanmakuService:
    def __init__(self, ctx, hub, api) -> None:
        self.ctx = ctx
        self.hub = hub
        self.api = api
        self.client: DanmakuClient | None = None
        self.prefixes = _normalize_prefixes(ctx.config)
        # 由 create_app 注入，负责往直播间发提示弹幕
        self.replier = None

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def start(self) -> None:
        from app.log import get

        log = get("danmaku")
        room_id = int(self.ctx.config.room_id or 0)
        if not room_id or not self.ctx.config.danmaku_enabled:
            log.info("弹幕未启用 room=%s enabled=%s", room_id, self.ctx.config.danmaku_enabled)
            self._set_status("off", "")
            return
        log.info("连接弹幕房间 %d", room_id)

        bili = self.ctx.bilibili
        try:
            await bili.ensure_buvid()
        except Exception:
            pass

        self.client = DanmakuClient(
            room_id,
            lambda: bili.danmu_info(room_id),
            buvid=bili._cookie.get("buvid3", ""),
            # 登录态连接必须带真实 uid，否则服务端拒绝（见 auth 包注释）
            uid=int(bili.uid or bili._cookie.get("DedeUserID") or 0),
        )
        await self.client.start(self._on_message, self._on_status)

    async def stop(self) -> None:
        if self.client:
            await self.client.stop()
            self.client = None
        self._set_status("off", "")

    def _set_status(self, state: str, detail: str) -> None:
        self.api.danmaku_status = {"state": state, "detail": detail}
        self.hub.emit({"event": "danmaku", "state": state, "detail": detail})

    def _on_status(self, state: str, detail: str) -> None:
        from app.log import get

        if state in ("connected", "authed", "disconnected"):
            get("danmaku").info("弹幕状态 %s %s", state, detail or "")
        else:
            get("danmaku").warning("弹幕状态 %s %s", state, detail or "")
        self._set_status(state, detail)

    # ── 消息处理 ──────────────────────────────────────────────────────

    async def _on_message(self, event: dict[str, Any]) -> None:
        text = event.get("text", "")
        user = event.get("uname", "")
        self.hub.emit({
            "event": "log",
            "entry": {"user": user, "text": text, "kind": ""},
        })

        cmd, arg = parse_command(text, self.prefixes)
        if not cmd:
            # 不是点歌指令：看看有没有命中「关键词 → 自动回复」
            if self.replier is not None:
                reply = self.replier.match_rule(text)
                if reply:
                    from app.log import get

                    get("reply").info("关键词命中 user=%s text=%r reply=%r",
                                      user, text[:20], reply)
                    self.replier.say(reply)
            return

        perms = self.ctx.config.permissions
        need = {
            "request": perms.request_song,
            "skip": perms.skip,
            "undo": perms.remove_own,
            "top": perms.top_song,
            "queue": "all",
        }.get(cmd, "all")

        if not has_permission(event, need):
            from app.log import get

            get("danmaku").info("权限不足 user=%s cmd=%s need=%s guard=%s",
                                user, cmd, need, event.get("guard_level"))
            self._log(user, "这条指令需要更高权限", "fail")
            return

        from app.log import get

        get("danmaku").info("指令 user=%s cmd=%s arg=%s", user, cmd, arg or "-")

        if cmd == "request":
            await self._handle_request(user, str(event.get("uid", "")), arg)
        elif cmd == "skip":
            await self._handle_skip(user)
        elif cmd == "undo":
            await self._handle_undo(user, str(event.get("uid", "")))
        elif cmd == "top":
            await self._handle_top(user)
        elif cmd == "queue":
            self._handle_queue(user)

    async def _handle_request(self, user: str, uid: str, keyword: str) -> None:
        """点歌：解析 → 入队 → 把每一步状态写进日志区。"""
        from app.log import get

        log = get("danmaku")
        if not keyword:
            self._log(user, "点歌要带上歌名", "fail")
            return

        self._log(user, f"正在搜索「{keyword}」…", "")
        started = time.time()
        try:
            result = await self.api.enqueue_by_keyword(keyword, user, uid)
        except Exception as exc:  # noqa: BLE001
            log.warning("点歌失败 user=%s keyword=%r error=%s", user, keyword, exc)
            self._log(user, f"没找到「{keyword}」，换个歌名试试", "fail")
            return
        elapsed = int((time.time() - started) * 1000)

        if result.get("duplicate"):
            pos = result.get("position") or 0
            log.info("点歌重复 user=%s keyword=%r pos=%s", user, keyword, pos)
            self._log(user, f"「{keyword}」已经在队列第 {pos} 位了", "fail")
            return

        song = result.get("song") or {}
        title = song.get("title") or keyword
        artist = song.get("artists") or ""
        source = "网易云" if song.get("source") == "netease" else "B站"
        pos = result.get("position") or 0
        playing = result.get("playing")

        label = f"{title} - {artist}" if artist else title
        log.info("点歌入队 user=%s pos=%s song=%s source=%s %dms",
                 user, pos, title, source, elapsed)
        if playing:
            self._log(user, f"正在播放：{label}", "ok")
        else:
            self._log(user, f"已加入队列第 {pos} 位：{label}（{source}）", "ok")

        # 往直播间播报入队结果（开关在设置里，默认关）
        if self.replier is not None and self.replier.cfg.announce_enqueue:
            self.replier.say(self.replier.fmt(
                self.replier.cfg.enqueue_template,
                user=user, title=title, artist=artist, source=source,
                position=pos,
            ))

    async def _handle_skip(self, user: str) -> None:
        ok = await self.ctx.engine.play_next(reason=f"danmaku:{user}")
        self._log(user, "已切到下一首" if ok else "队列里没有下一首", "ok" if ok else "fail")

    async def _handle_undo(self, user: str, uid: str) -> None:
        from app.store import repo

        removed = repo.queue_remove_by_requester(uid)
        self._log(user, f"已取消 {removed} 首" if removed else "你还没点过歌",
                  "ok" if removed else "fail")
        if removed:
            self.hub.emit({"event": "state", **self.ctx.engine.state.to_dict()})

    async def _handle_top(self, user: str) -> None:
        from app.store import repo

        items = repo.queue_list()
        if not items:
            self._log(user, "队列是空的", "fail")
            return
        repo.queue_promote(items[0].id)
        self._log(user, "已置顶", "ok")

    def _handle_queue(self, user: str) -> None:
        from app.store import repo

        items = repo.queue_list()
        if not items:
            self._log(user, "队列是空的，快来点歌", "")
            return
        names = "、".join(i.song.title for i in items[:3])
        more = f" 等 {len(items)} 首" if len(items) > 3 else ""
        self._log(user, f"队列：{names}{more}", "")

    async def simulate(self, event: dict[str, Any]) -> None:
        """调试用：喂一条假弹幕，走完整的解析/权限/入队链路。"""
        from app.log import get

        get("danmaku").info("模拟弹幕 user=%s text=%s",
                            event.get("uname"), event.get("text"))
        await self._on_message(event)

    def _log(self, user: str, text: str, kind: str) -> None:
        self.hub.emit({"event": "log", "entry": {"user": user, "text": text, "kind": kind}})
