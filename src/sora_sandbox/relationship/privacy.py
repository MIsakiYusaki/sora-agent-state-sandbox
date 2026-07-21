from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


JsonDict = dict[str, Any]


class RelationshipExportProtectionError(RuntimeError):
    pass


class RelationshipExportProtector:
    """Portable, passphrase-protected Relationship backup envelope."""

    _AAD = b"Project Sora Relationship export:v1"

    @staticmethod
    def protect(bundle: Mapping[str, Any], *, passphrase: str) -> JsonDict:
        if len(passphrase) < 12:
            raise ValueError("Relationship export passphrase must contain at least 12 characters")
        raw = json.dumps(dict(bundle), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        salt = os.urandom(16)
        nonce = os.urandom(12)
        key = RelationshipExportProtector._derive_key(passphrase, salt)
        ciphertext = AESGCM(key).encrypt(nonce, raw, RelationshipExportProtector._AAD)
        return {
            "schema_version": "RelationshipEncryptedExport.v1",
            "cipher": "AES-256-GCM",
            "kdf": {"name": "scrypt", "n": 32768, "r": 8, "p": 1, "salt": base64.b64encode(salt).decode("ascii")},
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            "plaintext_sha256": hashlib.sha256(raw).hexdigest(),
        }

    @staticmethod
    def unprotect(envelope: Mapping[str, Any], *, passphrase: str) -> JsonDict:
        if str(envelope.get("schema_version")) != "RelationshipEncryptedExport.v1":
            raise RelationshipExportProtectionError("unsupported Relationship export envelope")
        try:
            kdf = dict(envelope.get("kdf") or {})
            if (kdf.get("name"), int(kdf.get("n", 0)), int(kdf.get("r", 0)), int(kdf.get("p", 0))) != ("scrypt", 32768, 8, 1):
                raise RelationshipExportProtectionError("unsupported Relationship export KDF parameters")
            salt = base64.b64decode(str(kdf["salt"]), validate=True)
            nonce = base64.b64decode(str(envelope["nonce"]), validate=True)
            ciphertext = base64.b64decode(str(envelope["ciphertext"]), validate=True)
            if len(salt) != 16 or len(nonce) != 12:
                raise RelationshipExportProtectionError("invalid Relationship export cryptographic parameters")
            raw = AESGCM(RelationshipExportProtector._derive_key(passphrase, salt)).decrypt(
                nonce,
                ciphertext,
                RelationshipExportProtector._AAD,
            )
        except (InvalidTag, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, RelationshipExportProtectionError):
                raise
            raise RelationshipExportProtectionError("Relationship export authentication failed") from exc
        checksum = hashlib.sha256(raw).hexdigest()
        if checksum != str(envelope.get("plaintext_sha256") or ""):
            raise RelationshipExportProtectionError("Relationship export checksum mismatch")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RelationshipExportProtectionError("Relationship export payload is invalid") from exc
        if not isinstance(payload, dict):
            raise RelationshipExportProtectionError("Relationship export payload must be an object")
        return payload

    @staticmethod
    def _derive_key(passphrase: str, salt: bytes) -> bytes:
        return Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(passphrase.encode("utf-8"))


@dataclass(slots=True)
class _TransientValue:
    value: str
    expires_at: float


class TransientEvidenceBuffer:
    """Process-local raw evidence buffer with a hard TTL and destructive reads."""

    def __init__(self, *, ttl_seconds: int = 60, max_items: int = 1000) -> None:
        if not 1 <= ttl_seconds <= 60:
            raise ValueError("Relationship raw evidence TTL must be in [1, 60] seconds")
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self._values: dict[str, _TransientValue] = {}
        self._lock = threading.RLock()

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self._purge_locked()
            if len(self._values) >= self.max_items:
                oldest = min(self._values, key=lambda item: self._values[item].expires_at)
                self._values.pop(oldest, None)
            self._values[key] = _TransientValue(value=value, expires_at=time.monotonic() + self.ttl_seconds)

    def pop(self, key: str) -> str | None:
        with self._lock:
            item = self._values.pop(key, None)
            if item is None or item.expires_at <= time.monotonic():
                return None
            return item.value

    def get(self, key: str) -> str | None:
        with self._lock:
            item = self._values.get(key)
            if item is None:
                return None
            if item.expires_at <= time.monotonic():
                self._values.pop(key, None)
                return None
            return item.value

    def discard(self, key: str) -> None:
        with self._lock:
            self._values.pop(key, None)

    def purge(self) -> int:
        with self._lock:
            before = len(self._values)
            self._purge_locked()
            return before - len(self._values)

    @property
    def size(self) -> int:
        with self._lock:
            self._purge_locked()
            return len(self._values)

    def _purge_locked(self) -> None:
        now = time.monotonic()
        expired = [key for key, item in self._values.items() if item.expires_at <= now]
        for key in expired:
            self._values.pop(key, None)
