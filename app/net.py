"""端口占用处理。

启动时目标端口被占，分两种情况：
- 是自己的旧实例 → 直接杀掉并接管，否则两个进程会同时写一个 SQLite，队列会错乱；
- 是别的软件 → 自动往上找空闲端口，并把原因回传给界面提示用户。

识别「是不是自己」靠 /healthz 返回的 app 标识，不靠进程名猜。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

APP_MARK = "bili-singer"
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _netstat_lines() -> list[str]:
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    try:
        raw = subprocess.check_output(["netstat", "-ano"], creationflags=flags, timeout=10)
    except Exception:  # noqa: BLE001
        return []
    for enc in ("gbk", "utf-8", "latin-1"):
        try:
            return raw.decode(enc).splitlines()
        except UnicodeDecodeError:
            continue
    return []


def pids_on_port(port: int, host: str = "127.0.0.1") -> list[int]:
    pids: set[int] = set()
    needle = f":{port}"
    for line in _netstat_lines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if not parts[1].endswith(needle) or parts[3].upper() != "LISTENING":
            continue
        try:
            pid = int(parts[4])
        except ValueError:
            continue
        if pid and pid != os.getpid():
            pids.add(pid)
    return sorted(pids)


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def probe_identity(port: int, timeout: float = 1.2) -> str:
    """返回 'self' / 'other' / ''（空表示探测不到，当作别人）。"""
    try:
        with _NO_PROXY.open(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as resp:
            data = json.loads(resp.read())
        return "self" if isinstance(data, dict) and data.get("app") == APP_MARK else "other"
    except Exception:  # noqa: BLE001
        return ""


def _kill(pids: list[int]) -> list[int]:
    killed = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except Exception:  # noqa: BLE001
            continue
    return killed


def resolve_port(desired: int, host: str = "127.0.0.1", span: int = 40) -> tuple[int, str, str]:
    """返回 (实际端口, 状态, 说明)。状态：ok / replaced_self / moved / conflict。"""
    if is_port_free(desired, host):
        return desired, "ok", ""

    if probe_identity(desired) == "self":
        pids = pids_on_port(desired, host)
        killed = _kill(pids)
        for _ in range(24):
            time.sleep(0.25)
            if is_port_free(desired, host):
                break
        if is_port_free(desired, host):
            who = "、".join(str(p) for p in killed) or "旧进程"
            return desired, "replaced_self", f"已结束旧实例（PID {who}）并接管端口 {desired}"

    for port in range(desired + 1, desired + span):
        if is_port_free(port, host):
            return port, "moved", f"端口 {desired} 被其他程序占用，已改用 {port}"

    return desired, "conflict", f"端口 {desired} 及后续 {span} 个端口都被占用，请在设置里换一个"
