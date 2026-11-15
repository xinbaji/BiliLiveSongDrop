"""网易云音乐接口客户端。

统一走 `music.163.com/api/*` 的 GET 接口：实测比 weapi 稳定，不需要
加密签名，也不会被风控静默返回空 body。cookie 由外部注入，登录后热更新。
"""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from app.store.models import Song

BASE = "https://music.163.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
)

# 免费账号能拿到的最高音质；exhigh=320k 需要 VIP
QUALITY_LEVELS = {"standard", "higher", "exhigh", "lossless", "sky"}

_BASE_COOKIE = {
    "os": "pc",
    "appver": "8.9.70",
    "osver": "Microsoft-Windows-10-Professional-build-19045-64bit",
    "deviceId": "biliSinger",
    "channel": "netease",
}

# /api 接口对频率敏感，连续请求要留间隔，否则返回 -460
_MIN_INTERVAL = 0.12


class NeteaseError(RuntimeError):
    pass


def _cover(url: str) -> str:
    if not url:
        return ""
    url = url.replace("http://", "https://", 1)
    return f"{url}?param=400y400" if "?" not in url else url


def _artists(item: dict[str, Any]) -> str:
    arr = item.get("ar") or item.get("artists") or []
    return " / ".join(a.get("name", "") for a in arr if a.get("name"))


def _album(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("al") or item.get("album") or {}


def song_from_api(item: dict[str, Any]) -> Song:
    if not item or not item.get("id"):
        raise ValueError("无效的歌曲数据")
    album = _album(item)
    return Song(
        source="netease",
        source_id=str(item["id"]),
        title=item.get("name") or "",
        artists=_artists(item),
        album=album.get("name") or "",
        cover_url=_cover(album.get("picUrl") or ""),
        duration=int(item.get("dt") or item.get("duration") or 0),
        quality="",
    )


def parse_cookie_string(raw: str) -> dict[str, str]:
    jar: dict[str, str] = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        jar[k.strip()] = v.strip()
    return jar


def cookie_to_string(jar: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in jar.items() if v)


class NeteaseClient:
    def __init__(self, cookie: str = "") -> None:
        self._cookie = parse_cookie_string(cookie)
        self._session: aiohttp.ClientSession | None = None
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    # ── 状态 ──────────────────────────────────────────────────────────

    def set_cookie(self, cookie: str) -> None:
        self._cookie = parse_cookie_string(cookie)

    @property
    def cookie_string(self) -> str:
        return cookie_to_string(self._cookie)

    @property
    def logged_in(self) -> bool:
        return bool(self._cookie.get("MUSIC_U"))

    def _merged_cookie(self) -> dict[str, str]:
        jar = dict(_BASE_COOKIE)
        jar.update({k: v for k, v in self._cookie.items() if v})
        return jar

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                trust_env=True,  # 跟随系统代理
                timeout=aiohttp.ClientTimeout(total=15, connect=8),
                headers={
                    "User-Agent": UA,
                    "Referer": f"{BASE}/",
                    "Origin": BASE,
                    "Accept": "*/*",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _throttle(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            gap = loop.time() - self._last_call
            if gap < _MIN_INTERVAL:
                await asyncio.sleep(_MIN_INTERVAL - gap)
            self._last_call = loop.time()

    async def _api(self, path: str, **params: Any) -> Any:
        """GET 请求，返回解析后的 JSON；返回 None 表示被风控。"""
        await self._throttle()
        session = await self._get_session()
        async with session.get(
            f"{BASE}/api{path}",
            params=params,
            cookies=self._merged_cookie(),
            allow_redirects=False,
        ) as resp:
            if resp.status == 403:
                raise NeteaseError("请求被限流（403），稍后重试或降低频率")
            text = await resp.text()
        if not text.strip():
            raise NeteaseError("接口返回空响应，可能被风控")
        import json

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise NeteaseError(f"返回非 JSON：{text[:120]}") from exc
        code = data.get("code")
        if code == -460:
            raise NeteaseError("触发风控（-460），需要稍后重试")
        if code == 301:
            raise NeteaseError("需要登录")
        return data

    # ── 搜索 ──────────────────────────────────────────────────────────

    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        data = await self._api("/search/get/web", s=keyword, type=1, limit=limit, offset=0)
        rows = ((data.get("result") or {}).get("songs")) or []
        out: list[Song] = []
        for r in rows:
            try:
                out.append(song_from_api(r))
            except ValueError:
                continue
        return out

    # ── 详情 ──────────────────────────────────────────────────────────

    async def song_detail(self, song_ids: list[str]) -> list[Song]:
        if not song_ids:
            return []
        query = "[" + ",".join(str(i) for i in song_ids) + "]"
        data = await self._api("/song/detail/", ids=query)
        out: list[Song] = []
        for r in data.get("songs") or []:
            try:
                out.append(song_from_api(r))
            except ValueError:
                continue
        return out

    # ── 播放链接 ──────────────────────────────────────────────────────

    async def song_url(self, song_id: str, level: str = "exhigh") -> dict[str, Any]:
        """拿播放直链。url 为空表示无版权 / 需 VIP / 地区限制。"""
        if level not in QUALITY_LEVELS:
            level = "exhigh"
        data = await self._api(
            "/song/enhance/player/url/v1",
            ids=f"[{song_id}]", level=level, encodeType="aac",
        )
        rows = data.get("data") or []
        if not rows:
            return {"url": "", "br": 0, "size": 0, "trial": False, "fee": -1,
                    "level": level, "code": -1, "reason": "no_data"}
        row = rows[0] or {}
        url = row.get("url") or ""
        return {
            "url": url,
            "br": int(row.get("br") or 0),
            "size": int(row.get("size") or 0),
            "trial": bool(row.get("freeTrialInfo")),
            "fee": int(row.get("fee") if row.get("fee") is not None else -1),
            "level": row.get("level") or level,
            "code": row.get("code"),
            "reason": "" if url else _reason(row),
        }

    # ── 歌单 ──────────────────────────────────────────────────────────

    async def playlist_detail(self, playlist_id: int | str) -> tuple[str, list[Song]]:
        data = await self._api("/v6/playlist/detail", id=int(playlist_id), n=1000, s=0)
        pl = data.get("playlist") or {}
        name = pl.get("name") or f"歌单 {playlist_id}"
        track_ids = [str(t["id"]) for t in (pl.get("trackIds") or []) if t.get("id")]

        songs: list[Song] = []
        # 先按 detail 里已带的前 n 首填，剩下的再批量补
        for t in pl.get("tracks") or []:
            try:
                songs.append(song_from_api(t))
            except ValueError:
                continue
        got = {s.source_id for s in songs}
        missing = [i for i in track_ids if i not in got]
        for i in range(0, min(len(missing), 1000), 200):
            try:
                songs.extend(await self.song_detail(missing[i:i + 200]))
            except NeteaseError:
                break
        return name, songs

    # ── 歌词 ──────────────────────────────────────────────────────────

    async def lyric(self, song_id: str) -> str:
        data = await self._api("/song/lyric", id=song_id, lv=-1, kv=-1, tv=-1)
        return ((data.get("lrc") or {}).get("lyric")) or ""

    # ── 二维码登录 ────────────────────────────────────────────────────

    async def qr_create(self) -> dict[str, str]:
        data = await self._api("/login/qrcode/unikey", type=1)
        key = (data or {}).get("unikey") or ""
        if not key:
            raise NeteaseError(f"二维码 key 获取失败：{data}")
        return {"key": key, "url": f"https://music.163.com/login?codekey={key}"}

    async def qr_check(self, key: str) -> dict[str, Any]:
        """code: 800 过期 / 801 待扫 / 802 待确认 / 803 成功。"""
        await self._throttle()
        session = await self._get_session()
        async with session.get(
            f"{BASE}/api/login/qrcode/client/login",
            params={"key": key, "type": 1},
            cookies=self._merged_cookie(),
            allow_redirects=False,
        ) as resp:
            payload = await resp.json(content_type=None)
            merged_headers = resp.headers.getall("Set-Cookie", [])

        code = int((payload or {}).get("code") or 0)
        result: dict[str, Any] = {"code": code, "message": (payload or {}).get("message", "")}

        if code == 803:
            jar: dict[str, str] = {}
            for line in merged_headers:
                for part in line.split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        if k.strip() in ("MUSIC_U", "__csrf", "NMTID", "__remember_me", "MUSIC_A"):
                            jar[k.strip()] = v.strip()
            if jar:
                merged = dict(self._cookie)
                merged.update(jar)
                self._cookie = merged
            result["cookie"] = self.cookie_string
        return result

    # ── 账号 ──────────────────────────────────────────────────────────

    async def account_info(self) -> dict[str, Any]:
        data = await self._api("/nuser/account/get")
        profile = (data or {}).get("profile") or {}
        return {
            "logged_in": bool((data or {}).get("account")),
            "nickname": profile.get("nickname", ""),
            "uid": str(profile.get("userId", "") or ""),
            "vip": profile.get("vipType", 0) or 0,
            "avatar": _cover(profile.get("avatarUrl", "") or ""),
        }


def _reason(row: dict[str, Any]) -> str:
    code = row.get("code")
    fee = row.get("fee")
    if code == 404:
        return "no_copyright"
    if fee == 1:
        return "vip_required"
    if fee == 4:
        return "paid_album"
    if code == -110:
        return "region_blocked"
    return f"unavailable_{code}"
