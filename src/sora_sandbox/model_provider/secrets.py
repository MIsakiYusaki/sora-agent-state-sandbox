from __future__ import annotations

import json
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator, Protocol, TypeVar

from .errors import ModelProviderError, ProviderErrorCode


T = TypeVar("T")


@dataclass(frozen=True, repr=False)
class SecretMaterial:
    api_key: str | None = field(default=None, repr=False)
    headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not self.api_key and not self.headers:
            raise ValueError("secret material cannot be empty")
        if self.api_key and (
            len(self.api_key) > 16_384 or "\r" in self.api_key or "\n" in self.api_key
        ):
            raise ValueError("provider API key has an invalid format")
        forbidden = {
            "host",
            "content-length",
            "transfer-encoding",
            "connection",
            "content-type",
            "accept",
            "user-agent",
            "forwarded",
            "x-forwarded-for",
            "x-forwarded-host",
            "x-forwarded-proto",
        }
        for name, value in self.headers:
            normalized = name.strip().lower()
            if (
                not normalized
                or len(normalized) > 256
                or not re.fullmatch(r"[a-z0-9!#$%&'*+.^_`|~-]+", normalized)
                or normalized in forbidden
            ):
                raise ValueError(f"secret header is not allowed: {name}")
            if len(value) > 16_384 or "\r" in value or "\n" in value:
                raise ValueError("secret header values cannot contain newlines")

    def to_json(self, profile_id: str) -> str:
        return json.dumps(
            {
                "schema_version": "ProviderSecret.v1",
                "profile_id": profile_id,
                "api_key": self.api_key,
                "headers": list(self.headers),
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str, *, expected_profile_id: str) -> "SecretMaterial":
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ModelProviderError(
                ProviderErrorCode.CREDENTIAL_UNAVAILABLE,
                "stored provider credential is malformed",
            ) from exc
        if payload.get("schema_version") != "ProviderSecret.v1":
            raise ModelProviderError(
                ProviderErrorCode.CREDENTIAL_UNAVAILABLE,
                "stored provider credential version is unsupported",
            )
        if not secrets.compare_digest(str(payload.get("profile_id") or ""), expected_profile_id):
            raise ModelProviderError(
                ProviderErrorCode.CREDENTIAL_UNAVAILABLE,
                "provider credential is bound to a different profile",
            )
        return cls(
            api_key=(str(payload["api_key"]) if payload.get("api_key") else None),
            headers=tuple((str(name), str(value)) for name, value in payload.get("headers") or []),
        )


class SecretStore(Protocol):
    def put(
        self,
        profile_id: str,
        material: SecretMaterial,
        *,
        secret_ref: str | None = None,
    ) -> str:
        ...

    def use(self, secret_ref: str, profile_id: str, operation: Callable[[SecretMaterial], T]) -> T:
        ...

    def open(self, secret_ref: str, profile_id: str) -> Iterator[SecretMaterial]:
        ...

    def exists(self, secret_ref: str, profile_id: str) -> bool:
        ...

    def delete(self, secret_ref: str, profile_id: str) -> None:
        ...


class OSKeyringSecretStore:
    """Fail-closed OS credential storage. It never falls back to a plaintext file."""

    service_name = "project-sora/model-provider"

    def __init__(self, *, backend=None) -> None:
        if backend is None:
            try:
                import keyring
            except ImportError as exc:  # pragma: no cover - dependency is mandatory in production.
                raise ModelProviderError(
                    ProviderErrorCode.CREDENTIAL_UNAVAILABLE,
                    "the OS credential backend is unavailable",
                ) from exc
            backend = keyring.get_keyring()
        self.backend = backend

    def put(
        self,
        profile_id: str,
        material: SecretMaterial,
        *,
        secret_ref: str | None = None,
    ) -> str:
        reference = secret_ref or f"cred_{secrets.token_urlsafe(24)}"
        try:
            self.backend.set_password(self.service_name, reference, material.to_json(profile_id))
        except Exception as exc:
            raise ModelProviderError(
                ProviderErrorCode.CREDENTIAL_UNAVAILABLE,
                "the OS credential backend rejected the provider credential",
            ) from exc
        return reference

    def use(self, secret_ref: str, profile_id: str, operation: Callable[[SecretMaterial], T]) -> T:
        with self.open(secret_ref, profile_id) as material:
            return operation(material)

    @contextmanager
    def open(self, secret_ref: str, profile_id: str) -> Iterator[SecretMaterial]:
        raw = self._get(secret_ref)
        material = SecretMaterial.from_json(raw, expected_profile_id=profile_id)
        try:
            yield material
        finally:
            del material
            del raw

    def exists(self, secret_ref: str, profile_id: str) -> bool:
        try:
            self.use(secret_ref, profile_id, lambda _material: True)
            return True
        except ModelProviderError:
            return False

    def delete(self, secret_ref: str, profile_id: str) -> None:
        self.use(secret_ref, profile_id, lambda _material: None)
        try:
            self.backend.delete_password(self.service_name, secret_ref)
        except Exception as exc:
            raise ModelProviderError(
                ProviderErrorCode.CREDENTIAL_UNAVAILABLE,
                "the OS credential backend could not delete the provider credential",
            ) from exc

    def _get(self, secret_ref: str) -> str:
        try:
            raw = self.backend.get_password(self.service_name, secret_ref)
        except Exception as exc:
            raise ModelProviderError(
                ProviderErrorCode.CREDENTIAL_UNAVAILABLE,
                "the OS credential backend could not read the provider credential",
            ) from exc
        if not raw:
            raise ModelProviderError(
                ProviderErrorCode.CREDENTIAL_UNAVAILABLE,
                "the provider credential is unavailable in this OS user context",
            )
        return raw


class InMemorySecretStore:
    """Injectable test store with the same profile binding checks as the OS store."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def put(
        self,
        profile_id: str,
        material: SecretMaterial,
        *,
        secret_ref: str | None = None,
    ) -> str:
        reference = secret_ref or f"cred_{secrets.token_urlsafe(18)}"
        self._values[reference] = material.to_json(profile_id)
        return reference

    def use(self, secret_ref: str, profile_id: str, operation: Callable[[SecretMaterial], T]) -> T:
        with self.open(secret_ref, profile_id) as material:
            return operation(material)

    @contextmanager
    def open(self, secret_ref: str, profile_id: str) -> Iterator[SecretMaterial]:
        raw = self._values.get(secret_ref)
        if raw is None:
            raise ModelProviderError(
                ProviderErrorCode.CREDENTIAL_UNAVAILABLE,
                "provider credential is unavailable",
            )
        material = SecretMaterial.from_json(raw, expected_profile_id=profile_id)
        try:
            yield material
        finally:
            del material

    def exists(self, secret_ref: str, profile_id: str) -> bool:
        try:
            return self.use(secret_ref, profile_id, lambda _material: True)
        except ModelProviderError:
            return False

    def delete(self, secret_ref: str, profile_id: str) -> None:
        self.use(secret_ref, profile_id, lambda _material: None)
        self._values.pop(secret_ref, None)
