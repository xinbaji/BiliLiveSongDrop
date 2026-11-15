"""数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SOURCE_NETEASE = "netease"
SOURCE_BILIBILI = "bilibili"


@dataclass
class Song:
    """一首可播放的曲子。source + source_id + extra 唯一标识一个音源。"""

    source: str
    source_id: str
    title: str
    artists: str = ""
    album: str = ""
    cover_url: str = ""
    duration: int = 0          # 毫秒
    quality: str = ""
    extra: str = ""            # B 站分 P 的 cid，网易云留空
    id: int = 0
    created_at: int = 0

    @property
    def key(self) -> str:
        """缓存文件名与前端引用的稳定标识。"""
        parts = [self.source, self.source_id]
        if self.extra:
            parts.append(self.extra)
        return "_".join(parts)

    @property
    def is_bilibili(self) -> bool:
        return self.source == SOURCE_BILIBILI

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "source": self.source,
            "source_id": self.source_id,
            "extra": self.extra,
            "title": self.title,
            "artists": self.artists,
            "album": self.album,
            "cover_url": self.cover_url,
            "duration": self.duration,
            "quality": self.quality,
        }

    @classmethod
    def from_row(cls, row) -> "Song":
        return cls(
            id=row["id"],
            source=row["source"],
            source_id=row["source_id"],
            extra=row["extra"],
            title=row["title"],
            artists=row["artists"],
            album=row["album"],
            cover_url=row["cover_url"],
            duration=row["duration"],
            quality=row["quality"],
            created_at=row["created_at"],
        )


@dataclass
class QueueItem:
    id: int
    song: Song
    requester: str = ""
    requester_uid: str = ""
    priority: int = 0
    status: str = "pending"
    created_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_id": self.id,
            "song": self.song.to_dict(),
            "requester": self.requester,
            "requester_uid": self.requester_uid,
            "priority": self.priority,
            "status": self.status,
        }


@dataclass
class Playlist:
    id: int
    name: str
    kind: str = "normal"     # normal | idle
    sort_order: int = 0
    created_at: int = 0
    count: int = 0
    # 远程歌单来源，用于「刷新」时重新拉取对比。非远程导入的留空。
    remote_type: str = ""
    remote_id: str = ""
    synced_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "sort_order": self.sort_order,
            "count": self.count,
            "remote_type": self.remote_type,
            "remote_id": self.remote_id,
            "synced_at": self.synced_at,
            # 只对远程导入的歌单开放「刷新」
            "can_refresh": bool(self.remote_type and self.remote_id),
        }


@dataclass
class Track:
    """传给前端的当前播放曲目。"""

    song: Song
    requester: str = ""
    playlist_id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.song.to_dict(),
            "requester": self.requester,
            "playlist_id": self.playlist_id,
        }
