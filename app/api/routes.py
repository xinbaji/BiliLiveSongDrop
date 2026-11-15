"""HTTP API 路由。"""
from __future__ import annotations

import base64
import io
import json
import re
import time
from typing import Any

from aiohttp import web

from app.config import AppConfig, config_as_dict, save_config
from app.player import cache
from app.store import repo
from app import updater
from app.store.models import SOURCE_BILIBILI, SOURCE_NETEASE, Song


def json_ok(data: Any = None, **extra) -> web.Response:
    payload = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return web.json_response(payload, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def json_err(message: str, code: int = 400) -> web.Response:
    return web.json_response(
        {"ok": False, "error": message}, status=code,
        dumps=lambda o: json.dumps(o, ensure_ascii=False),
    )


def qr_data_uri(text: str) -> str:
    # 延迟导入：qrcode + PIL 约 30ms，只有扫码时才用得上，不该拖慢启动
    import qrcode

    img = qrcode.make(text, box_size=8, border=1)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


class Api:
    def __init__(self, engine, resolver, netease, bilibili, hub, config: AppConfig) -> None:
        self.engine = engine
        self.resolver = resolver
        self.netease = netease
        self.bilibili = bilibili
        self.hub = hub
        self.config = config
        self.danmaku_status = {"state": "off", "detail": ""}
        self._room_cache: dict[int, tuple[float, dict]] = {}
        self._state_cache: dict[str, Any] = {}
        self._auth_cache: dict[str, Any] | None = None
        self._auth_cache_at = 0.0
        self.web_login_cb = None  # 由 main 注入，用于弹真实登录窗口
        self.replier = None       # 由 create_app 注入，负责发提示弹幕
        self.restart_cb = None    # 由 main 注入，用于重启进程

    # ── 装配 ──────────────────────────────────────────────────────────

    def register(self, app: web.Application) -> None:
        r = app.router
        r.add_get("/api/state", self.state)

        r.add_post("/api/player/next", self.player_next)
        r.add_post("/api/player/prev", self.player_prev)
        r.add_post("/api/player/toggle", self.player_toggle)
        r.add_post("/api/player/pause", self.player_pause)
        r.add_post("/api/player/resume", self.player_resume)
        r.add_post("/api/player/seek", self.player_seek)
        r.add_post("/api/player/mode", self.player_mode)
        r.add_post("/api/player/volume", self.player_volume)
        r.add_post("/api/player/ended", self.player_ended)
        r.add_post("/api/player/position", self.player_position)
        r.add_post("/api/player/play", self.player_play)

        r.add_get("/api/queue", self.queue_list)
        r.add_post("/api/queue/add", self.queue_add)
        r.add_post("/api/queue/search_add", self.queue_search_add)
        r.add_post("/api/queue/clear", self.queue_clear)
        r.add_delete("/api/queue/{qid}", self.queue_remove)
        r.add_post("/api/queue/{qid}/top", self.queue_top)

        r.add_get("/api/search", self.search)

        r.add_get("/api/playlists", self.playlists)
        r.add_post("/api/playlists", self.playlist_create)
        r.add_delete("/api/playlists/{pid}", self.playlist_delete)
        r.add_get("/api/playlists/{pid}/songs", self.playlist_songs)
        r.add_post("/api/playlists/{pid}/add", self.playlist_add)
        r.add_post("/api/playlists/{pid}/add_by_id", self.playlist_add_by_id)
        r.add_post("/api/playlists/{pid}/add_song", self.playlist_add_song)
        r.add_post("/api/playlists/{pid}/import", self.playlist_import)
        r.add_post("/api/playlists/import_remote", self.playlist_import_remote)
        r.add_post("/api/playlists/{pid}/refresh", self.playlist_refresh)
        r.add_delete("/api/playlists/{pid}/songs/{sid}", self.playlist_remove)
        r.add_post("/api/playlists/{pid}/clear", self.playlist_clear)
        r.add_post("/api/playlists/{pid}/reorder", self.playlist_reorder)

        r.add_get("/api/auth/status", self.auth_status)
        r.add_post("/api/auth/{provider}/qr", self.auth_qr)
        r.add_get("/api/auth/{provider}/qr/poll", self.auth_qr_poll)
        r.add_post("/api/auth/{provider}/logout", self.auth_logout)
        r.add_post("/api/auth/{provider}/web_login", self.auth_web_login)

        r.add_get("/api/config", self.get_config)
        r.add_put("/api/config", self.put_config)
        r.add_get("/api/room/info", self.room_info)
        r.add_get("/api/history", self.history)
        r.add_get("/api/cache/stats", self.cache_stats)
        r.add_post("/api/cache/clear", self.cache_clear)
        r.add_get("/api/danmaku/status", self.danmaku_state)
        r.add_get("/api/reply/stats", self.reply_stats)
        r.add_post("/api/reply/test", self.reply_test)
        r.add_post("/api/system/restart", self.system_restart)
        r.add_get("/api/about", self.about)
        r.add_get("/api/update/check", self.update_check)
        r.add_post("/api/update/apply", self.update_apply)

    # ── 状态 ──────────────────────────────────────────────────────────

    async def state(self, request: web.Request) -> web.Response:
        return json_ok({
            "player": self.engine.state.to_dict(),
            "queue": [q.to_dict() for q in repo.queue_list()],
            "playlists": [p.to_dict() for p in repo.list_playlists()],
            "config": config_as_dict(self.config),
            "auth": await self._auth_summary(),
            "danmaku": self.danmaku_status,
            "hub_clients": self.hub.count,
        })

    async def _auth_summary(self) -> dict[str, Any]:
        # DPAPI 解密不便宜，而登录态几乎不变，缓存 5 秒
        if self._auth_cache is not None and time.time() - self._auth_cache_at < 5:
            return self._auth_cache

        out: dict[str, Any] = {}
        ne = repo.auth_get("netease")
        bl = repo.auth_get("bilibili")
        # uid 一并给前端，登录后要在界面上显示「昵称 · ID」
        out["netease"] = {"logged_in": bool(ne and "MUSIC_U" in ne["cookie"]),
                          "account": (ne or {}).get("account", ""),
                          "uid": (ne or {}).get("uid", "")}
        out["bilibili"] = {"logged_in": bool(bl and "SESSDATA" in bl["cookie"]),
                           "account": (bl or {}).get("account", ""),
                           "uid": (bl or {}).get("uid", "")}
        self._auth_cache = out
        self._auth_cache_at = time.time()
        return out

    def _broadcast_state(self) -> None:
        # 入队 / 出队后必须用实时长度广播，否则前端判断不出「队列变了」
        self.engine.state.queue_length = repo.queue_count_active()
        self.hub.emit({"event": "state", **self.engine.state.to_dict()})

    # ── 播放控制 ──────────────────────────────────────────────────────

    async def player_next(self, request: web.Request) -> web.Response:
        ok = await self.engine.play_next(reason="manual")
        return json_ok({"started": ok})

    async def player_prev(self, request: web.Request) -> web.Response:
        ok = await self.engine.play_prev()
        return json_ok({"started": ok})

    async def player_pause(self, request: web.Request) -> web.Response:
        self.engine.set_status("paused")
        return json_ok()

    async def player_resume(self, request: web.Request) -> web.Response:
        self.engine.set_status("playing")
        return json_ok()

    async def player_toggle(self, request: web.Request) -> web.Response:
        status = self.engine.state.status
        self.engine.set_status("paused" if status == "playing" else "playing")
        return json_ok({"status": self.engine.state.status})

    async def player_seek(self, request: web.Request) -> web.Response:
        body = await request.json()
        position = int(body.get("position") or 0)
        self.engine.report_position(position)
        return json_ok({"position": self.engine.state.position})

    async def player_mode(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.engine.set_mode(str(body.get("mode") or ""))
        return json_ok({"mode": self.engine.state.mode})

    async def player_volume(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.engine.set_volume(float(body.get("volume") or 0))
        save_config(self.config)
        return json_ok({"volume": self.engine.state.volume})

    async def player_ended(self, request: web.Request) -> web.Response:
        await self.engine.on_track_ended()
        return json_ok()

    async def player_position(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.engine.report_position(
            int(body.get("position") or 0), int(body.get("duration") or 0)
        )
        return json_ok()

    async def player_play(self, request: web.Request) -> web.Response:
        """从歌单直接播某一首。"""
        body = await request.json()
        song_id = int(body.get("song_id") or 0)
        playlist_id = int(body.get("playlist_id") or 0)
        song = repo.get_song(song_id)
        if song is None:
            return json_err("歌曲不存在", 404)
        ok = await self.engine.play_now(song, playlist_id=playlist_id)
        return json_ok({"started": ok, "note": self.engine.state.error if not ok else ""})

    # ── 队列 ──────────────────────────────────────────────────────────

    async def queue_list(self, request: web.Request) -> web.Response:
        # 含正在播放的那一首：播完才移出列表，这样主播始终能看到「正在播的这首 + 后面排队的」
        return json_ok([q.to_dict() for q in repo.queue_list_active()])

    async def queue_add(self, request: web.Request) -> web.Response:
        body = await request.json()
        source = str(body.get("source") or "")
        source_id = str(body.get("source_id") or "")
        extra = str(body.get("extra") or "")
        requester = str(body.get("requester") or "")
        if source not in (SOURCE_NETEASE, SOURCE_BILIBILI) or not source_id:
            return json_err("缺少 source / source_id")

        song = repo.find_song(source, source_id, extra)
        if song is None:
            return json_err("歌曲不存在，请先搜索", 404)

        dup = repo.queue_find_duplicate(song.id)
        if dup:
            return json_ok({"duplicate": True,
                            "position": repo.queue_position(dup.id)}, )
        qid = repo.queue_add(song.id, requester)
        self._broadcast_state()
        return json_ok({"queue_id": qid, "position": repo.queue_position(qid)})

    async def queue_search_add(self, request: web.Request) -> web.Response:
        body = await request.json()
        keyword = str(body.get("keyword") or "").strip()
        requester = str(body.get("requester") or "")
        if not keyword:
            return json_err("关键词为空")
        try:
            result = await self.enqueue_by_keyword(keyword, requester)
        except RuntimeError as exc:
            return json_err(str(exc), 404)
        return json_ok(result)

    async def enqueue_by_keyword(
        self, keyword: str, requester: str = "", requester_uid: str = ""
    ) -> dict[str, Any]:
        """弹幕点歌与手动搜索共用的一条入队路径。"""
        song = await self._search_first(keyword)
        if song is None:
            raise RuntimeError("没搜到这首歌")

        dup = repo.queue_find_duplicate(song.id)
        if dup:
            return {"duplicate": True, "position": repo.queue_position(dup.id),
                    "song": song.to_dict()}

        qid = repo.queue_add(song.id, requester, requester_uid)

        # 只有「当前什么都没在播」时才立刻起播；正在播的话保持不打断，
        # 由 on_track_ended 在自然播完后转入队列。
        idle = self.engine.state.status in ("idle", "error")
        if idle:
            import asyncio

            asyncio.create_task(self.engine.play_next(reason="queue_add"))
        self._broadcast_state()
        return {"queue_id": qid, "position": repo.queue_position(qid),
                "song": song.to_dict(), "playing": idle}

    async def queue_remove(self, request: web.Request) -> web.Response:
        qid = int(request.match_info["qid"])
        repo.queue_remove(qid)
        self._broadcast_state()
        return json_ok()

    async def queue_top(self, request: web.Request) -> web.Response:
        qid = int(request.match_info["qid"])
        repo.queue_promote(qid)
        self._broadcast_state()
        return json_ok()

    async def queue_clear(self, request: web.Request) -> web.Response:
        n = repo.queue_clear()
        self._broadcast_state()
        return json_ok({"removed": n})

    # ── 搜索 ──────────────────────────────────────────────────────────

    async def _search_first(self, keyword: str) -> Song | None:
        """双源并发，按配置的优先级取首个可用结果。"""
        import asyncio

        tasks = {}
        if self.config.prefer_source == SOURCE_NETEASE:
            tasks["netease"] = asyncio.create_task(self.netease.search(keyword, 5))
            tasks["bilibili"] = asyncio.create_task(self.bilibili.search(keyword, 5))
        else:
            tasks["bilibili"] = asyncio.create_task(self.bilibili.search(keyword, 5))
            tasks["netease"] = asyncio.create_task(self.netease.search(keyword, 5))

        results: dict[str, list[Song]] = {"netease": [], "bilibili": []}
        for name, task in tasks.items():
            try:
                results[name] = await task
            except Exception:
                results[name] = []

        for name in ("netease", "bilibili") if self.config.prefer_source == SOURCE_NETEASE \
                else ("bilibili", "netease"):
            if results[name]:
                return await self._persist_song(results[name][0])
        return None

    async def _persist_song(self, song: Song) -> Song:
        """入库前补齐网易云搜索缺失的封面。"""
        if song.source == SOURCE_NETEASE and not song.cover_url:
            try:
                detail = await self.netease.song_detail([song.source_id])
                if detail and detail[0].cover_url:
                    song.cover_url = detail[0].cover_url
                    song.album = song.album or detail[0].album
            except Exception:
                pass
        return repo.upsert_song(song)

    async def search(self, request: web.Request) -> web.Response:
        keyword = (request.query.get("q") or "").strip()
        source = request.query.get("source") or "both"
        if not keyword:
            return json_ok({"netease": [], "bilibili": []})

        import asyncio

        out: dict[str, list[dict[str, Any]]] = {"netease": [], "bilibili": []}
        jobs = []
        # all 和 both 都当「两个源都搜」。前端按界面语义传 all，
        # 只认 both 的话会静默返回空结果，看起来像「没搜到」。
        if source in ("both", "all", "netease"):
            jobs.append(("netease", asyncio.create_task(self.netease.search(keyword, 12))))
        if source in ("both", "all", "bilibili"):
            jobs.append(("bilibili", asyncio.create_task(self.bilibili.search(keyword, 12))))

        for name, task in jobs:
            try:
                songs = await task
            except Exception:
                songs = []
            out[name] = [s.to_dict() for s in songs]
        return json_ok(out)

    # ── 歌单 ──────────────────────────────────────────────────────────

    async def playlists(self, request: web.Request) -> web.Response:
        return json_ok([p.to_dict() for p in repo.list_playlists()])

    async def playlist_create(self, request: web.Request) -> web.Response:
        body = await request.json()
        name = str(body.get("name") or "").strip() or "新歌单"
        kind = str(body.get("kind") or "normal")
        pl = repo.create_playlist(name, kind)
        return json_ok(pl.to_dict())

    async def playlist_delete(self, request: web.Request) -> web.Response:
        pid = int(request.match_info["pid"])
        repo.delete_playlist(pid)
        self.engine.invalidate_playlist_cache(pid)
        # 空闲歌单被删掉时要清空引用，否则引擎会一直指一个不存在的歌单
        if self.config.idle_playlist_id == pid:
            self.config.idle_playlist_id = 0
            save_config(self.config)
        return json_ok()

    async def playlist_songs(self, request: web.Request) -> web.Response:
        pid = int(request.match_info["pid"])
        return json_ok([s.to_dict() for s in repo.playlist_songs(pid)])

    async def playlist_add_by_id(self, request: web.Request) -> web.Response:
        """按本地 song_id 收藏进歌单。

        界面上的曲目行只知道自己在本地的 id，不该为了收藏再拼一次
        source + source_id + extra —— 拼错一个就会命中不到已有记录，插出重复行。
        """
        pid = int(request.match_info["pid"])
        body = await request.json()
        song_id = int(body.get("song_id") or 0)
        song = repo.get_song(song_id)
        if song is None:
            return json_err("歌曲不存在", 404)
        added = repo.playlist_add(pid, song.id)
        self.engine.invalidate_playlist_cache(pid)
        return json_ok({"added": added, "playlist_id": pid})

    async def playlist_add(self, request: web.Request) -> web.Response:
        pid = int(request.match_info["pid"])
        body = await request.json()
        source = str(body.get("source") or "")
        source_id = str(body.get("source_id") or "")
        extra = str(body.get("extra") or "")
        song = repo.find_song(source, source_id, extra)
        if song is None:
            return json_err("歌曲不存在", 404)
        added = repo.playlist_add(pid, song.id)
        self.engine.invalidate_playlist_cache(pid)
        return json_ok({"added": added})

    async def playlist_remove(self, request: web.Request) -> web.Response:
        pid = int(request.match_info["pid"])
        sid = int(request.match_info["sid"])
        repo.playlist_remove(pid, sid)
        self.engine.invalidate_playlist_cache(pid)
        return json_ok()

    async def playlist_reorder(self, request: web.Request) -> web.Response:
        pid = int(request.match_info["pid"])
        body = await request.json()
        ids = [int(x) for x in (body.get("song_ids") or [])]
        repo.playlist_reorder(pid, ids)
        self.engine.invalidate_playlist_cache(pid)
        return json_ok()

    async def playlist_clear(self, request: web.Request) -> web.Response:
        pid = int(request.match_info["pid"])
        removed = repo.playlist_clear(pid)
        self.engine.invalidate_playlist_cache(pid)
        return json_ok({"removed": removed})

    async def room_info(self, request: web.Request) -> web.Response:
        room_id = int(request.query.get("room_id") or self.config.room_id or 0)
        if not room_id:
            return json_err("还没设置房间号")

        # 直播间信息 60 秒内不变，缓存掉，避免前端每次刷新都打一遍 B 站接口
        cached = self._room_cache.get(room_id)
        if cached and time.time() - cached[0] < 60:
            return json_ok(cached[1])

        try:
            info = await self.bilibili.room_info(room_id)
        except Exception as exc:  # noqa: BLE001
            return json_err(f"取直播间信息失败：{exc}", 502)

        self._room_cache[room_id] = (time.time(), info)
        return json_ok(info)

    async def playlist_import(self, request: web.Request) -> web.Response:
        """往指定歌单里导入内容。

        一个输入框吃下所有形式：BV 号、网易云歌曲 ID、各种分享链接、歌名关键词。
        """
        pid = int(request.match_info["pid"])
        body = await request.json()
        raw = str(body.get("input") or body.get("url") or "").strip()
        if not raw:
            return json_err("内容为空")
        if repo.list_playlists() and not any(p.id == pid for p in repo.list_playlists()):
            return json_err("歌单不存在", 404)

        kind, value = classify_input(raw)
        self.engine.invalidate_playlist_cache(pid)
        try:
            if kind == "bili_video":
                song = await self.bilibili.resolve_song(value)
                saved = repo.upsert_song(song)
                added = repo.playlist_add(pid, saved.id)
                return json_ok({"kind": kind, "added": 1 if added else 0,
                                "songs": [saved.to_dict()], "candidates": []})

            if kind == "ncm_song":
                songs = await self.netease.song_detail([value])
                if not songs:
                    return json_err("这首歌不存在或已下架", 404)
                saved = await self._persist_song(songs[0])
                added = repo.playlist_add(pid, saved.id)
                return json_ok({"kind": kind, "added": 1 if added else 0,
                                "songs": [saved.to_dict()], "candidates": []})

            if kind == "ncm_playlist":
                name, songs = await self.netease.playlist_detail(value)
                count = 0
                for s in songs:
                    saved = await self._persist_song(s)
                    if repo.playlist_add(pid, saved.id):
                        count += 1
                return json_ok({"kind": kind, "added": count, "songs": [],
                                "candidates": [], "source_name": name})

            if kind == "bili_fav":
                songs = await self.bilibili.fav_items(int(value), 200)
                count = 0
                for s in songs:
                    saved = repo.upsert_song(s)
                    if repo.playlist_add(pid, saved.id):
                        count += 1
                return json_ok({"kind": kind, "added": count, "songs": [],
                                "candidates": []})

            # 关键词：先给候选，让用户挑，不盲目塞进去
            candidates = await self.search_both(value, 8)
            return json_ok({"kind": "keyword", "added": 0, "songs": [],
                            "candidates": [s.to_dict() for s in candidates]})
        except Exception as exc:  # noqa: BLE001
            return json_err(f"导入失败：{exc}", 502)

    # ── 远程歌单：自动建单 + 差异刷新 ──────────────────────────────────

    async def _fetch_remote(self, remote_type: str, remote_id: str) -> tuple[str, list[Song]]:
        """按来源拉远程歌单，返回 (歌单名, 歌曲列表)。"""
        if remote_type == "netease_playlist":
            name, songs = await self.netease.playlist_detail(remote_id)
            return name or f"网易云歌单 {remote_id}", songs
        if remote_type == "bili_fav":
            name, songs = await self.bilibili.fav_detail(int(remote_id))
            return name or f"B站收藏夹 {remote_id}", songs
        raise RuntimeError(f"不支持的远程歌单类型：{remote_type}")

    async def playlist_import_remote(self, request: web.Request) -> web.Response:
        """导入歌单链接：以远程歌单的名字新建歌单，并把全部曲目导进去。

        和「往已有歌单里导入」不同，这条路径会记住远程来源，
        之后可以点刷新做差异更新。
        """
        body = await request.json()
        raw = str(body.get("input") or body.get("url") or "").strip()
        if not raw:
            return json_err("内容为空")

        kind, value = classify_input(raw)
        remote_type = {"ncm_playlist": "netease_playlist", "bili_fav": "bili_fav"}.get(kind)
        if not remote_type:
            return json_err("这不是歌单链接。单曲请用左侧输入框直接导入")

        try:
            name, songs = await self._fetch_remote(remote_type, value)
        except Exception as exc:  # noqa: BLE001
            return json_err(f"拉取歌单失败：{exc}", 502)

        pl = repo.create_playlist(name, remote_type=remote_type, remote_id=value)
        added = 0
        for song in songs:
            saved = await self._persist_song(song)
            if repo.playlist_add(pl.id, saved.id, origin="remote"):
                added += 1
        repo.touch_playlist_synced(pl.id)
        self.engine.invalidate_playlist_cache(pl.id)

        return json_ok({
            "playlist": repo.get_playlist(pl.id).to_dict(),
            "imported": added,
            "total": len(songs),
        })

    async def playlist_refresh(self, request: web.Request) -> web.Response:
        """按远程歌单做差异更新。

        只处理 origin=remote 的曲目：
          - 远程还有、本地没有 → 补进来
          - 远程已删、本地是 remote 来的 → 移出
        手动收藏/搜索加进来的歌一律不动，哪怕远程列表里没有。
        """
        pid = int(request.match_info["pid"])
        pl = repo.get_playlist(pid)
        if pl is None:
            return json_err("歌单不存在", 404)
        if not (pl.remote_type and pl.remote_id):
            return json_err("这个歌单不是从远程导入的，无法刷新")

        try:
            _name, songs = await self._fetch_remote(pl.remote_type, pl.remote_id)
        except Exception as exc:  # noqa: BLE001
            return json_err(f"拉取歌单失败：{exc}", 502)

        # 远程曲目 → 本地 song id
        remote_ids: set[int] = set()
        added = 0
        for song in songs:
            saved = await self._persist_song(song)
            remote_ids.add(saved.id)
            if repo.playlist_add(pid, saved.id, origin="remote"):
                added += 1

        # 本地「远程来源」的曲目里，已经不在远程列表的，移出
        stale = list(repo.playlist_remote_song_ids(pid) - remote_ids)
        removed = repo.playlist_delete_items(pid, stale)
        repo.touch_playlist_synced(pid)
        self.engine.invalidate_playlist_cache(pid)
        self._broadcast_state()

        return json_ok({
            "added": added,
            "removed": removed,
            "total": len(songs),
            "playlist": repo.get_playlist(pid).to_dict(),
        })

    async def search_both(self, keyword: str, limit: int = 8) -> list[Song]:
        import asyncio

        jobs = [
            asyncio.create_task(self.netease.search(keyword, limit)),
            asyncio.create_task(self.bilibili.search(keyword, limit)),
        ]
        out: list[Song] = []
        for task in jobs:
            try:
                out.extend(await task)
            except Exception:
                continue
        return out

    async def playlist_add_song(self, request: web.Request) -> web.Response:
        """把搜索结果里的某一首具体音源加进歌单（已知 source + source_id）。"""
        pid = int(request.match_info["pid"])
        body = await request.json()
        source = str(body.get("source") or "")
        source_id = str(body.get("source_id") or "")
        extra = str(body.get("extra") or "")
        title = str(body.get("title") or "")
        artists = str(body.get("artists") or "")
        cover = str(body.get("cover_url") or "")
        duration = int(body.get("duration") or 0)

        if source not in (SOURCE_NETEASE, SOURCE_BILIBILI) or not source_id:
            return json_err("缺少音源信息")

        song = repo.find_song(source, source_id, extra)
        if song is None:
            song = repo.upsert_song(Song(
                source=source, source_id=source_id, extra=extra,
                title=title or "未知曲目", artists=artists,
                cover_url=cover, duration=duration, album="",
            ))
        if source == SOURCE_BILIBILI and not song.extra:
            # 缺 cid 的 B 站歌补一次，否则播的时候还要多跑一趟
            try:
                fresh = await self.bilibili.resolve_song(source_id)
                song = repo.upsert_song(fresh)
            except Exception:
                pass

        added = repo.playlist_add(pid, song.id)
        self.engine.invalidate_playlist_cache(pid)
        return json_ok({"added": added, "song": song.to_dict()})

    # ── 登录 ──────────────────────────────────────────────────────────

    async def auth_status(self, request: web.Request) -> web.Response:
        return json_ok(await self._auth_summary())

    async def auth_qr(self, request: web.Request) -> web.Response:
        provider = request.match_info["provider"]
        try:
            if provider == "netease":
                info = await self.netease.qr_create()
            elif provider == "bilibili":
                info = await self.bilibili.qr_create()
            else:
                return json_err("unknown provider", 404)
        except Exception as exc:  # noqa: BLE001
            return json_err(f"二维码生成失败：{exc}", 502)
        return json_ok({
            "key": info["key"],
            "url": info["url"],
            "image": qr_data_uri(info["url"]),
        })

    async def auth_qr_poll(self, request: web.Request) -> web.Response:
        provider = request.match_info["provider"]
        key = request.query.get("key") or ""
        if not key:
            return json_err("缺少 key")
        try:
            if provider == "netease":
                result = await self.netease.qr_check(key)
            elif provider == "bilibili":
                result = await self.bilibili.qr_check(key)
            else:
                return json_err("unknown provider", 404)
        except Exception as exc:  # noqa: BLE001
            return json_err(f"轮询失败：{exc}", 502)

        if result.get("code") in (0, 803) and result.get("cookie"):
            account = ""
            uid = ""
            try:
                if provider == "netease":
                    info = await self.netease.account_info()
                else:
                    info = await self.bilibili.account_info()
                account = info.get("nickname", "")
                uid = info.get("uid", "")
            except Exception:
                pass
            repo.auth_set(provider, result["cookie"], account, uid)
            self._auth_cache = None
            self.hub.emit({"event": "auth", "provider": provider, "logged_in": True})
        return json_ok(result)

    async def auth_logout(self, request: web.Request) -> web.Response:
        provider = request.match_info["provider"]
        repo.auth_clear(provider)
        self._auth_cache = None
        if provider == "netease":
            self.netease.set_cookie("")
        elif provider == "bilibili":
            self.bilibili.set_cookie("")
        self.hub.emit({"event": "auth", "provider": provider, "logged_in": False})
        return json_ok()

    async def auth_web_login(self, request: web.Request) -> web.Response:
        """保存「网页登录」拿到的 cookie。

        前端先调原生桥弹真实窗口，拿到 cookie 后再 POST 上来，
        这样避免跨线程去操作 WebView2 的 cookie 管理器。
        """
        provider = request.match_info["provider"]
        if provider not in ("netease", "bilibili"):
            return json_err("unknown provider", 404)

        body: dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}

        cookie = str(body.get("cookie") or "").strip()
        if not cookie and self.web_login_cb is not None:
            try:
                cookie = await self.web_login_cb(provider) or ""
            except Exception as exc:  # noqa: BLE001
                return json_err(f"网页登录失败：{exc}", 500)

        if not cookie:
            return json_err("没有拿到 cookie，可能没登录成功")

        need = "MUSIC_U" if provider == "netease" else "SESSDATA"
        if need not in cookie:
            return json_err(f"cookie 里没有 {need}，登录可能没完成")

        if provider == "netease":
            self.netease.set_cookie(cookie)
        else:
            self.bilibili.set_cookie(cookie)

        account, uid = "", ""
        try:
            info = (await self.netease.account_info() if provider == "netease"
                    else await self.bilibili.account_info())
            account, uid = info.get("nickname", ""), info.get("uid", "")
        except Exception:
            pass
        repo.auth_set(provider, cookie, account, uid)
        self._auth_cache = None
        if uid and provider == "bilibili":
            try:
                self.config.bili_uid = int(uid)
                save_config(self.config)
            except (TypeError, ValueError):
                pass
        self.hub.emit({"event": "auth", "provider": provider, "logged_in": True})
        return json_ok({"account": account, "uid": uid})

    # ── 配置 / 杂项 ───────────────────────────────────────────────────

    async def get_config(self, request: web.Request) -> web.Response:
        return json_ok(config_as_dict(self.config))

    async def put_config(self, request: web.Request) -> web.Response:
        body = await request.json()
        from app.config import _build

        old_room = self.config.room_id
        old_prefix = self.config.command_prefix
        old_aliases = list(self.config.command_aliases or [])
        old_enabled = self.config.danmaku_enabled

        merged = _build(AppConfig, body)
        # 就地更新，保持引擎持有的同一对象
        for f in merged.__dataclass_fields__:
            setattr(self.config, f, getattr(merged, f))
        save_config(self.config)
        self.engine.set_mode(self.config.play_mode)
        self.engine.set_volume(self.config.volume)
        self.hub.emit({"event": "config", "config": config_as_dict(self.config)})

        # 只有直播间相关配置变了才重连，改主题/透明度不该打断弹幕
        changed = (
            old_room != self.config.room_id
            or old_prefix != self.config.command_prefix
            or old_aliases != list(self.config.command_aliases or [])
            or old_enabled != self.config.danmaku_enabled
        )
        danmaku = request.app.get("danmaku")
        if changed and danmaku is not None:
            import asyncio

            from app.danmaku_service import _normalize_prefixes

            danmaku.prefixes = _normalize_prefixes(self.config)
            asyncio.create_task(danmaku.restart())

        return json_ok(config_as_dict(self.config))

    async def history(self, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit") or 50)
        return json_ok(repo.history_recent(limit))

    async def cache_stats(self, request: web.Request) -> web.Response:
        return json_ok({
            "bytes": cache.total_size(),
            "files": len(list(cache.AUDIO_CACHE_DIR.glob("*"))),
        })

    async def cache_clear(self, request: web.Request) -> web.Response:
        removed = cache.clear_all()
        return json_ok({"removed": removed})

    # ── 弹幕提示 ──────────────────────────────────────────────────────

    async def reply_stats(self, request: web.Request) -> web.Response:
        """发送器状态 + 当前规则，设置页用它渲染。

        规则直接从 config 里取，保证「增删改」只有一个数据源。
        """
        cfg = self.config.danmaku_reply
        stats = self.replier.stats() if self.replier else {}
        return json_ok({
            "config": {
                "enabled": cfg.enabled,
                "rules": cfg.rules,
                "announce_enqueue": cfg.announce_enqueue,
                "announce_playing": cfg.announce_playing,
                "enqueue_template": cfg.enqueue_template,
                "playing_template": cfg.playing_template,
                "min_interval": cfg.min_interval,
            },
            "bili_logged_in": self.bilibili.is_logged_in(),
            "room_id": self.config.room_id,
            **stats,
        })

    async def reply_test(self, request: web.Request) -> web.Response:
        """立刻发一条测试弹幕，用来确认登录态和风控是否放行。"""
        body = await request.json() if request.can_read_body else {}
        text = str((body or {}).get("text") or "点歌机连接测试").strip()
        if self.replier is None:
            return json_err("发送器未初始化", 501)
        if not self.bilibili.is_logged_in():
            return json_err("B 站未登录，无法发弹幕")
        if not int(self.config.room_id or 0):
            return json_err("还没设置直播间房间号")

        try:
            await self.bilibili.send_danmaku(int(self.config.room_id), text)
        except Exception as exc:  # noqa: BLE001
            return json_err(f"发送失败：{exc}", 502)
        return json_ok({"sent": text})

    async def system_restart(self, request: web.Request) -> web.Response:
        """重启整个进程。

        端口这类配置只能在进程级别生效，热改没有意义。这里拉起一个新进程，
        稍后旧进程自行退出；新进程启动时会检测端口占用并接管。
        """
        if self.restart_cb is None:
            return json_err("当前模式不支持重启", 501)
        import asyncio

        asyncio.get_running_loop().call_later(0.6, self.restart_cb)
        return json_ok({"restarting": True})

    # ── 关于 / 更新 ───────────────────────────────────────────────────

    async def about(self, request: web.Request) -> web.Response:
        from app import APP_NAME, GITHUB_REPO, __version__

        return json_ok({
            "app_name": APP_NAME,
            "version": __version__,
            "repo": GITHUB_REPO,
            "repo_url": f"https://github.com/{GITHUB_REPO}",
            "author": "Xinbaji",
            "author_url": "https://github.com/xinbaji",
            "self_update": updater.can_self_update(),
        })

    async def update_check(self, request: web.Request) -> web.Response:
        result = await updater.check_update()
        return json_ok(result)

    async def update_apply(self, request: web.Request) -> web.Response:
        body = await request.json()
        url = str((body or {}).get("download_url") or "").strip()
        if not url or not url.startswith("https://"):
            return json_err("下载地址不合法")
        try:
            await updater.apply_update(url)
        except Exception as exc:  # noqa: BLE001
            return json_err(f"更新失败：{exc}", 502)
        # apply_update 成功时进程已经退出，走不到这里
        return json_ok()

    async def danmaku_state(self, request: web.Request) -> web.Response:
        return json_ok(self.danmaku_status)


def _extract(text: str, pattern: str) -> str:
    import re

    m = re.search(pattern, text)
    if not m:
        return ""
    for g in m.groups():
        if g:
            return g
    return ""


_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_NCM_PLAYLIST_RE = re.compile(r"music\.163\.com/(?:#/)?(?:playlist|my|discover/toplist)\?id=(\d+)")
_NCM_PLAYLIST_ALT = re.compile(r"music\.163\.com/(?:#/)?playlist/(\d+)")
_NCM_SONG_RE = re.compile(r"music\.163\.com/(?:#/)?song\?id=(\d+)")
_NCM_SONG_ALT = re.compile(r"music\.163\.com/(?:#/)?song/(\d+)")
_BILI_FAV_RE = re.compile(r"(?:fid|media_id)=(\d+)")
_BILI_ML_RE = re.compile(r"/ml(\d+)")


def classify_input(raw: str) -> tuple[str, str]:
    """把一段用户输入归类成可执行的导入动作。

    顺序很重要：先认 playlist 再认 song，因为歌单链接里也可能出现 song 字样。
    """
    text = (raw or "").strip()
    if not text:
        return "keyword", ""

    if _NCM_PLAYLIST_RE.search(text):
        return "ncm_playlist", _NCM_PLAYLIST_RE.search(text).group(1)  # type: ignore[union-attr]
    if _NCM_PLAYLIST_ALT.search(text):
        return "ncm_playlist", _NCM_PLAYLIST_ALT.search(text).group(1)  # type: ignore[union-attr]

    if _NCM_SONG_RE.search(text):
        return "ncm_song", _NCM_SONG_RE.search(text).group(1)  # type: ignore[union-attr]
    if _NCM_SONG_ALT.search(text):
        return "ncm_song", _NCM_SONG_ALT.search(text).group(1)  # type: ignore[union-attr]

    if "bilibili.com" in text and ("favlist" in text or "media_id" in text or "fid=" in text or "/ml" in text):
        m = _BILI_FAV_RE.search(text) or _BILI_ML_RE.search(text)
        if m:
            return "bili_fav", m.group(1)

    m = _BV_RE.search(text)
    if m:
        return "bili_video", m.group(0)

    # 纯数字：8 位以上按网易云歌曲 ID 处理（短号留给搜索）
    if text.isdigit() and len(text) >= 6:
        return "ncm_song", text

    return "keyword", text
