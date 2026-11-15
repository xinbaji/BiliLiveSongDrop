"""cookie 本地加密。

Windows 上用 DPAPI（CryptProtectData），密钥由系统按当前用户派生，
别的账户或拷走数据库文件都解不开。非 Windows 退化为明文，并打警告。
"""
from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes

_IS_WINDOWS = sys.platform == "win32"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _make_blob(data: bytes):
    # 必须保留 buffer 引用，否则 blob 指向的内存会被回收
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return buf, blob


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    buf, blob_in = _make_blob(data)
    blob_out = _DataBlob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN

    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, flags, ctypes.byref(blob_out)
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, flags, ctypes.byref(blob_out)
        )

    if not ok:
        raise OSError(f"DPAPI {'encrypt' if protect else 'decrypt'} failed")

    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def encrypt_text(plain: str) -> str:
    """明文 → 可入库的 base64 字符串。"""
    if not plain:
        return ""
    raw = plain.encode("utf-8")
    if not _IS_WINDOWS:
        return "plain:" + base64.b64encode(raw).decode("ascii")
    return "dpapi:" + base64.b64encode(_dpapi(raw, protect=True)).decode("ascii")


def decrypt_text(stored: str) -> str:
    if not stored:
        return ""
    if stored.startswith("plain:"):
        return base64.b64decode(stored[6:]).decode("utf-8")
    if stored.startswith("dpapi:"):
        blob = base64.b64decode(stored[6:])
        try:
            return _dpapi(blob, protect=False).decode("utf-8")
        except OSError:
            # 换了机器 / 换了用户，DPAPI 解不开，当作未登录
            return ""
    return ""
