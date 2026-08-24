from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Iterator

import httpx

from .contracts import (
    CancelRequest,
    GatewayError,
    GatewayErrorCode,
    GatewayEvent,
    HistoryPage,
    PROTOCOL_VERSION,
    TurnAccepted,
    TurnStatusView,
    TurnSubmit,
)


class GatewayClientError(RuntimeError):
    def __init__(self, error: GatewayError, *, status_code: int) -> None:
        super().__init__(f"{error.code}: {error.message}")
        self.error = error
        self.status_code = status_code


@dataclass(frozen=True)
class GatewayClientConfig:
    base_url: str
    capability_token: str
    client_instance_id: str
    origin: str | None = None
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float | None = None
    max_reconnect_attempts: int = 8
    reconnect_base_delay_seconds: float = 0.1


class ConversationGatewayClient:
    """Typed desktop client with SSE cursor recovery and event de-duplication."""

    def __init__(
        self,
        config: GatewayClientConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        timeout = httpx.Timeout(
            connect=config.connect_timeout_seconds,
            read=config.read_timeout_seconds,
            write=10.0,
            pool=5.0,
        )
        self._client = httpx.Client(
            base_url=config.base_url,
            headers=self._headers(),
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ConversationGatewayClient":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def submit(self, request: TurnSubmit) -> TurnAccepted:
        if request.client_instance_id != self.config.client_instance_id:
            raise ValueError("request client_instance_id differs from client config")
        response = self._client.post(
            "/v1/turns", json=request.model_dump(mode="json")
        )
        self._raise_for_error(response)
        return TurnAccepted.model_validate_json(response.text, strict=True)

    def status(self, turn_id: str) -> TurnStatusView:
        response = self._client.get(f"/v1/turns/{turn_id}")
        self._raise_for_error(response)
        return TurnStatusView.model_validate_json(response.text, strict=True)

    def cancel(self, turn_id: str, *, reason: str = "user_cancelled") -> TurnStatusView:
        payload = CancelRequest(reason=reason)
        response = self._client.post(
            f"/v1/turns/{turn_id}/cancel", json=payload.model_dump(mode="json")
        )
        self._raise_for_error(response)
        return TurnStatusView.model_validate_json(response.text, strict=True)

    def history(
        self,
        conversation_id: str,
        *,
        limit: int | None = None,
        before_sequence: int | None = None,
    ) -> HistoryPage:
        params = {
            key: value
            for key, value in {
                "limit": limit,
                "before_sequence": before_sequence,
            }.items()
            if value is not None
        }
        response = self._client.get(
            f"/v1/conversations/{conversation_id}/history", params=params
        )
        self._raise_for_error(response)
        return HistoryPage.model_validate_json(response.text, strict=True)

    def events(
        self,
        turn_id: str,
        *,
        after_sequence: int = 0,
    ) -> Iterator[GatewayEvent]:
        cursor = max(0, after_sequence)
        attempt = 0
        terminal = {"turn.committed", "turn.cancelled", "turn.failed"}
        while True:
            headers = {"Last-Event-ID": str(cursor)} if cursor else {}
            try:
                with self._client.stream(
                    "GET",
                    f"/v1/turns/{turn_id}/events",
                    params={"after_sequence": cursor},
                    headers=headers,
                ) as response:
                    self._raise_for_error(response)
                    attempt = 0
                    for payload in _iter_sse_data(response.iter_lines()):
                        event = GatewayEvent.model_validate_json(payload, strict=True)
                        if event.sequence <= cursor:
                            continue
                        cursor = event.sequence
                        yield event
                        if event.event_type in terminal:
                            return
                status = self.status(turn_id)
                if status.product_status != "active":
                    return
            except (httpx.TransportError, httpx.TimeoutException):
                attempt += 1
                if attempt > self.config.max_reconnect_attempts:
                    raise
                delay = min(
                    2.0,
                    self.config.reconnect_base_delay_seconds * (2 ** (attempt - 1)),
                )
                time.sleep(delay)

    def run_turn(self, request: TurnSubmit) -> tuple[TurnAccepted, str]:
        accepted = self.submit(request)
        chunks: list[str] = []
        for event in self.events(accepted.turn_id):
            if event.event_type == "response.delta":
                chunks.append(str(event.payload.get("delta") or ""))
        status = self.status(accepted.turn_id)
        if status.product_status != "committed":
            raise RuntimeError(f"turn ended with status {status.product_status}")
        return accepted, "".join(chunks)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.config.capability_token}",
            "X-Sora-Protocol-Version": PROTOCOL_VERSION,
            "X-Sora-Client-Instance": self.config.client_instance_id,
            "Accept": "application/json",
        }
        if self.config.origin:
            headers["Origin"] = self.config.origin
        return headers

    @staticmethod
    def _raise_for_error(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            error = GatewayError.model_validate_json(response.text)
        except Exception:
            error = GatewayError(
                code=GatewayErrorCode.INTERNAL_ERROR,
                message="gateway returned an invalid error response",
                retryable=response.status_code >= 500,
            )
        raise GatewayClientError(error, status_code=response.status_code)


def _iter_sse_data(lines: Iterator[str]) -> Iterator[str]:
    data_lines: list[str] = []
    for line in lines:
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield "\n".join(data_lines)
