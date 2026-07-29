from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class KeyProvider(Protocol):
    @property
    def key_id(self) -> str:
        ...

    def load_key(self, key_id: str | None = None) -> bytes:
        ...


class KeyUnavailableError(RuntimeError):
    pass


class KeyMismatchError(KeyUnavailableError):
    pass


@dataclass(frozen=True)
class StaticKeyProvider:
    key: bytes
    key_id: str = "static-test-key"

    def load_key(self, key_id: str | None = None) -> bytes:
        if key_id is not None and key_id != self.key_id:
            raise KeyMismatchError(
                f"ciphertext key_id {key_id!r} is not available; active key is {self.key_id!r}"
            )
        if len(self.key) != 32:
            raise ValueError("AES-256-GCM requires a 32-byte key")
        return self.key


class LocalProtectedKeyProvider:
    """Creates a per-installation key and protects it with Windows DPAPI when available."""

    def __init__(self, path: Path, *, env_var: str = "SORA_CONVERSATION_MASTER_KEY") -> None:
        self.path = path
        self.env_var = env_var

    @property
    def key_id(self) -> str:
        return "environment-v1" if os.environ.get(self.env_var) else "local-v1"

    def load_key(self, key_id: str | None = None) -> bytes:
        requested = key_id or self.key_id
        if requested == "environment-v1":
            configured = os.environ.get(self.env_var)
            if not configured:
                raise KeyUnavailableError(
                    f"ciphertext requires environment-v1 but {self.env_var} is not configured"
                )
            key = base64.urlsafe_b64decode(configured.encode("ascii"))
            if len(key) != 32:
                raise ValueError(f"{self.env_var} must be urlsafe-base64 encoded 32 bytes")
            return key
        if requested != "local-v1":
            raise KeyMismatchError(f"ciphertext key_id {requested!r} is not available")
        return self._load_local_key()

    def _load_local_key(self) -> bytes:
        if sys.platform != "win32":
            return self._load_keyring_key()
        if self.path.exists():
            packed = self.path.read_bytes()
            key = _dpapi_unprotect(packed)
            if len(key) != 32:
                raise ValueError("conversation runtime key has invalid length")
            return key
        key = AESGCM.generate_key(bit_length=256)
        packed = _dpapi_protect(key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(packed)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return key

    def _load_keyring_key(self) -> bytes:
        try:
            import keyring
        except ImportError as exc:
            raise KeyUnavailableError(
                "non-Windows key storage requires the 'keyring' package or "
                f"{self.env_var}"
            ) from exc
        account = "conversation-runtime-" + hashlib.sha256(
            str(self.path.resolve()).encode("utf-8")
        ).hexdigest()[:24]
        try:
            encoded = keyring.get_password("project-sora", account)
            if encoded:
                key = base64.urlsafe_b64decode(encoded.encode("ascii"))
                if len(key) != 32:
                    raise ValueError("OS keyring contains an invalid conversation runtime key")
                return key
            if self.path.exists():
                legacy = self.path.read_bytes()
                if len(legacy) != 32:
                    raise ValueError("legacy conversation runtime key has invalid length")
                key = legacy
            else:
                key = AESGCM.generate_key(bit_length=256)
            keyring.set_password(
                "project-sora", account, base64.urlsafe_b64encode(key).decode("ascii")
            )
            if self.path.exists():
                self.path.unlink()
            return key
        except Exception as exc:
            if isinstance(exc, (ValueError, KeyUnavailableError)):
                raise
            raise KeyUnavailableError(
                "OS keyring is unavailable; configure a desktop keyring or "
                f"set {self.env_var}"
            ) from exc


class AtRestCipher:
    VERSION = "aes256gcm-v1"

    def __init__(self, provider: KeyProvider) -> None:
        self.provider = provider

    def encrypt_text(self, value: str, *, associated_data: str) -> str:
        key = self.provider.load_key()
        key_id = self.provider.key_id
        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(
            nonce,
            value.encode("utf-8"),
            associated_data.encode("utf-8"),
        )
        packed = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return f"{self.VERSION}:{key_id}:{packed}"

    def decrypt_text(self, value: str, *, associated_data: str) -> str:
        try:
            version, key_id, packed = value.split(":", 2)
        except ValueError as exc:
            raise ValueError("ciphertext envelope is malformed") from exc
        if version != self.VERSION:
            raise ValueError(f"unsupported ciphertext version: {version}")
        key = self.provider.load_key(key_id)
        raw = base64.urlsafe_b64decode(packed.encode("ascii"))
        return AESGCM(key).decrypt(
            raw[:12], raw[12:], associated_data.encode("utf-8")
        ).decode("utf-8")

    def encrypt_json(self, value: Any, *, associated_data: str) -> str:
        return self.encrypt_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            associated_data=associated_data,
        )

    def decrypt_json(self, value: str, *, associated_data: str) -> Any:
        return json.loads(self.decrypt_text(value, associated_data=associated_data))


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi_protect(data: bytes) -> bytes:
    source, source_buffer = _blob(data)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptProtectData(
        ctypes.byref(source),
        "Project Sora Conversation Runtime",
        None,
        None,
        None,
        0x01,
        ctypes.byref(output),
    )
    del source_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    source, source_buffer = _blob(data)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0x01, ctypes.byref(output)
    )
    del source_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)
