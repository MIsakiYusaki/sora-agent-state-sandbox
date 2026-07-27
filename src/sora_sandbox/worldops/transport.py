from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from collections import defaultdict, deque
from typing import Any, Iterable, Mapping, Protocol

from ..memory_schema import JsonDict
from .contracts import WorldOpsContractError
from .server import WorldOpsServerAPI


class WorldOpsTransportError(RuntimeError):
    pass


class WorldOpsRequestAuthorizer(Protocol):
    def authorize(self, environ: Mapping[str, Any], route: str) -> bool:
        ...


class SlidingWindowRateLimiter:
    def __init__(self, *, requests: int, window_seconds: float = 60.0) -> None:
        if requests < 1 or window_seconds <= 0:
            raise ValueError("invalid WorldOps rate limit")
        self.requests = requests
        self.window_seconds = window_seconds
        self._entries: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            entries = self._entries[key]
            while entries and entries[0] <= now - self.window_seconds:
                entries.popleft()
            if len(entries) >= self.requests:
                return False
            entries.append(now)
            return True


class WorldOpsHttpDistributionClient:
    """HTTPS transport for an untrusted client talking to the canon service."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 5.0,
        max_response_bytes: int = 8 * 1024 * 1024,
        allow_insecure_localhost: bool = False,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        localhost = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and localhost and allow_insecure_localhost
        ):
            raise ValueError("WorldOps distribution requires HTTPS")
        if not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("invalid WorldOps distribution base URL")
        if timeout_seconds <= 0 or max_response_bytes < 1024:
            raise ValueError("invalid WorldOps transport limits")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = int(max_response_bytes)

    def get_latest_release(
        self,
        world_id: str,
        *,
        channel: str,
        cohort: str,
        etag: str | None = None,
    ) -> JsonDict:
        return self._request(
            "GET",
            f"/v1/worlds/{_segment(world_id)}/releases/latest",
            query={"channel": channel, "cohort": cohort},
            etag=etag,
        )

    def get_bundle(self, bundle_id: str, version: str) -> JsonDict:
        return self._request(
            "GET",
            f"/v1/bundles/{_segment(bundle_id)}/{_segment(version)}",
        )

    def post_contribution(self, proposal: Mapping[str, Any]) -> JsonDict:
        return self._request("POST", "/v1/contributions", payload=dict(proposal))

    def get_contribution(self, proposal_id: str, *, receipt_token: str) -> JsonDict:
        return self._request(
            "GET",
            f"/v1/contributions/{_segment(proposal_id)}",
            bearer=receipt_token,
        )

    def withdraw_contribution(self, proposal_id: str, *, withdrawal_token: str) -> JsonDict:
        return self._request(
            "DELETE",
            f"/v1/contributions/{_segment(proposal_id)}",
            bearer=withdrawal_token,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
        bearer: str | None = None,
        etag: str | None = None,
    ) -> JsonDict:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(dict(query))
        body = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "Project-Sora-WorldOps/1.0",
        }
        if payload is not None:
            body = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if etag:
            headers["If-None-Match"] = etag
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return self._decode_response(response.status, response.read(self.max_response_bytes + 1))
        except urllib.error.HTTPError as exc:
            response = self._decode_response(exc.code, exc.read(self.max_response_bytes + 1))
            if exc.code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
                raise PermissionError(str(response.get("error") or "WorldOps authorization failed")) from exc
            return response
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WorldOpsTransportError(f"WorldOps transport failed: {exc}") from exc

    def _decode_response(self, status: int, raw: bytes) -> JsonDict:
        if len(raw) > self.max_response_bytes:
            raise WorldOpsTransportError("WorldOps response exceeds the configured limit")
        if not raw:
            return {"status": int(status)}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorldOpsTransportError("WorldOps response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise WorldOpsTransportError("WorldOps response must be a JSON object")
        payload["status"] = int(status)
        return payload


class WorldOpsWSGIApplication:
    """Production-hostable WSGI adapter for the public WorldOps distribution API."""

    def __init__(
        self,
        api: WorldOpsServerAPI,
        *,
        max_request_bytes: int = 1024 * 1024,
        contribution_rate_limiter: SlidingWindowRateLimiter | None = None,
        request_authorizer: WorldOpsRequestAuthorizer | None = None,
    ) -> None:
        if max_request_bytes < 1024:
            raise ValueError("WorldOps request limit is too small")
        self.api = api
        self.max_request_bytes = int(max_request_bytes)
        self.contribution_rate_limiter = contribution_rate_limiter or SlidingWindowRateLimiter(requests=30)
        self.request_authorizer = request_authorizer

    def __call__(self, environ: Mapping[str, Any], start_response: Any) -> Iterable[bytes]:
        try:
            status, payload, extra_headers = self._dispatch(environ)
        except PermissionError:
            status, payload, extra_headers = 403, {"error": "forbidden"}, []
        except KeyError:
            status, payload, extra_headers = 404, {"error": "not_found"}, []
        except (ValueError, WorldOpsContractError) as exc:
            status, payload, extra_headers = 400, {"error": str(exc)[:500]}, []
        except Exception:
            status, payload, extra_headers = 500, {"error": "internal_error"}, []
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(raw))),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
        ]
        headers.extend(extra_headers)
        start_response(f"{status} {HTTPStatus(status).phrase}", headers)
        return [raw]

    def _dispatch(self, environ: Mapping[str, Any]) -> tuple[int, JsonDict, list[tuple[str, str]]]:
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        segments = [urllib.parse.unquote(item) for item in str(environ.get("PATH_INFO") or "").split("/") if item]
        query = urllib.parse.parse_qs(str(environ.get("QUERY_STRING") or ""), keep_blank_values=False)
        route = "/".join(segments)
        if self.request_authorizer is not None and not self.request_authorizer.authorize(environ, route):
            raise PermissionError("WorldOps request authorizer rejected the request")
        if method == "GET" and len(segments) == 5 and segments[:2] == ["v1", "worlds"] and segments[3:] == ["releases", "latest"]:
            result = self.api.get_latest_release(
                segments[2],
                channel=_single_query(query, "channel", "stable"),
                cohort=_single_query(query, "cohort", "default"),
                etag=(str(environ.get("HTTP_IF_NONE_MATCH")) if environ.get("HTTP_IF_NONE_MATCH") else None),
            )
            headers = [("ETag", str(result["etag"]))] if result.get("etag") else []
            headers.append(("Cache-Control", "public, max-age=60, stale-while-revalidate=300"))
            return int(result.get("status") or 200), result, headers
        if method == "GET" and len(segments) == 4 and segments[:2] == ["v1", "bundles"]:
            result = self.api.get_bundle(segments[2], segments[3])
            return int(result.get("status") or 200), result, [("Cache-Control", "public, immutable, max-age=31536000")]
        if method == "POST" and segments == ["v1", "contributions"]:
            remote = str(environ.get("REMOTE_ADDR") or "unknown")
            if not self.contribution_rate_limiter.allow(f"contribution:{remote}"):
                return 429, {"error": "rate_limited"}, [("Retry-After", "60"), ("Cache-Control", "no-store")]
            result = self.api.post_contribution(self._json_body(environ))
            return int(result.get("status") or 202), result, [("Cache-Control", "no-store")]
        if len(segments) == 3 and segments[:2] == ["v1", "contributions"]:
            token = _bearer_token(environ)
            if method == "GET":
                result = self.api.get_contribution(segments[2], receipt_token=token)
                return int(result.get("status") or 200), result, [("Cache-Control", "no-store")]
            if method == "DELETE":
                result = self.api.withdraw_contribution(segments[2], withdrawal_token=token)
                return int(result.get("status") or 200), result, [("Cache-Control", "no-store")]
        return 404, {"error": "not_found"}, []

    def _json_body(self, environ: Mapping[str, Any]) -> JsonDict:
        if not str(environ.get("CONTENT_TYPE") or "").lower().startswith("application/json"):
            raise WorldOpsContractError("application/json is required")
        raw_length = str(environ.get("CONTENT_LENGTH") or "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise WorldOpsContractError("invalid Content-Length") from exc
        if length < 0 or length > self.max_request_bytes:
            raise WorldOpsContractError("WorldOps request exceeds the configured limit")
        stream = environ.get("wsgi.input")
        if stream is None:
            raise WorldOpsContractError("request body is missing")
        raw = stream.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorldOpsContractError("request body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise WorldOpsContractError("request body must be a JSON object")
        return payload


def _segment(value: str) -> str:
    cleaned = str(value).strip()
    if not cleaned:
        raise ValueError("WorldOps URL identifier must not be empty")
    return urllib.parse.quote(cleaned, safe="")


def _single_query(query: Mapping[str, list[str]], key: str, default: str) -> str:
    values = query.get(key) or [default]
    return str(values[0])


def _bearer_token(environ: Mapping[str, Any]) -> str:
    header = str(environ.get("HTTP_AUTHORIZATION") or "")
    if not header.startswith("Bearer ") or len(header) <= len("Bearer "):
        raise PermissionError("missing contribution capability")
    return header.removeprefix("Bearer ").strip()
