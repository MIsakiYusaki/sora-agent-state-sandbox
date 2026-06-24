from __future__ import annotations

import base64
import ctypes
import getpass
import json
import os
import stat
import sys
from ctypes import wintypes
from pathlib import Path

from .paths import user_config_dir


SERVICE_NAME = "deepseek"


class CredentialError(RuntimeError):
    pass


class CredentialManager:
    def __init__(self, config_dir: Path | None = None) -> None:
        self.config_dir = config_dir or user_config_dir()
        self.path = self.config_dir / "credentials.json"

    def get_api_key(self, prompt_if_missing: bool = True) -> str:
        env_key = os.environ.get("DEEPSEEK_API_KEY")
        if env_key:
            return env_key.strip()

        saved = self.load_api_key()
        if saved:
            return saved

        if not prompt_if_missing:
            raise CredentialError("DeepSeek API key is not configured.")

        api_key = getpass.getpass("DeepSeek API Key（输入后会保存到本机）: ").strip()
        if not api_key:
            raise CredentialError("DeepSeek API key cannot be empty.")
        self.save_api_key(api_key)
        return api_key

    def load_api_key(self) -> str | None:
        if not self.path.exists():
            return None
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        record = payload.get(SERVICE_NAME)
        if not record:
            return None
        encoding = record.get("encoding")
        data = record.get("data")
        if not data:
            return None
        raw = base64.b64decode(data.encode("ascii"))
        if encoding == "win32-dpapi":
            return _dpapi_unprotect(raw).decode("utf-8")
        if encoding == "plain-0600":
            return raw.decode("utf-8")
        raise CredentialError(f"Unsupported credential encoding: {encoding}")

    def save_api_key(self, api_key: str) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            encoding = "win32-dpapi"
            raw = _dpapi_protect(api_key.encode("utf-8"))
        else:
            encoding = "plain-0600"
            raw = api_key.encode("utf-8")

        payload = {}
        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload[SERVICE_NAME] = {
            "encoding": encoding,
            "data": base64.b64encode(raw).decode("ascii"),
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _make_blob(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array[ctypes.c_byte]]:
    buffer = ctypes.create_string_buffer(data)
    byte_buffer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
    blob = _DATA_BLOB(len(data), byte_buffer)
    return blob, buffer


def _dpapi_protect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, _buffer = _make_blob(data)
    out_blob = _DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "ProjectSora DeepSeek API Key",
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise CredentialError("CryptProtectData failed.")
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
        raise CredentialError("CryptUnprotectData failed.")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
