from __future__ import annotations

import codecs
import json
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

import httpx

from ..turn_observability import observe_http

from .contracts import ProviderProfile
from .endpoint_policy import EndpointPolicy, EndpointPolicyError
from .errors import ModelProviderError, ProviderErrorCode
from .secrets import SecretMaterial, SecretStore


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class BrokerResponse:
    payload: JsonDict
    status_code: int
    provider_request_id: str | None


class CredentialedHTTPBroker:
    """Single credentialed network exit for provider adapters.

    The Python implementation is the host-neutral contract implementation. A
    desktop Rust host can replace it without changing callers.
    """

    def __init__(
        self,
        secret_store: SecretStore,
        *,
        endpoint_policy: EndpointPolicy | None = None,
        client: httpx.Client | None = None,
        max_request_bytes: int = 2 * 1024 * 1024,
        max_response_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.secret_store = secret_store
        self.endpoint_policy = endpoint_policy or EndpointPolicy()
        self.client = client or httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(60.0, connect=15.0),
            headers={"User-Agent": "ProjectSora-ModelProvider/1"},
        )
        self._owns_client = client is None
        self.max_request_bytes = max(1024, max_request_bytes)
        self.max_response_bytes = max(1024, max_response_bytes)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def post_json(
        self,
        profile: ProviderProfile,
        path: str,
        payload: JsonDict,
        *,
        timeout_seconds: float,
        cancellation_token: Any | None = None,
    ) -> BrokerResponse:
        self._check_cancellation(cancellation_token)
        url = self._request_url(profile, path)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.max_request_bytes:
            raise ModelProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "provider request exceeds the configured size limit",
            )
        with self._secret_context(profile) as material:
            headers = self._headers(profile, material, accept="application/json")
            try:
                request_stage = ("provider.http.embeddings" if path == "/embeddings" else
                                 "provider.http.rerank" if path == "/rerank" else "provider.http.json")
                with observe_http(request_stage), self.client.stream(
                    "POST",
                    url,
                    content=encoded,
                    headers=headers,
                    timeout=timeout_seconds,
                ) as response:
                    request_id = self._provider_request_id(response.headers)
                    self._raise_for_status(response.status_code, request_id=request_id)
                    status_code = response.status_code
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        self._check_cancellation(cancellation_token)
                        content.extend(chunk)
                        if len(content) > self.max_response_bytes:
                            raise ModelProviderError(
                                ProviderErrorCode.INVALID_RESPONSE,
                                "provider response exceeds the configured size limit",
                                provider_request_id=request_id,
                            )
            except httpx.TimeoutException as exc:
                raise ModelProviderError(
                    ProviderErrorCode.DEADLINE_EXCEEDED,
                    "provider request timed out",
                    retryable=True,
                ) from exc
            except httpx.TransportError as exc:
                code = ProviderErrorCode.TLS_ERROR if "ssl" in str(exc).lower() else ProviderErrorCode.PROVIDER_UNAVAILABLE
                raise ModelProviderError(code, "provider connection failed", retryable=True) from exc
        self._check_cancellation(cancellation_token)
        try:
            body = json.loads(bytes(content).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ModelProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "provider returned a non-JSON response",
                provider_request_id=request_id,
            ) from exc
        if not isinstance(body, dict):
            raise ModelProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "provider returned an unexpected JSON shape",
                provider_request_id=request_id,
            )
        return BrokerResponse(body, status_code, request_id)

    def stream_sse(
        self,
        profile: ProviderProfile,
        path: str,
        payload: JsonDict,
        *,
        timeout_seconds: float,
        cancellation_token: Any | None = None,
    ) -> Iterator[JsonDict]:
        self._check_cancellation(cancellation_token)
        url = self._request_url(profile, path)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.max_request_bytes:
            raise ModelProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "provider request exceeds the configured size limit",
            )
        with self._secret_context(profile) as material:
            headers = self._headers(profile, material, accept="text/event-stream")
            try:
                with observe_http("provider.http.sse"), self.client.stream(
                    "POST",
                    url,
                    content=encoded,
                    headers=headers,
                    timeout=timeout_seconds,
                ) as response:
                    request_id = self._provider_request_id(response.headers)
                    self._raise_for_status(response.status_code, request_id=request_id)
                    for line in self._bounded_utf8_lines(
                        response.iter_bytes(), request_id=request_id
                    ):
                        self._check_cancellation(cancellation_token)
                        stripped = line.strip()
                        if not stripped or not stripped.startswith("data:"):
                            continue
                        data = stripped[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            item = json.loads(data)
                        except json.JSONDecodeError as exc:
                            raise ModelProviderError(
                                ProviderErrorCode.INVALID_RESPONSE,
                                "provider stream contained invalid JSON",
                                provider_request_id=request_id,
                            ) from exc
                        if not isinstance(item, dict):
                            raise ModelProviderError(
                                ProviderErrorCode.INVALID_RESPONSE,
                                "provider stream contained an unexpected event",
                                provider_request_id=request_id,
                            )
                        item["_provider_request_id"] = request_id
                        yield item
            except ModelProviderError:
                raise
            except httpx.TimeoutException as exc:
                raise ModelProviderError(
                    ProviderErrorCode.DEADLINE_EXCEEDED,
                    "provider stream timed out",
                    retryable=True,
                ) from exc
            except httpx.TransportError as exc:
                raise ModelProviderError(
                    ProviderErrorCode.PROVIDER_UNAVAILABLE,
                    "provider stream connection failed",
                    retryable=True,
                ) from exc

    def _bounded_utf8_lines(
        self,
        chunks: Iterator[bytes],
        *,
        request_id: str | None,
    ) -> Iterator[str]:
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        buffer = ""
        total = 0
        try:
            for chunk in chunks:
                total += len(chunk)
                if total > self.max_response_bytes:
                    raise ModelProviderError(
                        ProviderErrorCode.INVALID_RESPONSE,
                        "provider stream exceeds the configured size limit",
                        provider_request_id=request_id,
                    )
                buffer += decoder.decode(chunk)
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    yield line
            buffer += decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise ModelProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "provider stream is not valid UTF-8",
                provider_request_id=request_id,
            ) from exc
        if buffer:
            yield buffer

    def _request_url(self, profile: ProviderProfile, path: str) -> str:
        if not path.startswith("/") or "://" in path or ".." in path:
            raise ValueError("provider adapter supplied an unsafe request path")
        try:
            validated = self.endpoint_policy.validate(profile.endpoint, resolve_dns=True)
        except EndpointPolicyError as exc:
            raise ModelProviderError(
                ProviderErrorCode.ENDPOINT_BLOCKED,
                str(exc),
            ) from exc
        base = validated.base_url
        if base.endswith(path):
            return base
        return base + path

    def _secret_context(self, profile: ProviderProfile):
        if profile.auth_scheme == "none":
            return nullcontext(None)
        if not profile.credential_ref:
            raise ModelProviderError(
                ProviderErrorCode.CREDENTIAL_UNAVAILABLE,
                "provider profile has no credential binding",
            )
        return self.secret_store.open(profile.credential_ref, profile.profile_id)

    @staticmethod
    def _headers(
        profile: ProviderProfile,
        material: SecretMaterial | None,
        *,
        accept: str,
    ) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": accept}
        if profile.auth_scheme == "none":
            return headers
        if material is None:
            raise ModelProviderError(
                ProviderErrorCode.CREDENTIAL_UNAVAILABLE,
                "provider credential material is unavailable",
            )
        if profile.auth_scheme == "bearer":
            if not material.api_key:
                raise ModelProviderError(
                    ProviderErrorCode.CREDENTIAL_UNAVAILABLE,
                    "provider API key is unavailable",
                )
            headers["Authorization"] = f"Bearer {material.api_key}"
        elif profile.auth_scheme == "anthropic_api_key":
            if not material.api_key:
                raise ModelProviderError(
                    ProviderErrorCode.CREDENTIAL_UNAVAILABLE,
                    "provider API key is unavailable",
                )
            headers["x-api-key"] = material.api_key
            headers["anthropic-version"] = "2023-06-01"
        elif profile.auth_scheme == "google_api_key":
            if not material.api_key:
                raise ModelProviderError(
                    ProviderErrorCode.CREDENTIAL_UNAVAILABLE,
                    "provider API key is unavailable",
                )
            headers["x-goog-api-key"] = material.api_key
        elif profile.auth_scheme == "custom_headers":
            headers.update(dict(material.headers))
        else:
            raise ModelProviderError(
                ProviderErrorCode.UNSUPPORTED_PROTOCOL,
                f"unsupported provider authentication scheme: {profile.auth_scheme}",
            )
        return headers

    @staticmethod
    def _provider_request_id(headers: Mapping[str, str]) -> str | None:
        for name in ("x-request-id", "request-id", "x-amzn-requestid", "cf-ray"):
            value = headers.get(name)
            if value:
                return str(value)[:256]
        return None

    @staticmethod
    def _raise_for_status(status_code: int, *, request_id: str | None) -> None:
        if 200 <= status_code < 300:
            return
        if 300 <= status_code < 400:
            raise ModelProviderError(
                ProviderErrorCode.ENDPOINT_BLOCKED,
                "provider redirects are disabled",
                provider_request_id=request_id,
            )
        if status_code in {401, 403}:
            code = ProviderErrorCode.INVALID_CREDENTIAL
            retryable = False
        elif status_code == 402:
            code = ProviderErrorCode.QUOTA_EXHAUSTED
            retryable = False
        elif status_code == 429:
            code = ProviderErrorCode.RATE_LIMITED
            retryable = True
        elif status_code >= 500:
            code = ProviderErrorCode.PROVIDER_UNAVAILABLE
            retryable = True
        else:
            code = ProviderErrorCode.INVALID_RESPONSE
            retryable = False
        raise ModelProviderError(
            code,
            f"provider request failed with HTTP {status_code}",
            retryable=retryable,
            provider_request_id=request_id,
        )

    @staticmethod
    def _check_cancellation(token: Any | None) -> None:
        if token is not None:
            raiser = getattr(token, "raise_if_cancelled", None)
            if callable(raiser):
                raiser()
