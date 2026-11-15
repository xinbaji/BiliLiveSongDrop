"""配置模型与读写。

配置存在 %LOCALAPPDATA%\\BiliSinger\\config.json。读取时用默认值递归补齐，
所以新增字段不会导致旧配置文件报错。
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, get_type_hints

from app.paths import CONFIG_PATH

_TYPE_HINTS: dict[type, dict[str, Any]] = {}

# 权限等级：数值越小权限越高
PERMISSION_LEVELS = {
    "all": 0,       # 所有人
    "guard3": 3,    # 舰长
    "guard2": 2,    # 提督
    "guard1": 1,    # 总督
    "admin": -1,    # 房管
    "streamer": -2, # 主播本人
}

PERMISSION_LABELS = {
    "all": "所有人",
    "guard3": "舰长",
    "guard2": "提督",
    "guard1": "总督",
    "admin": "房管",
    "streamer": "仅主播",
}


@dataclass
class Permissions:
    """每条弹幕指令的最低权限。"""

    request_song: str = "all"
    remove_own: str = "all"
    skip: str = "streamer"
    pause: str = "streamer"
    clear_queue: str = "streamer"
    top_song: str = "streamer"


@dataclass
class OverlayConfig:
    """OBS 叠加层卡片的外观。"""

    theme: str = "glass"
    show_cover: bool = True
    show_requester: bool = True
    show_time: bool = True          # 当前时间 / 总时长（不再画进度条）
    show_next: bool = False
    # 卡片背景总开关：关掉后六套主题一律透明，只留文字（OBS 里叠画面最干净）
    card_background: bool = True
    # 基准宽度。实际按 scale 倍渲染，放大后再缩小比直接小尺寸更清晰。
    card_width: int = 420
    scale: float = 3.0
    corner_radius: int = 18
    blur: int = 18
    opacity: float = 0.62
    accent: str = ""


@dataclass
class DanmakuReplyConfig:
    """往直播间发弹幕的提示。全部默认关闭，只有 B 站登录后才有意义。"""

    enabled: bool = False
    # [{"keyword": "早安", "reply": "早安呀~"}]
    rules: list[dict[str, str]] = field(default_factory=list)
    announce_enqueue: bool = False
    announce_playing: bool = False
    enqueue_template: str = "{user} 点的《{title}》已加入点歌队列"
    playing_template: str = "正在播放：{title} - {artist}"
    min_interval: float = 4.0


@dataclass
class AppConfig:
    # —— 直播间 ——
    room_id: int = 0
    danmaku_enabled: bool = True

    # —— 弹幕指令 ——
    command_prefix: str = "点歌"
    command_aliases: list[str] = field(default_factory=lambda: ["求歌", "点唱"])
    strip_brackets: bool = True
    permissions: Permissions = field(default_factory=Permissions)

    # —— 播放 ——
    play_mode: str = "sequential"  # sequential | random | repeat_all | repeat_one
    volume: float = 0.8
    audio_quality: str = "exhigh"  # standard | higher | exhigh | lossless
    prefer_source: str = "netease"  # netease | bilibili
    fallback_to_bili: bool = True
    dedup_by_source_id: bool = True
    prefetch_enabled: bool = True

    # —— 空闲歌单 ——
    idle_playlist_id: int = 0
    idle_when_queue_empty: bool = True

    # —— 服务 ——
    port: int = 9527
    auto_port: bool = True

    # —— 缓存 ——
    cache_limit_mb: int = 2048

    # —— 登录 ——
    login_mode: dict[str, str] = field(
        default_factory=lambda: {"netease": "qr", "bilibili": "qr"}
    )

    # —— 弹幕提示 ——
    danmaku_reply: DanmakuReplyConfig = field(default_factory=DanmakuReplyConfig)

    # —— 界面 ——
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    ui_theme: str = "dark"

    # —— B 站登录后可读收藏夹 ——
    bili_uid: int = 0


def _build(cls: type, data: Any) -> Any:
    """按 dataclass 定义递归构造，忽略多余键、缺省键用默认值。

    注意：模块开了 `from __future__ import annotations`，f.type 拿到的是字符串
    而不是真正的类型对象，必须用 get_type_hints 解析，否则嵌套配置永远走不到
    递归分支，overlay 会一直是裸 dict。
    """
    if not is_dataclass(cls):
        return data
    if not isinstance(data, dict):
        return cls()

    hints = _TYPE_HINTS.get(cls)
    if hints is None:
        try:
            hints = get_type_hints(cls)
        except Exception:  # noqa: BLE001
            hints = {}
        _TYPE_HINTS[cls] = hints

    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        raw = data[f.name]
        typ = hints.get(f.name, f.type)
        if isinstance(typ, type) and is_dataclass(typ):
            kwargs[f.name] = _build(typ, raw)
        elif isinstance(raw, dict) and f.default_factory is not None:
            base = f.default_factory()  # type: ignore[misc]
            if isinstance(base, dict):
                merged = dict(base)
                merged.update({str(k): v for k, v in raw.items()})
                kwargs[f.name] = merged
            else:
                kwargs[f.name] = raw
        else:
            kwargs[f.name] = raw
    return cls(**kwargs)


def load_config() -> AppConfig:
    if not CONFIG_PATH.exists():
        cfg = AppConfig()
        save_config(cfg)
        return cfg
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 配置坏了不阻塞启动，退回默认值并备份坏文件
        try:
            CONFIG_PATH.rename(CONFIG_PATH.with_suffix(".broken.json"))
        except OSError:
            pass
        cfg = AppConfig()
        save_config(cfg)
        return cfg
    cfg = _build(AppConfig, raw)
    # 语义迁移：旧版的 scale 是 CSS transform 系数（默认 1），
    # 新版是「按几倍原生排版」，默认 3。低于 2 的旧值统一抬到 3，
    # 否则从旧配置升级上来会一直是 1 倍，OBS 里放大就糊。
    if float(getattr(cfg.overlay, "scale", 0) or 0) < 2:
        cfg.overlay.scale = 3.0
    return cfg


def save_config(cfg: AppConfig) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(dataclasses.asdict(cfg), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(CONFIG_PATH)


def config_as_dict(cfg: AppConfig) -> dict[str, Any]:
    return dataclasses.asdict(cfg)
