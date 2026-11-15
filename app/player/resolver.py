"""音源解析：把 Song 变成一条可直接拉流的 URL。

两条铁律：
1. B 站音频必须带 Referer/UA，且 URL 有时效，所以只能后端代理，不能前端直连；
2. 网易云拿不到链接时（无版权/VIP/地区限制）换源到 B 站，但保留原曲的
   封面与歌手信息 —— 只在播放层换音源，展示层不变。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.sources.bilibili.client import BilibiliClient, BilibiliError
from app.sources.netease.client import NeteaseClient, NeteaseError
from app.store.models import SOURCE_BILIBILI, SOURCE_NETEASE, Song


@dataclass
class ResolvedAudio:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    quality: str = ""
    # 实际音源（换源后是 B 站视频），用于下载与缓存
    play_song: Song | None = None
    # 展示元数据（永远是用户点的那首）
    display_song: Song | None = None
    fallback: bool = False
    note: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.url)


class AudioResolver:
    def __init__(
        self,
        netease: NeteaseClient,
        bilibili: BilibiliClient,
        quality: str = "exhigh",
        fallback_to_bili: bool = True,
    ) -> None:
        self.netease = netease
        self.bilibili = bilibili
        self.quality = quality
        self.fallback_to_bili = fallback_to_bili

    async def resolve(self, song: Song) -> ResolvedAudio:
        if song.source == SOURCE_NETEASE:
            result = await self._netease(song)
            if result.ok:
                return result
            if self.fallback_to_bili:
                alt = await self._switch_to_bili(song)
                if alt.ok:
                    return alt
            return ResolvedAudio(url="", note=result.note, display_song=song, play_song=song)
        return await self._bilibili(song)

    # ── 网易云 ────────────────────────────────────────────────────────

    async def _netease(self, song: Song) -> ResolvedAudio:
        try:
            info = await self.netease.song_url(song.source_id, self.quality)
        except NeteaseError as exc:
            return ResolvedAudio(url="", note=f"网易云接口异常：{exc}", display_song=song, play_song=song)

        if not info.get("url"):
            return ResolvedAudio(url="", note=info.get("reason") or "unavailable",
                                 display_song=song, play_song=song)
        return ResolvedAudio(
            url=info["url"],
            headers={},  # 网易云直链无需特殊请求头
            quality=info.get("level") or self.quality,
            display_song=song,
            play_song=song,
        )

    # ── B 站 ──────────────────────────────────────────────────────────

    async def _bilibili(self, song: Song) -> ResolvedAudio:
        cid = song.extra
        try:
            if not cid:
                info = await self.bilibili.video_info(song.source_id)
                cid = str(info.get("cid") or "")
            if not cid:
                return ResolvedAudio(url="", note="缺少 cid", display_song=song, play_song=song)
            audio = await self.bilibili.audio_url(song.source_id, cid)
        except (BilibiliError, Exception) as exc:  # noqa: BLE001
            return ResolvedAudio(url="", note=f"B 站解析失败：{exc}", display_song=song, play_song=song)

        if not audio.get("url"):
            return ResolvedAudio(url="", note=audio.get("reason") or "unavailable",
                                 display_song=song, play_song=song)
        return ResolvedAudio(
            url=audio["url"],
            headers=await self.bilibili.audio_request_headers(),
            quality=audio.get("quality", ""),
            display_song=song,
            play_song=song,
        )

    # ── 换源 ──────────────────────────────────────────────────────────

    async def _switch_to_bili(self, song: Song) -> ResolvedAudio:
        keyword = f"{song.title} {song.artists}".strip()
        try:
            candidates = await self.bilibili.search(keyword, 5)
        except (BilibiliError, Exception) as exc:  # noqa: BLE001
            return ResolvedAudio(url="", note=f"换源搜索失败：{exc}", display_song=song, play_song=song)

        if not candidates:
            return ResolvedAudio(url="", note="B 站也搜不到", display_song=song, play_song=song)

        # 优先挑时长接近的，避开一片 15 秒的切片集锦
        def score(c: Song) -> tuple[int, int]:
            if song.duration and c.duration:
                diff = abs(c.duration - song.duration)
                return (0 if diff <= 45_000 else 1, diff)
            return (2, 0)

        for cand in sorted(candidates, key=score)[:3]:
            try:
                info = await self.bilibili.video_info(cand.source_id)
                cid = str(info.get("cid") or "")
                if not cid:
                    continue
                audio = await self.bilibili.audio_url(cand.source_id, cid)
            except (BilibiliError, Exception):  # noqa: BLE001
                continue
            if not audio.get("url"):
                continue

            play = Song(
                source=SOURCE_BILIBILI,
                source_id=cand.source_id,
                extra=cid,
                title=song.title,          # 展示层仍用原曲信息
                artists=song.artists,
                album=song.album,
                cover_url=song.cover_url,
                duration=song.duration or cand.duration,
                quality=audio.get("quality", ""),
            )
            return ResolvedAudio(
                url=audio["url"],
                headers=await self.bilibili.audio_request_headers(),
                quality=audio.get("quality", ""),
                display_song=song,
                play_song=play,
                fallback=True,
                note=f"换源到 B 站 {cand.source_id}",
            )

        return ResolvedAudio(url="", note="换源后仍无可用音频", display_song=song, play_song=song)
