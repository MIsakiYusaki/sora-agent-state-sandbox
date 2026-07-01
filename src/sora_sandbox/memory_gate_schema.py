from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .memory_schema import JsonDict


GATE_DECISION_SCHEMA_VERSION = "gate_decision.v1"
MEMORY_GATE_POLICY_VERSION = "memory_gate_policy.v1"


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class GateDecisionAction(StrEnum):
    AUTO_COMMIT = "AUTO_COMMIT"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REJECT = "REJECT"
    QUARANTINE = "QUARANTINE"
    DEFER = "DEFER"


@dataclass(frozen=True)
class GateDecision:
    candidate_id: str
    decision: GateDecisionAction
    operation: str
    target_memory_id: str | None
    policy_version: str = MEMORY_GATE_POLICY_VERSION
    gate_score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    risk: JsonDict = field(default_factory=dict)
    scores: JsonDict = field(default_factory=dict)
    trace: JsonDict = field(default_factory=dict)
    review: JsonDict | None = None
    quarantine: JsonDict | None = None
    deferred_until: str | None = None


def gate_decision_to_dict(decision: GateDecision) -> JsonDict:
    return {
        "schema_version": GATE_DECISION_SCHEMA_VERSION,
        "candidate_id": decision.candidate_id,
        "decision": decision.decision.value,
        "operation": decision.operation,
        "target_memory_id": decision.target_memory_id,
        "policy_version": decision.policy_version,
        "gate_score": decision.gate_score,
        "reasons": list(decision.reasons),
        "risk": dict(decision.risk),
        "scores": dict(decision.scores),
        "trace": dict(decision.trace),
        "review": dict(decision.review) if decision.review is not None else None,
        "quarantine": dict(decision.quarantine) if decision.quarantine is not None else None,
        "deferred_until": decision.deferred_until,
    }
