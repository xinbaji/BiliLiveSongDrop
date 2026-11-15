"""仓储层：歌曲、歌单、队列、历史、登录态的读写。"""
from __future__ import annotations

from typing import Any

from app.security import decrypt_text, encrypt_text
from app.store.db import get_conn, kv_get, kv_set, now, tx
from app.store.models import Playlist, QueueItem, Song


# ── 歌曲 ──────────────────────────────────────────────────────────────

def upsert_song(song: Song) -> Song:
    """按 (source, source_id, extra) 幂等写入，返回带 id 的完整对象。"""
    with tx() as conn:
        row = conn.execute(
            """
            INSERT INTO songs(source, source_id, extra, title, artists, album,
                              cover_url, duration, quality, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source, source_id, extra) DO UPDATE SET
                title=excluded.title,
                artists=excluded.artists,
                album=excluded.album,
                cover_url=CASE WHEN excluded.cover_url='' THEN songs.cover_url
                               ELSE excluded.cover_url END,
                duration=CASE WHEN excluded.duration=0 THEN songs.duration
                              ELSE excluded.duration END,
                quality=CASE WHEN excluded.quality='' THEN songs.quality
                             ELSE excluded.quality END
            RETURNING *
            """,
            (
                song.source, song.source_id, song.extra, song.title, song.artists,
                song.album, song.cover_url, song.duration, song.quality, now(),
            ),
        ).fetchone()
    return Song.from_row(row)


def get_song(song_id: int) -> Song | None:
    row = get_conn().execute("SELECT * FROM songs WHERE id=?", (song_id,)).fetchone()
    return Song.from_row(row) if row else None


def find_song(source: str, source_id: str, extra: str = "") -> Song | None:
    row = get_conn().execute(
        "SELECT * FROM songs WHERE source=? AND source_id=? AND extra=?",
        (source, source_id, extra),
    ).fetchone()
    return Song.from_row(row) if row else None


# ── 歌单 ──────────────────────────────────────────────────────────────

def list_playlists() -> list[Playlist]:
    rows = get_conn().execute(
        """
        SELECT p.*, (SELECT COUNT(*) FROM playlist_items i WHERE i.playlist_id=p.id) AS cnt
        FROM playlists p
        ORDER BY p.kind='idle' DESC, p.sort_order ASC, p.id ASC
        """
    ).fetchall()
    return [
        Playlist(
            id=r["id"], name=r["name"], kind=r["kind"],
            sort_order=r["sort_order"], created_at=r["created_at"], count=r["cnt"],
            remote_type=r["remote_type"], remote_id=r["remote_id"],
            synced_at=r["synced_at"],
        )
        for r in rows
    ]


def create_playlist(
    name: str, kind: str = "normal", remote_type: str = "", remote_id: str = ""
) -> Playlist:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO playlists(name, kind, sort_order, created_at, remote_type, remote_id) "
            "VALUES(?,?,?,?,?,?)",
            (name, kind, 0, now(), remote_type, remote_id),
        )
        pid = cur.lastrowid
    return Playlist(
        id=pid, name=name, kind=kind, created_at=now(),
        remote_type=remote_type, remote_id=remote_id,
    )


def get_playlist(pid: int) -> Playlist | None:
    for p in list_playlists():
        if p.id == pid:
            return p
    return None


def set_playlist_remote(pid: int, remote_type: str, remote_id: str) -> None:
    """把已有歌单绑定成某个远程歌单，之后就能走刷新。"""
    with tx() as conn:
        conn.execute(
            "UPDATE playlists SET remote_type=?, remote_id=? WHERE id=?",
            (remote_type, remote_id, pid),
        )


def touch_playlist_synced(pid: int) -> None:
    with tx() as conn:
        conn.execute("UPDATE playlists SET synced_at=? WHERE id=?", (now(), pid))


def rename_playlist(pid: int, name: str) -> None:
    with tx() as conn:
        conn.execute("UPDATE playlists SET name=? WHERE id=?", (name, pid))


def delete_playlist(pid: int) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM playlists WHERE id=?", (pid,))


def playlist_songs(pid: int) -> list[Song]:
    rows = get_conn().execute(
        """
        SELECT s.* FROM playlist_items i
        JOIN songs s ON s.id = i.song_id
        WHERE i.playlist_id=?
        ORDER BY i.sort_order ASC, i.id ASC
        """,
        (pid,),
    ).fetchall()
    return [Song.from_row(r) for r in rows]


def playlist_add(pid: int, song_id: int, origin: str = "manual") -> bool:
    """加入歌单，已存在则跳过。

    origin 记「这首歌是怎么来的」：remote = 从远程歌单同步进来的，
    manual = 手动/搜索/收藏加进来的。刷新时只删 remote 的那些，
    手动加的不动。
    """
    with tx() as conn:
        nxt = conn.execute(
            "SELECT COALESCE(MAX(sort_order),0)+1 FROM playlist_items WHERE playlist_id=?",
            (pid,),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT OR IGNORE INTO playlist_items(playlist_id, song_id, sort_order, origin) "
            "VALUES(?,?,?,?)",
            (pid, song_id, nxt, origin),
        )
        return cur.rowcount > 0


def playlist_remote_song_ids(pid: int) -> set[int]:
    """该歌单里由远程同步加入的歌曲 id 集合。"""
    rows = get_conn().execute(
        "SELECT song_id FROM playlist_items WHERE playlist_id=? AND origin='remote'",
        (pid,),
    ).fetchall()
    return {r["song_id"] for r in rows}


def playlist_delete_items(pid: int, song_ids: list[int]) -> int:
    if not song_ids:
        return 0
    marks = ",".join("?" * len(song_ids))
    with tx() as conn:
        cur = conn.execute(
            f"DELETE FROM playlist_items WHERE playlist_id=? AND song_id IN ({marks})",
            (pid, *song_ids),
        )
        return cur.rowcount


def playlist_has_item(pid: int, song_id: int) -> bool:
    row = get_conn().execute(
        "SELECT 1 FROM playlist_items WHERE playlist_id=? AND song_id=?",
        (pid, song_id),
    ).fetchone()
    return row is not None


def playlist_remove(pid: int, song_id: int) -> None:
    with tx() as conn:
        conn.execute(
            "DELETE FROM playlist_items WHERE playlist_id=? AND song_id=?", (pid, song_id)
        )


def playlist_clear(pid: int) -> int:
    """清空歌单里的歌，但保留歌单本身。"""
    with tx() as conn:
        cur = conn.execute("DELETE FROM playlist_items WHERE playlist_id=?", (pid,))
        return cur.rowcount


def playlist_reorder(pid: int, ordered_song_ids: list[int]) -> None:
    with tx() as conn:
        for idx, sid in enumerate(ordered_song_ids):
            conn.execute(
                "UPDATE playlist_items SET sort_order=? WHERE playlist_id=? AND song_id=?",
                (idx, pid, sid),
            )


def playlist_count(pid: int) -> int:
    return get_conn().execute(
        "SELECT COUNT(*) FROM playlist_items WHERE playlist_id=?", (pid,)
    ).fetchone()[0]


# ── 点歌队列 ──────────────────────────────────────────────────────────

def queue_list(status: str = "pending") -> list[QueueItem]:
    rows = get_conn().execute(
        """
        SELECT q.*, s.id AS s_id, s.source, s.source_id, s.extra, s.title, s.artists,
               s.album, s.cover_url, s.duration, s.quality, s.created_at AS s_created
        FROM queue q JOIN songs s ON s.id = q.song_id
        WHERE q.status=?
        ORDER BY q.priority DESC, q.id ASC
        """,
        (status,),
    ).fetchall()
    return [_row_to_queue_item(r) for r in rows]


def queue_list_active() -> list[QueueItem]:
    """队列视图：正在播的那一首 + 所有待播的。

    正在播放的曲目标记成 playing 后如果直接从列表里消失，
    主播看到的就是「点了歌但队列是空的」。所以队列要一直包含它，
    等播完（done）或失败（failed）才真正移出。
    """
    rows = get_conn().execute(
        """
        SELECT q.*, s.id AS s_id, s.source, s.source_id, s.extra, s.title, s.artists,
               s.album, s.cover_url, s.duration, s.quality, s.created_at AS s_created
        FROM queue q JOIN songs s ON s.id = q.song_id
        WHERE q.status IN ('playing', 'pending')
        ORDER BY CASE q.status WHEN 'playing' THEN 0 ELSE 1 END,
                 q.priority DESC, q.id ASC
        """
    ).fetchall()
    return [_row_to_queue_item(r) for r in rows]


def queue_count_active() -> int:
    row = get_conn().execute(
        "SELECT COUNT(*) AS n FROM queue WHERE status IN ('playing', 'pending')"
    ).fetchone()
    return int(row["n"]) if row else 0


def _row_to_queue_item(r) -> QueueItem:
    song = Song(
        id=r["s_id"], source=r["source"], source_id=r["source_id"], extra=r["extra"],
        title=r["title"], artists=r["artists"], album=r["album"],
        cover_url=r["cover_url"], duration=r["duration"], quality=r["quality"],
        created_at=r["s_created"],
    )
    return QueueItem(
        id=r["id"], song=song, requester=r["requester"],
        requester_uid=r["requester_uid"], priority=r["priority"],
        status=r["status"], created_at=r["created_at"],
    )


def queue_find_duplicate(song_id: int) -> QueueItem | None:
    row = get_conn().execute(
        """
        SELECT q.*, s.id AS s_id, s.source, s.source_id, s.extra, s.title, s.artists,
               s.album, s.cover_url, s.duration, s.quality, s.created_at AS s_created
        FROM queue q JOIN songs s ON s.id = q.song_id
        WHERE q.status='pending' AND q.song_id=?
        """,
        (song_id,),
    ).fetchone()
    return _row_to_queue_item(row) if row else None


def queue_add(
    song_id: int, requester: str = "", requester_uid: str = "", priority: int = 0
) -> int:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO queue(song_id, requester, requester_uid, priority, status, created_at) "
            "VALUES(?,?,?,?,'pending',?)",
            (song_id, requester, requester_uid, priority, now()),
        )
        return cur.lastrowid


def queue_position(queue_id: int) -> int:
    """返回 1 基的排队序号，口径与队列列表一致（含正在播的那一首）。

    如果只按 pending 算，界面上正在播的排第一、新歌排第二，
    但回执会说「第 1 位」，观众一对不上就会以为系统算错了。
    """
    rows = get_conn().execute(
        "SELECT id FROM queue WHERE status IN ('playing','pending') "
        "ORDER BY CASE status WHEN 'playing' THEN 0 ELSE 1 END, priority DESC, id ASC"
    ).fetchall()
    for idx, r in enumerate(rows, start=1):
        if r["id"] == queue_id:
            return idx
    return 0


def queue_remove(queue_id: int) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM queue WHERE id=?", (queue_id,))


def queue_mark(queue_id: int, status: str) -> None:
    with tx() as conn:
        conn.execute("UPDATE queue SET status=? WHERE id=?", (status, queue_id))


def queue_promote(queue_id: int) -> None:
    with tx() as conn:
        conn.execute("UPDATE queue SET priority=1 WHERE id=?", (queue_id,))


def queue_clear() -> int:
    """清空队列。

    原来只删 pending，正在播的那首会留下一条 playing 永久挂在列表里，
    清空后界面依然显示有歌。这里整体清掉，语义才和按钮一致。
    """
    with tx() as conn:
        cur = conn.execute("DELETE FROM queue")
        return cur.rowcount


def queue_remove_by_requester(uid: str) -> int:
    with tx() as conn:
        cur = conn.execute(
            "DELETE FROM queue WHERE requester_uid=? AND status='pending'", (uid,)
        )
        return cur.rowcount


def queue_reorder(ordered_ids: list[int]) -> None:
    """把队列按给定顺序落成 priority，越大越靠前。"""
    total = len(ordered_ids)
    with tx() as conn:
        for idx, qid in enumerate(ordered_ids):
            conn.execute(
                "UPDATE queue SET priority=? WHERE id=?", (total - idx, qid)
            )


def queue_pop_next() -> QueueItem | None:
    rows = queue_list("pending")
    return rows[0] if rows else None


# ── 播放历史 ──────────────────────────────────────────────────────────

def history_add(song_id: int, requester: str, source: str, playlist_id: int = 0) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO history(song_id, requester, played_at, playlist_id, source) "
            "VALUES(?,?,?,?,?)",
            (song_id, requester, now(), playlist_id, source),
        )


def history_recent(limit: int = 50) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        """
        SELECT h.*, s.title, s.artists, s.cover_url, s.source, s.source_id, s.duration
        FROM history h LEFT JOIN songs s ON s.id = h.song_id
        ORDER BY h.played_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def history_recent_song_ids(limit: int = 200) -> set[int]:
    rows = get_conn().execute(
        "SELECT song_id FROM history ORDER BY played_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return {r["song_id"] for r in rows}


# ── 登录态 ────────────────────────────────────────────────────────────

def auth_get(provider: str) -> dict[str, str] | None:
    row = get_conn().execute("SELECT * FROM auth WHERE provider=?", (provider,)).fetchone()
    if not row:
        return None
    cookie = decrypt_text(row["cookie"])
    if not cookie:
        return None
    return {"cookie": cookie, "account": row["account"], "uid": row["uid"],
            "updated_at": str(row["updated_at"])}


def auth_set(provider: str, cookie: str, account: str = "", uid: str = "") -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO auth(provider, cookie, account, uid, updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(provider) DO UPDATE SET "
            "cookie=excluded.cookie, account=excluded.account, uid=excluded.uid, "
            "updated_at=excluded.updated_at",
            (provider, encrypt_text(cookie), account, uid, now()),
        )


def auth_clear(provider: str) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM auth WHERE provider=?", (provider,))


__all__ = [
    "upsert_song", "get_song", "find_song",
    "list_playlists", "create_playlist", "rename_playlist", "delete_playlist",
    "playlist_songs", "playlist_add", "playlist_remove", "playlist_reorder", "playlist_count",
    "queue_list", "queue_add", "queue_position", "queue_remove", "queue_mark",
    "queue_promote", "queue_clear", "queue_remove_by_requester", "queue_reorder",
    "queue_pop_next", "queue_find_duplicate",
    "history_add", "history_recent", "history_recent_song_ids",
    "auth_get", "auth_set", "auth_clear",
    "kv_get", "kv_set",
]
