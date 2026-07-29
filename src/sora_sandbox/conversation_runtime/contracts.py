from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Mapping


JsonDict = dict[str, Any]


class TurnKind(StrEnum):
    REACTIVE = "reactive"
    PROACTIVE = "proactive"
    PROACTIVE_RESERVED = "proactive_reserved"


class TurnStatus(StrEnum):
    ACCEPTED = "accepted"
    PREPARING = "preparing"
    GENERATING = "generating"
    STREAMING = "streaming"
    COMMITTING = "committing"
    COMMITTED = "committed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL_STATUSES = frozenset(
    {TurnStatus.COMMITTED, TurnStatus.CANCELLED, TurnStatus.FAILED}
)

ALLOWED_TRANSITIONS: Mapping[TurnStatus, frozenset[TurnStatus]] = {
    TurnStatus.ACCEPTED: frozenset(
        {TurnStatus.PREPARING, TurnStatus.CANCEL_REQUESTED, TurnStatus.CANCELLED, TurnStatus.FAILED}
    ),
    TurnStatus.PREPARING: frozenset(
        {TurnStatus.GENERATING, TurnStatus.CANCEL_REQUESTED, TurnStatus.CANCELLED, TurnStatus.FAILED}
    ),
    TurnStatus.GENERATING: frozenset(
        {TurnStatus.STREAMING, TurnStatus.COMMITTING, TurnStatus.CANCEL_REQUESTED, TurnStatus.CANCELLED, TurnStatus.FAILED}
    ),
    TurnStatus.STREAMING: frozenset(
        {TurnStatus.COMMITTING, TurnStatus.CANCEL_REQUESTED, TurnStatus.CANCELLED, TurnStatus.FAILED}
    ),
    TurnStatus.COMMITTING: frozenset({TurnStatus.COMMITTED, TurnStatus.FAILED}),
    TurnStatus.CANCEL_REQUESTED: frozenset(
        {TurnStatus.CANCELLED, TurnStatus.COMMITTING, TurnStatus.COMMITTED, TurnStatus.FAILED}
    ),
    TurnStatus.COMMITTED: frozenset(),
    TurnStatus.CANCELLED: frozenset(),
    TurnStatus.FAILED: frozenset(),
}


class UnifiedDisclosureLevel(StrEnum):
    NONE = "none"
    SILENT = "silent"
    IMPLICIT = "implicit"
    EXPLICIT_GIST = "explicit_gist"
    EXPLICIT_QUOTE = "explicit_quote"


DISCLOSURE_RANK: Mapping[UnifiedDisclosureLevel, int] = {
    UnifiedDisclosureLevel.NONE: 0,
    UnifiedDisclosureLevel.SILENT: 1,
    UnifiedDisclosureLevel.IMPLICIT: 2,
    UnifiedDisclosureLevel.EXPLICIT_GIST: 3,
    UnifiedDisclosureLevel.EXPLICIT_QUOTE: 4,
}


def most_restrictive_disclosure(
    *levels: UnifiedDisclosureLevel | str | None,
) -> UnifiedDisclosureLevel:
    normalized = [UnifiedDisclosureLevel(level) for level in levels if level is not None]
    return min(normalized, key=DISCLOSURE_RANK.__getitem__) if normalized else UnifiedDisclosureLevel.NONE


@dataclass(frozen=True)
class TurnExecutionContext:
    turn_id: str
    conversation_id: str
    request_id: str
    correlation_id: str
    branch_id: str
    turn_kind: TurnKind
    status: TurnStatus
    stage: str
    created_at_ms: int
    updated_at_ms: int
    deadline_at_ms: int | None = None
    visible_output_started: bool = False
    committed_at_ms: int | None = None
    revision: int = 1

    def to_dict(self) -> JsonDict:
        return {
            "schema_version": "TurnExecutionContext.v2",
            "turn_id": self.turn_id,
            "conversation_id": self.conversation_id,
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "branch_id": self.branch_id,
            "turn_kind": self.turn_kind.value,
            "status": self.status.value,
            "stage": self.stage,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "deadline_at_ms": self.deadline_at_ms,
            "visible_output_started": self.visible_output_started,
            "committed_at_ms": self.committed_at_ms,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class ClientEvent:
    turn_id: str
    sequence: int
    event_type: str
    payload: JsonDict
    created_at_ms: int

    def to_dict(self) -> JsonDict:
        return {
            "schema_version": "ConversationClientEvent.v1",
            "turn_id": self.turn_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "payload": self.payload,
            "created_at_ms": self.created_at_ms,
        }


class TurnCancelled(RuntimeError):
    def __init__(self, reason: str = "cancelled") -> None:
        super().__init__(reason)
        self.reason = reason


class CancellationToken:
    """Thread-safe cooperative cancellation with provider abort callbacks."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.RLock()
        self._reason: str | None = None
        self._callbacks: list[Callable[[str], None]] = []

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def cancel(self, reason: str = "cancelled") -> bool:
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason
            self._event.set()
            callbacks = tuple(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback(reason)
            except Exception:
                continue
        return True

    def add_callback(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)
                return
            reason = self._reason or "cancelled"
        callback(reason)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise TurnCancelled(self.reason or "cancelled")

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


@dataclass
class RuntimeTrace:
    turn_id: str
    correlation_id: str
    started_at: float = field(default_factory=time.perf_counter)
    events: list[JsonDict] = field(default_factory=list)

    def record(self, stage: str, status: str, **details: Any) -> None:
        self.events.append(
            {
                "sequence": len(self.events) + 1,
                "stage": stage,
                "status": status,
                "elapsed_ms": round((time.perf_counter() - self.started_at) * 1000, 2),
                "details": details,
            }
        )
