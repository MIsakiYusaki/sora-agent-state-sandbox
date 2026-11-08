from __future__ import annotations

from typing import Iterator, Protocol

from ..conversation_runtime.contracts import ClientEvent, JsonDict, TurnExecutionContext
from ..conversation_runtime.journal import JournalConflict
from ..conversation_runtime.runtime import ConversationRuntime


class RuntimeIdempotencyConflict(RuntimeError):
    """Portable runtime-port error for conflicting idempotency keys."""


class ConversationRuntimePort(Protocol):
    def stream_turn(
        self,
        state: JsonDict,
        user_input: str,
        *,
        request_id: str,
        conversation_id: str,
        branch_id: str,
        after_sequence: int = 0,
        correlation_id: str | None = None,
    ) -> Iterator[ClientEvent]: ...

    def reconnect(self, turn_id: str, *, after_sequence: int = 0) -> list[ClientEvent]: ...

    def cancel(self, conversation_id: str, *, reason: str, turn_id: str | None = None) -> bool: ...

    def turn_status(self, turn_id: str) -> TurnExecutionContext: ...

    def turn_by_request(
        self, conversation_id: str, request_id: str
    ) -> TurnExecutionContext | None: ...

    def history_page(
        self,
        conversation_id: str,
        *,
        limit: int,
        before_sequence: int | None,
    ) -> list[JsonDict]: ...

    def conversation_version(self, conversation_id: str) -> int: ...

    def diagnostic_turns(
        self, conversation_id: str, *, limit: int = 20
    ) -> list[JsonDict]: ...

    def diagnostic_frame(self, conversation_id: str, turn_id: str, frame_id: str) -> JsonDict: ...

    def set_frame_observation(self, enabled: bool) -> JsonDict: ...
    def frame_observation(self) -> JsonDict: ...
    def response_basis(self, conversation_id: str, turn_id: str) -> JsonDict | None: ...

    def health(self) -> JsonDict: ...

    def recover(self, *, stale_after_ms: int = 300_000) -> JsonDict: ...

    def maintain(self, *, at_ms: int | None = None) -> JsonDict: ...


class LocalConversationRuntimeAdapter:
    """Stable logical port for the in-process deployment topology."""

    def __init__(self, runtime: ConversationRuntime) -> None:
        self.runtime = runtime

    def stream_turn(
        self,
        state: JsonDict,
        user_input: str,
        *,
        request_id: str,
        conversation_id: str,
        branch_id: str,
        after_sequence: int = 0,
        correlation_id: str | None = None,
    ) -> Iterator[ClientEvent]:
        try:
            yield from self.runtime.stream_turn(
                state,
                user_input,
                request_id=request_id,
                conversation_id=conversation_id,
                branch_id=branch_id,
                after_sequence=after_sequence,
                correlation_id=correlation_id,
            )
        except JournalConflict as exc:
            raise RuntimeIdempotencyConflict(str(exc)) from exc

    def reconnect(self, turn_id: str, *, after_sequence: int = 0) -> list[ClientEvent]:
        return self.runtime.reconnect(turn_id, after_sequence=after_sequence)

    def cancel(self, conversation_id: str, *, reason: str, turn_id: str | None = None) -> bool:
        return self.runtime.cancel(conversation_id, reason=reason, turn_id=turn_id)

    def turn_status(self, turn_id: str) -> TurnExecutionContext:
        return self.runtime.turn_status(turn_id)

    def turn_by_request(
        self, conversation_id: str, request_id: str
    ) -> TurnExecutionContext | None:
        return self.runtime.journal.find_request(conversation_id, request_id)

    def history_page(
        self,
        conversation_id: str,
        *,
        limit: int,
        before_sequence: int | None,
    ) -> list[JsonDict]:
        return self.runtime.history_page(
            conversation_id, limit=limit, before_sequence=before_sequence
        )

    def conversation_version(self, conversation_id: str) -> int:
        return self.runtime.conversation_version(conversation_id)

    def history_summary(self, conversation_id: str) -> JsonDict:
        return self.runtime.journal.history_summary(conversation_id)

    def conversation_activity(self) -> dict[str, int]:
        return self.runtime.journal.conversation_activity()

    def diagnostic_turns(
        self, conversation_id: str, *, limit: int = 20
    ) -> list[JsonDict]:
        return self.runtime.diagnostic_turns(conversation_id, limit=limit)

    def diagnostic_frame(self, conversation_id: str, turn_id: str, frame_id: str) -> JsonDict:
        return self.runtime.diagnostic_frame(conversation_id, turn_id, frame_id)

    def set_frame_observation(self, enabled: bool) -> JsonDict:
        return self.runtime.set_frame_observation(enabled)

    def frame_observation(self) -> JsonDict:
        return self.runtime.frame_observation()

    def response_basis(self, conversation_id: str, turn_id: str) -> JsonDict | None:
        return self.runtime.response_basis(conversation_id, turn_id)

    def health(self) -> JsonDict:
        return self.runtime.health()

    def recover(self, *, stale_after_ms: int = 300_000) -> JsonDict:
        return self.runtime.recover(stale_after_ms=stale_after_ms)

    def maintain(self, *, at_ms: int | None = None) -> JsonDict:
        return self.runtime.maintain(at_ms=at_ms)
