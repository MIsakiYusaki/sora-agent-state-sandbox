from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit

from pydantic import Field

from .contracts import ProtocolModel, validate_identifier


def now_ms() -> int:
    return int(time.time() * 1000)


class CapabilityClaims(ProtocolModel):
    schema_version: str = "CapabilityClaims.v1"
    capability_id: str
    profile_id: str
    subject_id: str
    agent_id: str
    client_instance_id: str
    permissions: frozenset[str]
    allowed_origins: frozenset[str]
    allow_missing_origin: bool = False
    issued_at_ms: int = Field(ge=0)
    expires_at_ms: int = Field(ge=0)


@dataclass(frozen=True)
class VerifiedIdentity:
    capability_id: str
    profile_id: str
    subject_id: str
    agent_id: str
    pair_id: str
    client_instance_id: str
    permissions: frozenset[str]


class CapabilityError(PermissionError):
    pass


class LocalCapabilityProvider:
    """Stores only token digests and supports immediate revocation."""

    def __init__(self, *, token_bytes: int = 32) -> None:
        if token_bytes < 24:
            raise ValueError("capability tokens must contain at least 192 bits")
        self.token_bytes = token_bytes
        self._records: dict[str, CapabilityClaims] = {}
        self._lock = threading.RLock()

    def issue(
        self,
        *,
        profile_id: str,
        subject_id: str,
        agent_id: str,
        client_instance_id: str,
        permissions: Iterable[str],
        allowed_origins: Iterable[str],
        ttl_ms: int = 15 * 60 * 1000,
        allow_missing_origin: bool = False,
    ) -> tuple[str, CapabilityClaims]:
        if ttl_ms < 30_000 or ttl_ms > 24 * 60 * 60 * 1000:
            raise ValueError("capability ttl must be between 30 seconds and 24 hours")
        for identifier in (profile_id, subject_id, agent_id, client_instance_id):
            validate_identifier(identifier)
        origins = frozenset(_validate_origin(str(item)) for item in allowed_origins)
        issued = now_ms()
        raw = secrets.token_urlsafe(self.token_bytes)
        claims = CapabilityClaims(
            capability_id=f"cap_{secrets.token_hex(12)}",
            profile_id=profile_id,
            subject_id=subject_id,
            agent_id=agent_id,
            client_instance_id=client_instance_id,
            permissions=frozenset(str(item) for item in permissions),
            allowed_origins=origins,
            allow_missing_origin=allow_missing_origin,
            issued_at_ms=issued,
            expires_at_ms=issued + ttl_ms,
        )
        with self._lock:
            self._records[self._digest(raw)] = claims
            self._purge_expired_locked(issued)
        return raw, claims

    def verify(
        self,
        token: str,
        *,
        permission: str,
        origin: str | None,
        client_instance_id: str | None = None,
    ) -> VerifiedIdentity:
        with self._lock:
            claims = self._records.get(self._digest(token))
        if claims is None:
            raise CapabilityError("unknown or revoked capability")
        if claims.expires_at_ms <= now_ms():
            self.revoke(claims.capability_id)
            raise CapabilityError("expired capability")
        if permission not in claims.permissions:
            raise CapabilityError("capability does not grant this operation")
        if client_instance_id and client_instance_id != claims.client_instance_id:
            raise CapabilityError("client instance does not match capability")
        if origin is None:
            if not claims.allow_missing_origin:
                raise CapabilityError("origin is required")
        elif origin not in claims.allowed_origins:
            raise CapabilityError("origin is not allowed")
        pair_material = f"{claims.profile_id}\x1f{claims.subject_id}\x1f{claims.agent_id}"
        pair_id = "pair_" + hashlib.sha256(pair_material.encode("utf-8")).hexdigest()[:32]
        return VerifiedIdentity(
            capability_id=claims.capability_id,
            profile_id=claims.profile_id,
            subject_id=claims.subject_id,
            agent_id=claims.agent_id,
            pair_id=pair_id,
            client_instance_id=claims.client_instance_id,
            permissions=claims.permissions,
        )

    def revoke(self, capability_id: str) -> bool:
        with self._lock:
            for digest, claims in tuple(self._records.items()):
                if claims.capability_id == capability_id:
                    del self._records[digest]
                    return True
        return False

    def revoke_profile(self, profile_id: str) -> int:
        removed = 0
        with self._lock:
            for digest, claims in tuple(self._records.items()):
                if claims.profile_id == profile_id:
                    del self._records[digest]
                    removed += 1
        return removed

    def _purge_expired_locked(self, current_ms: int) -> None:
        for digest, claims in tuple(self._records.items()):
            if claims.expires_at_ms <= current_ms:
                del self._records[digest]

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_origin(value: str) -> str:
    if any(ord(character) < 32 for character in value):
        raise ValueError("origin contains control characters")
    parsed = urlsplit(value)
    if not parsed.scheme or not (parsed.netloc or parsed.path):
        raise ValueError("origin must be absolute")
    if parsed.query or parsed.fragment:
        raise ValueError("origin must not contain query or fragment components")
    return value


class SlidingWindowRateLimiter:
    def __init__(self, *, window_ms: int = 60_000) -> None:
        self.window_ms = max(1_000, window_ms)
        self._events: dict[tuple[str, str], deque[int]] = defaultdict(deque)
        self._lock = threading.RLock()

    def consume(self, key: str, operation: str, *, limit: int, at_ms: int | None = None) -> int:
        if limit < 1:
            raise ValueError("rate limit must be positive")
        current = at_ms or now_ms()
        cutoff = current - self.window_ms
        bucket_key = (key, operation)
        with self._lock:
            bucket = self._events[bucket_key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return max(1, bucket[0] + self.window_ms - current)
            bucket.append(current)
        return 0
