from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class TurnContext:
    """Stable input contract shared by all pluggable subsystems."""

    state: JsonDict
    event_bus: JsonDict
    session_context: JsonDict
    turn_frame: JsonDict
    context_use_policy: JsonDict
    frames: JsonDict = field(default_factory=dict)

    def frame(self, name: str) -> JsonDict:
        value = self.frames.get(name, {})
        return value if isinstance(value, dict) else {}

    def to_dict(self, *, include_state: bool = False) -> JsonDict:
        payload = {
            "event_bus": self.event_bus,
            "session_context": self.session_context,
            "turn_frame": self.turn_frame,
            "context_use_policy": self.context_use_policy,
            "frames": self.frames,
        }
        if include_state:
            payload["state"] = self.state
        return payload


@dataclass(frozen=True)
class SubsystemHealth:
    subsystem: str
    status: str
    latency_ms: float
    adapter: str
    signal_count: int = 0
    detail: str | None = None

    def to_dict(self) -> JsonDict:
        return {
            "subsystem": self.subsystem,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "adapter": self.adapter,
            "signal_count": self.signal_count,
            "detail": self.detail,
        }


def validate_signal(signal: JsonDict, subsystem: str) -> JsonDict:
    required = ("id", "source", "type", "priority", "confidence", "visibility")
    missing = [key for key in required if key not in signal]
    if missing:
        raise ValueError(f"{subsystem} returned an invalid signal; missing: {', '.join(missing)}")
    if signal["source"] != subsystem:
        raise ValueError(
            f"{subsystem} returned signal source={signal['source']!r}; adapters must own their source"
        )
    return signal


def state_candidate(
    *,
    candidate_id: str,
    source: str,
    candidate_type: str,
    payload: JsonDict,
    reason: str,
    confidence: float,
) -> JsonDict:
    """Canonical candidate-only state mutation contract for future adapters."""

    return {
        "candidate_id": candidate_id,
        "source": source,
        "type": candidate_type,
        "payload": payload,
        "reason": reason,
        "confidence": confidence,
        "status": "pending",
    }
