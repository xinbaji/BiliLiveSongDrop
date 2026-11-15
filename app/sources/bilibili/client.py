"""B 站接口客户端：WBI 签名、搜索、视频信息、音频直链、扫码登录。

音频直链有两个硬约束，决定了必须走后端代理：
1. 必须带 `Referer: https://www.bilibili.com`，浏览器 <audio> 无法自定义该请求头；
2. URL 带 deadline 参数，约 2 小时过期，必须在播放前现取。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

from app.store.models import Song

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
)
REFERER = "https://www.bilibili.com"

API = "https://api.bilibili.com"
PASSPORT = "https://passport.bilibili.com"
LIVE = "https://api.live.bilibili.com"

# WBI 混合密钥的重排表，官方前端固定值
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

# 音频档位：id → 说明。数值越大音质越好
AUDIO_QUALITY = {
    30216: ("64k", "m4a"),
    30232: ("132k", "m4a"),
    30280: ("192k", "m4a"),
    30250: ("dolby", "ec3"),
    30251: ("hires", "flac"),
}


class BilibiliError(RuntimeError):
    pass


def _mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in _MIXIN_TAB)[:32]


def _wbi_query(params: dict[str, Any], img_key: str, sub_key: str) -> str:
    """构造带 w_rid 的完整 query string。

    关键：必须用 quote（空格→%20）而不是 urlencode 默认的 quote_plus（空格→+）。
    签名用的 query 和真正发出去的必须逐字节一致，否则 w_rid 校验失败——
    而 B 站校验失败时不报错，只返回空结果，很难排查。
    """
    mixin = _mixin_key(img_key + sub_key)
    signed = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
    signed["wts"] = str(int(time.time()))
    query = urlencode(sorted(signed.items()), quote_via=quote)
    w_rid = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
    return f"{query}&w_rid={w_rid}"


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


def _cover(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    return url.replace("http://", "https://", 1)


def parse_duration(raw: Any) -> int:
    """B 站搜索接口的 duration 可能是秒数，也可能是 "4:30" / "1:02:03"。"""
    if isinstance(raw, (int, float)):
        return int(raw) * 1000
    text = str(raw or "").strip()
    if not text:
        return 0
    if ":" in text:
        total = 0
        for part in text.split(":"):
            try:
                total = total * 60 + int(part)
            except ValueError:
                return 0
        return total * 1000
    try:
        return int(text) * 1000
    except ValueError:
        return 0


class BilibiliClient:
    def __init__(self, cookie: str = "", uid: int = 0) -> None:
        self._cookie = parse_cookie_string(cookie)
        self.uid = uid
        self._session: aiohttp.ClientSession | None = None
        self._wbi: tuple[str, str] | None = None
        self._wbi_at = 0.0

    # ── 基础 ──────────────────────────────────────────────────────────

    def set_cookie(self, cookie: str) -> None:
        self._cookie = parse_cookie_string(cookie)

    @property
    def cookie_string(self) -> str:
        return cookie_to_string(self._cookie)

    def is_logged_in(self) -> bool:
        """发弹幕需要同时有 SESSDATA（身份）和 bili_jct（CSRF）。"""
        return bool(self._cookie.get("SESSDATA") and self._cookie.get("bili_jct"))

    @property
    def logged_in(self) -> bool:
        return bool(self._cookie.get("SESSDATA"))

    def _merged_cookie(self) -> dict[str, str]:
        jar = {"buvid3": self._cookie.get("buvid3", "")}
        jar.update({k: v for k, v in self._cookie.items() if v})
        return jar

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                trust_env=True,
                timeout=aiohttp.ClientTimeout(total=15, connect=8),
                headers={
                    "User-Agent": UA,
                    "Referer": REFERER,
                    "Origin": REFERER,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _json(
        self, url: str, params: dict[str, Any] | None = None,
        allow_codes: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        session = await self._get_session()
        async with session.get(
            url, params=params, cookies=self._merged_cookie(), allow_redirects=True
        ) as resp:
            if resp.status != 200:
                raise BilibiliError(f"HTTP {resp.status} @ {url}")
            data = await resp.json(content_type=None)
        code = data.get("code")
        if code not in (0, None) and code not in allow_codes:
            raise BilibiliError(f"接口错误 {code}: {data.get('message') or data.get('msg')}")
        return data

    async def ensure_buvid(self) -> None:
        """没有 buvid3 时向指纹接口要一个，否则后续接口风控概率很高。"""
        if self._cookie.get("buvid3"):
            return
        try:
            data = await self._json(f"{API}/x/frontend/finger/spi")
            info = data.get("data") or {}
            if info.get("b_3"):
                self._cookie["buvid3"] = info["b_3"]
            if info.get("b_4"):
                self._cookie["buvid4"] = info["b_4"]
        except (BilibiliError, aiohttp.ClientError):
            pass

    # ── WBI ───────────────────────────────────────────────────────────

    async def _wbi_keys(self) -> tuple[str, str]:
        if self._wbi and time.time() - self._wbi_at < 3600:
            return self._wbi
        # 未登录时 nav 返回 -101，但 wbi_img 依然在 data 里，所以要放行
        data = await self._json(f"{API}/x/web-interface/nav", allow_codes=(-101,))
        wbi = ((data.get("data") or {}).get("wbi_img")) or {}
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if not img or not sub:
            raise BilibiliError("WBI 密钥获取失败")
        self._wbi = (img, sub)
        self._wbi_at = time.time()
        return self._wbi

    # ── 搜索 ──────────────────────────────────────────────────────────

    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        await self.ensure_buvid()
        img, sub = await self._wbi_keys()
        query = _wbi_query(
            {"search_type": "video", "keyword": keyword, "page": 1,
             "page_size": min(limit, 20)},
            img, sub,
        )
        data = await self._json(f"{API}/x/web-interface/wbi/search/type?{query}")
        rows = ((data.get("data") or {}).get("result")) or []
        out: list[Song] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            if r.get("type") and r["type"] != "video":
                continue
            bvid = r.get("bvid") or ""
            if not bvid:
                continue
            try:
                out.append(
                    Song(
                        source="bilibili",
                        source_id=bvid,
                        title=_strip_tags(r.get("title") or ""),
                        artists=r.get("author") or "",
                        album="bilibili",
                        cover_url=_cover(r.get("pic") or ""),
                        duration=parse_duration(r.get("duration")),
                    )
                )
            except (ValueError, TypeError):
                # 单条脏数据不该拖垮整批搜索结果
                continue
            if len(out) >= limit:
                break
        return out

    # ── 视频信息 ──────────────────────────────────────────────────────

    async def video_info(self, bvid: str) -> dict[str, Any]:
        """拿 cid / 分P / 封面 / 时长。音频直链必须带 cid。"""
        data = await self._json(f"{API}/x/web-interface/view", {"bvid": bvid})
        v = data.get("data") or {}
        pages = v.get("pages") or []
        main = pages[0] if pages else {}
        return {
            "bvid": v.get("bvid", bvid),
            "aid": v.get("aid"),
            "cid": main.get("cid") or v.get("cid"),
            "title": v.get("title", ""),
            "owner": ((v.get("owner") or {}).get("name")) or "",
            "cover": _cover(v.get("pic") or ""),
            "duration": int(v.get("duration") or 0) * 1000,
            "pages": [
                {"cid": p.get("cid"), "page": p.get("page"), "part": p.get("part", "")}
                for p in pages
            ],
        }

    async def resolve_song(self, bvid: str) -> Song:
        """BV 号 → 可入库的 Song（含 cid 与补全的元数据）。"""
        info = await self.video_info(bvid)
        if not info.get("cid"):
            raise BilibiliError(f"视频 {bvid} 没有可用的 cid")
        return Song(
            source="bilibili",
            source_id=info["bvid"],
            extra=str(info["cid"]),
            title=info["title"],
            artists=info["owner"],
            album="bilibili",
            cover_url=info["cover"],
            duration=info["duration"],
        )

    # ── 音频直链 ──────────────────────────────────────────────────────

    async def audio_url(self, bvid: str, cid: str | int) -> dict[str, Any]:
        """返回 {url, quality, codec, size_hint}。url 为空表示拿不到。"""
        params = {
            "bvid": bvid,
            "cid": cid,
            "fnval": 16,       # 16 = DASH，只有 dash 才有独立音频轨
            "fnver": 0,
            "fourk": 1,
        }
        data = await self._json(f"{API}/x/player/playurl", params)
        dash = ((data.get("data") or {}).get("dash")) or {}
        audios = dash.get("audio") or []
        if not audios:
            return {"url": "", "quality": "", "codec": "", "reason": "no_dash_audio"}

        # flac / dolby 优先，其次按带宽降序
        def rank(a: dict[str, Any]) -> tuple[int, int]:
            aid = int(a.get("id") or 0)
            tier = {30251: 3, 30250: 2, 30280: 1}.get(aid, 0)
            return (tier, int(a.get("bandwidth") or 0))

        best = max(audios, key=rank)
        url = best.get("baseUrl") or best.get("base_url") or ""
        if not url and best.get("backupUrl"):
            url = best["backupUrl"][0]
        quality, codec = AUDIO_QUALITY.get(int(best.get("id") or 0), ("unknown", "m4a"))
        return {
            "url": url,
            "quality": quality,
            "codec": best.get("codecs") or codec,
            "bandwidth": int(best.get("bandwidth") or 0),
            "reason": "" if url else "empty_url",
        }

    async def audio_request_headers(self) -> dict[str, str]:
        """抓音频时必须原样带上的头，少一个就 403。"""
        return {
            "User-Agent": UA,
            "Referer": REFERER,
            "Origin": REFERER,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

    # ── 直播间 ────────────────────────────────────────────────────────

    async def room_info(self, room_id: int) -> dict[str, Any]:
        """房间标题、主播、封面、在线人数、直播状态。

        和 getDanmuInfo 一样，这个接口不带 WBI 签名会直接返回 -352。
        """
        await self.ensure_buvid()
        img, sub = await self._wbi_keys()
        query = _wbi_query(
            {"room_id": room_id, "web_location": "444.8"}, img, sub
        )
        data = await self._json(
            f"{LIVE}/xlive/web-room/v1/index/getInfoByRoom?{query}"
        )
        d = data.get("data") or {}
        room = d.get("room_info") or {}
        base = ((d.get("anchor_info") or {}).get("base_info")) or {}
        if not room:
            raise BilibiliError(f"房间 {room_id} 信息为空，可能房间号不对")
        return {
            "room_id": room.get("room_id") or room_id,
            "short_id": room.get("short_id") or 0,
            "uid": room.get("uid"),
            "title": room.get("title") or "",
            "cover": _cover(room.get("cover") or room.get("keyframe") or ""),
            "keyframe": _cover(room.get("keyframe") or ""),
            "area": room.get("area_name") or "",
            "parent_area": room.get("parent_area_name") or "",
            "live_status": int(room.get("live_status") or 0),  # 1 直播中
            "online": int(room.get("online") or 0),
            "anchor": base.get("uname") or "",
            "anchor_face": _cover(base.get("face") or ""),
        }

    # ── 直播弹幕 ──────────────────────────────────────────────────────

    async def danmu_info(self, room_id: int) -> tuple[str, list[dict[str, Any]]]:
        """拿弹幕服务器 token 与节点列表。

        这个接口不带 WBI 签名会返回 -352（风控），且 -352 不带任何提示信息。
        """
        await self.ensure_buvid()

        from app.log import get

        log = get("bili")
        last: Exception | None = None
        for attempt in range(2):
            img, sub = await self._wbi_keys()
            query = _wbi_query(
                {"id": room_id, "type": 0, "web_location": "444.8"}, img, sub
            )
            log.debug("danmu_info 第%d次 room=%s wbi_img=%s buvid3=%s cookie_keys=%s",
                      attempt + 1, room_id, img[:10],
                      (self._cookie.get("buvid3") or "")[:10],
                      sorted(self._cookie.keys()))
            try:
                data = await self._json(
                    f"{LIVE}/xlive/web-room/v1/index/getDanmuInfo?{query}"
                )
            except BilibiliError as exc:
                last = exc
                # -352 = 签名/风控校验失败。两个可能：
                # 1) WBI 密钥被轮换，缓存里的签不对；
                # 2) buvid3 指纹被风控标记，换一个就好。
                # 重试时两个都换掉，取不到新指纹就还原旧的，不至于更糟。
                if "-352" in str(exc) and attempt == 0:
                    self._wbi = None
                    self._wbi_at = 0.0
                    saved = {k: self._cookie.get(k, "") for k in ("buvid3", "buvid4")}
                    self._cookie.pop("buvid3", None)
                    self._cookie.pop("buvid4", None)
                    await self.ensure_buvid()
                    if not self._cookie.get("buvid3"):
                        self._cookie.update({k: v for k, v in saved.items() if v})
                    continue
                raise

            info = data.get("data") or {}
            token = info.get("token") or ""
            hosts = info.get("host_list") or []
            if not token or not hosts:
                raise BilibiliError("弹幕服务器信息为空，房间号可能不存在")
            return token, hosts

        raise last or BilibiliError("弹幕服务器信息获取失败")

    # ── 收藏夹（登录后可用） ──────────────────────────────────────────

    async def fav_folders(self) -> list[dict[str, Any]]:
        if not self.uid:
            raise BilibiliError("未登录或缺少 UID")
        data = await self._json(
            f"{API}/x/v3/fav/folder/created/list-all", {"up_mid": self.uid}
        )
        return ((data.get("data") or {}).get("list")) or []

    async def fav_detail(self, media_id: int, limit: int = 500) -> tuple[str, list[Song]]:
        """拉收藏夹全部内容，连带收藏夹名字一起返回。

        导入歌单时要用名字直接建歌单，只返回曲目是不够的。
        """
        out: list[Song] = []
        title = ""
        page = 1
        while len(out) < limit:
            data = await self._json(
                f"{API}/x/v3/fav/resource/list",
                {"media_id": media_id, "pn": page, "ps": 20,
                 "keyword": "", "order": "mtime", "type": 0, "tid": 0,
                 "platform": "web"},
            )
            d = data.get("data") or {}
            if page == 1:
                title = ((d.get("info") or {}).get("title")) or ""
            medias = d.get("medias") or []
            if not medias:
                break
            for m in medias:
                bvid = m.get("bvid") or ""
                if not bvid:
                    continue
                out.append(
                    Song(
                        source="bilibili",
                        source_id=bvid,
                        title=m.get("title") or "",
                        artists=((m.get("upper") or {}).get("name")) or "",
                        album="bilibili",
                        cover_url=_cover(m.get("cover") or ""),
                        duration=int(m.get("duration") or 0) * 1000,
                    )
                )
            if not d.get("has_more"):
                break
            page += 1
        return title, out[:limit]

    async def fav_items(self, media_id: int, limit: int = 200) -> list[Song]:
        _title, songs = await self.fav_detail(media_id, limit)
        return songs

    # ── 发弹幕 ────────────────────────────────────────────────────────

    async def send_danmaku(self, room_id: int, text: str) -> dict[str, Any]:
        """往直播间发一条弹幕。

        必须已登录：SESSDATA 认身份，bili_jct 是 CSRF token，两个缺一个都会被拒。
        普通用户单条上限 20 字，超了直接截断而不是报错丢消息。
        """
        csrf = self._cookie.get("bili_jct", "")
        if not self._cookie.get("SESSDATA") or not csrf:
            raise BilibiliError("未登录 B 站，无法发送弹幕")

        msg = (text or "").strip()
        if not msg:
            raise BilibiliError("弹幕内容为空")
        if len(msg) > 20:
            msg = msg[:20]

        session = await self._get_session()
        form = {
            "bubble": "0",
            "msg": msg,
            "color": "16777215",
            "mode": "1",
            "fontsize": "25",
            "rnd": str(int(time.time())),
            "roomid": str(room_id),
            "csrf": csrf,
            "csrf_token": csrf,
        }
        async with session.post(
            f"{LIVE}/msg/send",
            data=form,
            headers={"Referer": f"https://live.bilibili.com/{room_id}"},
            cookies=self._merged_cookie(),
        ) as resp:
            data = await resp.json(content_type=None)

        code = data.get("code")
        if code not in (0, None):
            raise BilibiliError(
                f"发弹幕失败 {code}: {data.get('message') or data.get('msg')}"
            )
        return data

    # ── 扫码登录 ──────────────────────────────────────────────────────

    async def qr_create(self) -> dict[str, str]:
        data = await self._json(f"{PASSPORT}/x/passport-login/web/qrcode/generate")
        info = data.get("data") or {}
        key = info.get("qrcode_key") or ""
        if not key:
            raise BilibiliError("二维码生成失败")
        return {"key": key, "url": info.get("url") or ""}

    async def qr_check(self, key: str) -> dict[str, Any]:
        """code: 0 成功 / 86101 未扫 / 86090 已扫未确认 / 86038 过期。"""
        session = await self._get_session()
        async with session.get(
            f"{PASSPORT}/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": key, "source": "main-fe-header"},
            cookies=self._merged_cookie(),
            allow_redirects=False,
        ) as resp:
            payload = await resp.json(content_type=None)
            set_cookies = resp.headers.getall("Set-Cookie", [])

        inner = payload.get("data") or {}
        code = int(inner.get("code") or 0)
        result: dict[str, Any] = {"code": code, "message": inner.get("message", "")}

        if code == 0:
            jar: dict[str, str] = {}
            for line in set_cookies:
                for part in line.split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        if k.strip() in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5"):
                            jar[k.strip()] = v.strip()
            # 交叉页 url 里的参数更完整，优先用
            from urllib.parse import parse_qs, urlparse

            q = parse_qs(urlparse(inner.get("url") or "").query)
            for k in ("SESSDATA", "bili_jct", "DedeUserID"):
                if q.get(k):
                    jar[k] = q[k][0]
            if jar:
                self._cookie.update(jar)
                if jar.get("DedeUserID"):
                    self.uid = int(jar["DedeUserID"])
            result["cookie"] = self.cookie_string
        return result

    async def account_info(self) -> dict[str, Any]:
        data = await self._json(f"{API}/x/web-interface/nav", allow_codes=(-101,))
        d = data.get("data") or {}
        return {
            "logged_in": bool(d.get("isLogin")),
            "nickname": d.get("uname", ""),
            "uid": str(d.get("mid", "") or ""),
            "vip": 1 if (d.get("vipStatus") or 0) else 0,
            "avatar": _cover(d.get("face", "") or ""),
        }


def _strip_tags(text: str) -> str:
    """搜索结果标题里带 <em class="keyword"> 高亮标签，去掉。"""
    out, depth = [], 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)
