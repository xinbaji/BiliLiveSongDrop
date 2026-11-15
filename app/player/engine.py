"""播放引擎：调度、播放模式、状态广播。

前端 <audio> 是真正的解码器，后端只做三件事：
1. 决定下一首是什么（点歌队列优先，空了走空闲歌单）；
2. 解析出可拉流的地址并预取到磁盘缓存；
3. 维护一份权威状态，供 GUI 与 OBS 叠加层同步。

进度条由「锚点位置 + 锚点时间」在前端本地插值，不靠每秒推送，
避免 WebSocket 被高频消息淹没。
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import AppConfig
from app.player import cache
from app.player.resolver import AudioResolver, ResolvedAudio
from app.store import repo
from app.store.models import SOURCE_NETEASE, Song

MODE_SEQUENTIAL = "sequential"
MODE_RANDOM = "random"
MODE_REPEAT_ALL = "repeat_all"
MODE_REPEAT_ONE = "repeat_one"
MODES = (MODE_SEQUENTIAL, MODE_RANDOM, MODE_REPEAT_ALL, MODE_REPEAT_ONE)

MAX_RESOLVE_RETRY = 3
RESOLVE_TTL = 3600  # 解析结果缓存秒数，B 站直链约 2 小时过期，这里留保守值
# 解析缓存必须设上限：每首歌一条，Long 时间挂着几百条就是几十 MB 的隐性增长
MAX_RESOLVED = 64


@dataclass
class Track:
    song: Song
    requester: str = ""
    playlist_id: int = 0
    queue_id: int = 0
    fallback: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.song.to_dict(),
            "requester": self.requester,
            "playlist_id": self.playlist_id,
            "queue_id": self.queue_id,
            "fallback": self.fallback,
            "note": self.note,
        }


@dataclass
class EngineState:
    status: str = "idle"           # idle | loading | playing | paused | error
    track: Track | None = None
    position: int = 0              # 毫秒
    duration: int = 0
    anchor_ts: float = 0.0         # time.time() 锚点
    mode: str = MODE_SEQUENTIAL
    volume: float = 0.8
    context: str = "none"          # queue | playlist | none
    error: str = ""
    queue_length: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "track": self.track.to_dict() if self.track else None,
            "position": self.position,
            "duration": self.duration,
            "anchor_ts": self.anchor_ts,
            "mode": self.mode,
            "volume": self.volume,
            "context": self.context,
            "error": self.error,
            "queue_length": self.queue_length,
        }


ChangeHook = Callable[[dict[str, Any]], None]


class PlayerEngine:
    def __init__(self, config: AppConfig, resolver: AudioResolver) -> None:
        self.config = config
        self.resolver = resolver
        self.state = EngineState(mode=config.play_mode, volume=config.volume)
        self._resolved: dict[str, tuple[float, ResolvedAudio]] = {}
        self._history: list[Track] = []          # 上一曲栈
        self._idle_cursor: dict[int, int] = {}   # 歌单 id → 当前索引
        self._recent_random: list[str] = []
        self._hooks: list[ChangeHook] = []
        self._prefetch_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # 千首歌单每次查一遍要 20ms+，一次切歌里会被调好几次（重试 + 预取），
        # 加个短 TTL 缓存，同一次切换内只查一次库
        self._pl_cache: dict[int, tuple[float, list[Song]]] = {}
        self._pl_cache_at: dict[int, float] = {}

    # ── 订阅 ──────────────────────────────────────────────────────────

    def subscribe(self, hook: ChangeHook) -> None:
        self._hooks.append(hook)

    def _emit(self, event: str = "state") -> None:
        # queue_length 是广播的一部分，必须在序列化之前取实时值。
        # 以前只在切歌时更新，导致「入队」这条路径广播出去的永远是旧值，
        # 前端看到 queue_length 没变就不刷新队列 —— 表现就是点了歌队列里没有。
        self.state.queue_length = repo.queue_count_active()
        payload = {"event": event, **self.state.to_dict()}
        for hook in list(self._hooks):
            try:
                hook(payload)
            except Exception:
                continue

    # ── 解析缓存 ──────────────────────────────────────────────────────

    def get_resolved(self, key: str) -> ResolvedAudio | None:
        item = self._resolved.get(key)
        if not item:
            return None
        expire, resolved = item
        if time.time() > expire:
            self._resolved.pop(key, None)
            return None
        return resolved

    def put_resolved(self, key: str, resolved: ResolvedAudio) -> None:
        if len(self._resolved) >= MAX_RESOLVED:
            # 按过期时间排序，淘汰最早过期的那条
            oldest = min(self._resolved.items(), key=lambda kv: kv[1][0])[0]
            self._resolved.pop(oldest, None)
        self._resolved[key] = (time.time() + RESOLVE_TTL, resolved)

    # ── 选曲 ──────────────────────────────────────────────────────────

    def _next_from_queue(self) -> Track | None:
        item = repo.queue_pop_next()
        if not item:
            return None
        return Track(
            song=item.song,
            requester=item.requester,
            queue_id=item.id,
            note="",
        )

    PL_CACHE_TTL = 3.0

    def _playlist_songs(self) -> list[Song]:
        pid = self.config.idle_playlist_id
        if not pid:
            return []
        now = time.time()
        hit = self._pl_cache.get(pid)
        if hit and now - hit[0] < self.PL_CACHE_TTL:
            return hit[1]
        songs = repo.playlist_songs(pid)
        self._pl_cache[pid] = (now, songs)
        return songs

    def invalidate_playlist_cache(self, pid: int | None = None) -> None:
        """歌单被增删改后调一下，避免继续用旧列表。"""
        if pid is None:
            self._pl_cache.clear()
        else:
            self._pl_cache.pop(pid, None)

    def _next_from_playlist(self, current: Track | None) -> Track | None:
        pid = self.config.idle_playlist_id
        songs = self._playlist_songs()
        if not songs:
            return None

        ids = [s.id for s in songs]
        cur_index = -1
        if current and current.playlist_id == pid and current.song.id in ids:
            cur_index = ids.index(current.song.id)

        mode = self.state.mode

        if mode == MODE_REPEAT_ONE and cur_index >= 0:
            return Track(song=songs[cur_index], playlist_id=pid)

        if mode == MODE_RANDOM:
            # 尽量避开最近 5 首，池子太小就放弃避让
            recent = set(self._recent_random[-5:])
            pool = [s for s in songs if s.key not in recent] or songs
            pick = random.choice(pool)
            return Track(song=pick, playlist_id=pid)

        nxt = cur_index + 1
        if nxt >= len(songs):
            if mode == MODE_REPEAT_ALL:
                nxt = 0
            else:
                return None
        return Track(song=songs[nxt], playlist_id=pid)

    def _pick_next(self, current: Track | None) -> tuple[Track | None, str]:
        """返回 (曲目, 上下文)。点歌队列永远优先，这是「打断循环」的落点。"""
        queued = self._next_from_queue()
        if queued:
            return queued, "queue"
        if not self.config.idle_when_queue_empty:
            return None, "none"
        nxt = self._next_from_playlist(current)
        if nxt:
            return nxt, "playlist"
        return None, "none"

    # ── 播放控制 ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动时只恢复状态，不自动起播。

        以前这里直接 play_next，导致一开程序就自己放歌——主播还没开播、
        OBS 还没切场景的时候就已经在响了。现在改成完全由用户触发：
        点播放 / 点某首歌 / 队列从空变非空（有人点歌）才播。
        """
        self.state.status = "idle"
        self.state.queue_length = repo.queue_count_active()
        self._emit("idle")

    async def play_next(self, reason: str = "manual", auto: bool = False) -> bool:
        from app.log import get, Timer

        log = get("engine")
        async with self._lock:
            current = self.state.track
            # 不论这一下是自然播完、主播点下一首还是弹幕切歌，都要把上一首
            # 从「播放中」结掉。只在 on_track_ended 里标的话，手动切歌会让
            # 旧曲目一直挂着 playing，队列越用越长。
            if current and current.queue_id:
                repo.queue_mark(current.queue_id, "done")
            if auto and current:
                self._push_history(current)

            for attempt in range(MAX_RESOLVE_RETRY):
                track, context = self._pick_next(current)
                if not track:
                    log.info("队列空且无可播歌单，转入空闲 (reason=%s)", reason)
                    self._go_idle()
                    return False

                with Timer(log, f"准备播放 {track.song.title[:24]}",
                           level=20, ctx=context):
                    resolved = await self._ensure_resolved(track.song)
                if resolved.ok:
                    track.fallback = resolved.fallback
                    track.note = resolved.note
                    self.state.track = track
                    self.state.context = context
                    self.state.status = "loading"
                    self.state.position = 0
                    self.state.duration = track.song.duration
                    self.state.anchor_ts = time.time()
                    self.state.error = ""

                    if context == "queue" and track.queue_id:
                        repo.queue_mark(track.queue_id, "playing")
                    if context == "playlist":
                        self._idle_cursor[track.playlist_id] = self._idle_cursor.get(track.playlist_id, 0)
                    if self.state.mode == MODE_RANDOM and track.playlist_id:
                        self._recent_random.append(track.song.key)
                        self._recent_random = self._recent_random[-20:]

                    repo.history_add(
                        track.song.id, track.requester, track.song.source,
                        track.playlist_id,
                    )
                    self.state.queue_length = repo.queue_count_active()
                    self._emit("track_change")
                    self._schedule_prefetch()
                    return True

                # 解析失败：点歌队列里的直接标失败出队，避免死循环
                log.warning("第 %d 次选曲不可播：%s note=%s",
                            attempt + 1, track.song.title[:30], resolved.note)
                if track.queue_id:
                    repo.queue_mark(track.queue_id, "failed")
                self.state.error = resolved.note
                current = None

            self._go_idle()
            return False

    async def play_specific(self, song: Song, requester: str = "") -> bool:
        """主播手动点播：插到队列最前面再切。"""
        repo.queue_add(song.id, requester, "", priority=99)
        return await self.play_next(reason="manual_pick")

    async def play_now(self, song: Song, requester: str = "", playlist_id: int = 0) -> bool:
        """直接从歌单里播某一首，不经过点歌队列，也不污染队列。

        切到歌单上下文后，播完会顺着这个歌单往下走。
        """
        async with self._lock:
            resolved = await self._ensure_resolved(song)
            if not resolved.ok:
                self.state.error = resolved.note
                self._emit("error")
                return False

            if self.state.track:
                self._push_history(self.state.track)

            track = Track(
                song=song, requester=requester, playlist_id=playlist_id,
                fallback=resolved.fallback, note=resolved.note,
            )
            self.state.track = track
            self.state.context = "playlist" if playlist_id else "queue"
            self.state.status = "loading"
            self.state.position = 0
            self.state.duration = song.duration
            self.state.anchor_ts = time.time()
            self.state.error = ""

            repo.history_add(song.id, requester, song.source, playlist_id)
            self._emit("track_change")
            self._schedule_prefetch()
            return True

    async def play_prev(self) -> bool:
        if not self._history:
            return False
        last = self._history.pop()
        resolved = await self._ensure_resolved(last.song)
        if not resolved.ok:
            return await self.play_prev()
        self.state.track = last
        self.state.status = "loading"
        self.state.position = 0
        self.state.duration = last.song.duration
        self.state.anchor_ts = time.time()
        self._emit("track_change")
        return True

    def _push_history(self, track: Track) -> None:
        self._history.append(track)
        self._history = self._history[-30:]

    async def on_track_ended(self) -> None:
        """前端播放结束回调。"""
        if self.state.track and self.state.track.queue_id:
            repo.queue_mark(self.state.track.queue_id, "done")
        await self.play_next(reason="ended", auto=True)

    def set_status(self, status: str) -> None:
        self.state.status = status
        self.state.anchor_ts = time.time()
        self._emit()

    def report_position(self, position_ms: int, duration_ms: int = 0) -> None:
        """前端上报真实进度，用于叠加层校准。"""
        self.state.position = max(0, int(position_ms))
        self.state.anchor_ts = time.time()
        if duration_ms > 0:
            self.state.duration = int(duration_ms)

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            return
        self.state.mode = mode
        self.config.play_mode = mode
        self._emit("mode")

    def set_volume(self, volume: float) -> None:
        self.state.volume = max(0.0, min(1.0, float(volume)))
        self.config.volume = self.state.volume
        self._emit("volume")

    def _go_idle(self) -> None:
        self.state.status = "idle"
        self.state.track = None
        self.state.context = "none"
        self.state.position = 0
        self.state.duration = 0
        self.state.queue_length = len(repo.queue_list())
        self._emit("idle")

    # ── 预取 ──────────────────────────────────────────────────────────

    def _schedule_prefetch(self) -> None:
        from app.log import get

        if not self.config.prefetch_enabled:
            get("prefetch").debug("预取已关闭")
            return
        if self._prefetch_task and not self._prefetch_task.done():
            return
        self._prefetch_task = asyncio.create_task(self._prefetch_next())

    async def _prefetch_next(self) -> None:
        from app.log import Timer, get

        log = get("prefetch")
        try:
            # 只预取「点歌队列」的下一首。
            # 空闲歌单是长期滚动的列表，预载它等于持续吃带宽、磁盘和网络连接，
            # 而观众点的歌才是真正需要「点了就秒播」的。
            nxt = self._next_from_queue()
            if not nxt:
                return
            if cache.find_existing(nxt.song.key):
                log.debug("下一首已缓存 %s", nxt.song.key)
                return
            with Timer(log, f"预取 {nxt.song.title[:24]}", level=20):
                resolved = await self._ensure_resolved(nxt.song)
                if resolved.ok and resolved.play_song:
                    await self._download_to_cache(resolved.play_song, resolved)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _ensure_resolved(self, song: Song) -> ResolvedAudio:
        cached = self.get_resolved(song.key)
        if cached and cached.ok:
            return cached
        resolved = await self.resolver.resolve(song)
        if resolved.ok:
            self.put_resolved(song.key, resolved)
        return resolved

    async def _download_to_cache(self, song: Song, resolved: ResolvedAudio) -> None:
        """把整首拉下来落盘，下次切歌零等待。"""
        import aiohttp

        path = cache.audio_path(song.key)
        if cache.find_existing(song.key):
            return
        tmp = path.with_suffix(path.suffix + ".part")
        try:
            async with aiohttp.ClientSession(
                trust_env=True, headers=resolved.headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as session:
                async with session.get(resolved.url) as resp:
                    if resp.status not in (200, 206):
                        return
                    with open(tmp, "wb") as f:
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            f.write(chunk)
            tmp.replace(path)
            limit = max(256, self.config.cache_limit_mb) * 1024 * 1024
            cache.evict(limit)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    async def shutdown(self) -> None:
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
