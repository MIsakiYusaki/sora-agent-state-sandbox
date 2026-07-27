from __future__ import annotations

import base64
import ctypes
import json
import os
import stat
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any, Mapping, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


JsonDict = dict[str, Any]


class WorldOpsSecretProtectionError(RuntimeError):
    pass


class PortableKeyProvider(Protocol):
    provider_name: str

    def get_or_create_key(self, key_id: str) -> bytes:
        ...


class FilePortableKeyProvider:
    """Development fallback; production should inject an OS keyring or KMS provider."""

    provider_name = "restricted_file_fallback"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def get_or_create_key(self, key_id: str) -> bytes:
        if self.path.exists():
            key = self.path.read_bytes()
            if len(key) != 32:
                raise WorldOpsSecretProtectionError("WorldOps portable vault key is invalid")
            return key
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = os.urandom(32)
        self.path.write_bytes(key)
        try:
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return key


class WorldOpsSecretVault:
    """Small platform-protected vault for contribution receipt capabilities."""

    _AAD = b"Project Sora WorldOps capability vault:v1"

    def __init__(self, path: str | Path, *, portable_key_provider: PortableKeyProvider | None = None) -> None:
        self.path = Path(path)
        self.key_path = self.path.with_suffix(".key")
        self.portable_key_provider = portable_key_provider or FilePortableKeyProvider(self.key_path)

    def put(self, proposal_id: str, *, receipt_token: str, withdrawal_token: str) -> None:
        values = self._load_all()
        values[proposal_id] = {
            "receipt_token": receipt_token,
            "withdrawal_token": withdrawal_token,
        }
        self._save_all(values)

    def get(self, proposal_id: str) -> JsonDict | None:
        value = self._load_all().get(proposal_id)
        return dict(value) if isinstance(value, dict) else None

    def delete(self, proposal_id: str) -> None:
        values = self._load_all()
        if proposal_id in values:
            values.pop(proposal_id, None)
            self._save_all(values)

    def reset(self) -> None:
        self.path.unlink(missing_ok=True)

    def _load_all(self) -> JsonDict:
        if not self.path.exists():
            return {}
        raw = self._unprotect(self.path.read_bytes())
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorldOpsSecretProtectionError("WorldOps capability vault is invalid") from exc
        if not isinstance(value, dict):
            raise WorldOpsSecretProtectionError("WorldOps capability vault must contain an object")
        return value

    def _save_all(self, values: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(dict(values), sort_keys=True, separators=(",", ":")).encode("utf-8")
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_bytes(self._protect(raw))
        temporary.replace(self.path)
        try:
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def _protect(self, raw: bytes) -> bytes:
        if sys.platform == "win32":
            return b"dpapi:v1:" + base64.b64encode(_dpapi_protect(raw))
        key = self._portable_key()
        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(nonce, raw, self._AAD)
        return b"aesgcm:v1:" + base64.b64encode(nonce + ciphertext)

    def _unprotect(self, payload: bytes) -> bytes:
        if payload.startswith(b"dpapi:v1:"):
            if sys.platform != "win32":
                raise WorldOpsSecretProtectionError("DPAPI vault is unavailable on this platform")
            return _dpapi_unprotect(base64.b64decode(payload.removeprefix(b"dpapi:v1:")))
        if payload.startswith(b"aesgcm:v1:"):
            packed = base64.b64decode(payload.removeprefix(b"aesgcm:v1:"))
            if len(packed) < 28:
                raise WorldOpsSecretProtectionError("WorldOps capability vault is truncated")
            try:
                return AESGCM(self._portable_key()).decrypt(packed[:12], packed[12:], self._AAD)
            except InvalidTag as exc:
                raise WorldOpsSecretProtectionError("WorldOps capability vault authentication failed") from exc
        raise WorldOpsSecretProtectionError("unknown WorldOps capability vault format")

    def _portable_key(self) -> bytes:
        key = self.portable_key_provider.get_or_create_key("worldops.capability-vault.v1")
        if len(key) != 32:
            raise WorldOpsSecretProtectionError("WorldOps portable key provider must return 32 bytes")
        return key


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _make_blob(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    return _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi_protect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, _buffer = _make_blob(data)
    out_blob = _DATA_BLOB()
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "Project Sora WorldOps capability vault",
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise WorldOpsSecretProtectionError("CryptProtectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, _buffer = _make_blob(data)
    out_blob = _DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise WorldOpsSecretProtectionError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
