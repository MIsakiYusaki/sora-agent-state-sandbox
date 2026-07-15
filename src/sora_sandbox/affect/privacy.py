from __future__ import annotations

import base64
import ctypes
import os
import sys
from ctypes import wintypes

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class AffectPayloadProtectionError(RuntimeError):
    pass


class AffectPayloadProtector:
    """Protect transient semantic text at rest without putting keys in the database."""

    _VOLATILE_AAD = b"Project Sora Affect transient payload:v2"

    def __init__(self) -> None:
        self._volatile_key = os.urandom(32)
        self.method = "win32-dpapi" if sys.platform == "win32" else "process-volatile-aesgcm"

    def protect(self, text: str) -> str:
        raw = text.encode("utf-8")
        if sys.platform == "win32":
            protected = _dpapi_protect(raw)
            return "dpapi:v1:" + base64.b64encode(protected).decode("ascii")
        nonce = os.urandom(12)
        encrypted = AESGCM(self._volatile_key).encrypt(nonce, raw, self._VOLATILE_AAD)
        return "volatile:v2:" + base64.b64encode(nonce + encrypted).decode("ascii")

    def unprotect(self, payload: str) -> str:
        if payload.startswith("dpapi:v1:"):
            if sys.platform != "win32":
                raise AffectPayloadProtectionError("DPAPI payload cannot be opened on this platform")
            protected = base64.b64decode(payload.removeprefix("dpapi:v1:").encode("ascii"))
            return _dpapi_unprotect(protected).decode("utf-8")
        if payload.startswith("volatile:v2:"):
            packed = base64.b64decode(payload.removeprefix("volatile:v2:").encode("ascii"))
            if len(packed) < 28:
                raise AffectPayloadProtectionError("volatile payload is truncated")
            nonce, encrypted = packed[:12], packed[12:]
            try:
                raw = AESGCM(self._volatile_key).decrypt(nonce, encrypted, self._VOLATILE_AAD)
            except InvalidTag as exc:
                raise AffectPayloadProtectionError("volatile payload authentication failed") from exc
            return raw.decode("utf-8")
        raise AffectPayloadProtectionError("unknown Affect payload protection format")


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _make_blob(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    blob = _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def _dpapi_protect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, _buffer = _make_blob(data)
    out_blob = _DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "Project Sora Affect transient payload",
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise AffectPayloadProtectionError("CryptProtectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)

def _dpapi_unprotect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, _buffer = _make_blob(data)
    out_blob = _DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise AffectPayloadProtectionError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
