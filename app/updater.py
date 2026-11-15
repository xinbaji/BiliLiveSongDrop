"""在线更新：检查 GitHub latest release，下载 zip 并替换安装目录。

更新流程刻意做得朴素：
1. check 直接调 GitHub API，比较语义化版本号；
2. apply 下载资产 zip 到临时目录，生成一个批处理脚本，然后退出主进程；
3. 批处理等主进程退出后解压覆盖、重启 exe、自删。

不搞差量、不搞校验签名——单用户桌面工具，zip 是我们自己发的，
校验到位反而是负担。但下载必须落在临时目录，绝不边下边覆盖运行中的文件。
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

from app import GITHUB_REPO, __version__

# 允许下载的资产名。工作流打包时必须用这个名字。
ASSET_NAME = "BiliLiveSongDrop-windows-x64.zip"

# 语义化版本比较：1.0.0 / 1.2.3 / 2.0.0-rc1 都能比
_VER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _parse_version(text: str) -> tuple[int, int, int]:
    m = _VER_RE.search(text or "")
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def install_dir() -> Path:
    """安装根目录。打包后是 exe 所在目录；源码运行不支持自更新。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(sys.executable).resolve().parent


def can_self_update() -> bool:
    """源码运行时没有 exe 目录概念，替换文件是自找麻烦。"""
    return bool(getattr(sys, "frozen", False))


async def check_update() -> dict:
    """拉 latest release，和当前版本比较。网络错误不抛，返回错误说明。"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"{GITHUB_REPO} v{__version__}",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return {"ok": False, "error": f"GitHub 返回 HTTP {resp.status}"}
                data = await resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"网络请求失败：{exc}"}

    latest = str(data.get("tag_name") or "").lstrip("v")
    notes = str(data.get("body") or "")[:2000]
    html_url = str(data.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases")

    asset_url = ""
    for asset in data.get("assets") or []:
        if str(asset.get("name")) == ASSET_NAME:
            asset_url = str(asset.get("browser_download_url") or "")
            break

    current = _parse_version(__version__)
    target = _parse_version(latest)
    available = bool(latest) and target > current

    return {
        "ok": True,
        "current": __version__,
        "latest": latest or "",
        "update_available": available,
        "notes": notes,
        "release_url": html_url,
        "download_url": asset_url,
        "asset_found": bool(asset_url),
    }


async def download_update(download_url: str, dest: Path) -> None:
    """流式下载更新包。失败要删掉半截文件，否则下次还会捡到。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(
                download_url,
                headers={"User-Agent": f"{GITHUB_REPO} v{__version__}"},
                timeout=aiohttp.ClientTimeout(total=None, connect=15, sock_read=60),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"下载失败：HTTP {resp.status}")
                with open(tmp, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        fh.write(chunk)
        # 下载完成才改名，避免留下不完整的 zip 被误用
        tmp.replace(dest)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def spawn_updater(zip_path: Path) -> None:
    """写批处理，等本进程退出后覆盖安装、重启 exe。

    主进程必须先退出，Windows 才允许覆盖正在运行的 exe 目录。
    所以这里是「安排后事」：脚本写好，我们自己 os._exit，剩下交给 bat。
    """
    dest = install_dir()
    bat = dest / "update.bat"
    zip_posix = str(zip_path).replace("/", "\\")
    dest_posix = str(dest).replace("/", "\\")

    script = "\r\n".join([
        "@echo off",
        "rem 等主进程退出（主进程调用后马上 os._exit，3 秒足够）",
        "timeout /t 3 /nobreak >nul",
        f"powershell -NoProfile -ExecutionPolicy Bypass -Command "
        f"\"Expand-Archive -Force -LiteralPath '{zip_posix}' -DestinationPath '{dest_posix}'\"",
        "rem 解压完成后回到安装目录启动新版本",
        'start "" /D "%s" "BiliLiveSongDrop.exe"' % dest_posix,
        "del \"%~f0\"",
    ]) + "\r\n"

    bat.write_text(script, encoding="utf-8", errors="ascii")
    subprocess.Popen(
        ["cmd", "/c", str(bat)],
        cwd=str(dest),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    )


async def apply_update(download_url: str) -> dict:
    if not can_self_update():
        return {"ok": False, "error": "源码运行不支持在线更新，请用打包版"}

    if not download_url:
        return {"ok": False, "error": "release 里没有找到更新包"}

    tmp_dir = Path(tempfile.gettempdir()) / "BiliLiveSongDrop-update"
    stamp = str(int(time.time()))
    dest = tmp_dir / f"update-{stamp}.zip"

    await download_update(download_url, dest)

    # 写好脚本再退出。os._exit 跳过清理，因为 aiohttp 后台线程会阻塞正常退出。
    spawn_updater(dest)
    os._exit(0)
