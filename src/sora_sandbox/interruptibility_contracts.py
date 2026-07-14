from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Protocol


JsonDict = dict[str, Any]


RESPONSE_OBLIGATIONS = ("required", "optional", "forbidden")
EXPANSION_PERMISSIONS = ("blocked", "limited", "allowed", "invited")
TOPIC_SHIFT_PERMISSIONS = ("blocked", "same_topic", "adjacent", "open")
FOLLOW_UP_PERMISSIONS = ("none", "one", "multiple")
INITIATIVE_PERMISSIONS = ("blocked", "session_only", "allowed")
STATE_SURFACE_PERMISSIONS = ("silent", "implicit", "explicit")
RESPONSE_SHAPES = ("ack", "brief", "repair_brief", "normal", "deep", "safety")

INTERRUPTIBILITY_DIMENSIONS = (
    "expansion_permission",
    "topic_shift_permission",
    "follow_up_permission",
    "response_shape",
)


@dataclass(frozen=True, slots=True)
class PolicyEvidence:
    evidence_id: str
    kind: str
    direction: str
    confidence: float
    source: str = "runtime"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "direction": self.direction,
            "confidence": self.confidence,
            "source": self.source,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CanonicalControlEvent:
    event_id: str
    control: str
    value: Any
    scope: str = "turn"
    source: str = "natural_language"
    duration_seconds: int | None = None
    requires_confirmation: bool = False
    confidence: float = 1.0

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class InterruptibilityInput:
    turn_id: str
    correlation_id: str
    turn_kind: str
    user_input_present: bool
    timestamp_ms: int
    turn_interpretation: Mapping[str, Any]
    user_control_snapshot: Mapping[str, Any]
    session_interaction_state: Mapping[str, Any]
    safety_privacy_frame: Mapping[str, Any]
    domain_features: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StateSurfacePermission:
    memory: str = "implicit"
    relationship: str = "implicit"
    worldops: str = "silent"
    affect: str = "implicit"
    persona: str = "implicit"

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResponseBudgets:
    max_sentences: int
    question_budget: int
    topic_shift_budget: int
    state_surface_budget: int

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ModelAssistTrace:
    invoked: bool = False
    provider: str | None = None
    reason: str | None = None
    requested_dimensions: tuple[str, ...] = ()
    accepted_dimensions: tuple[str, ...] = ()
    rejected_dimensions: tuple[str, ...] = ()
    deadline_ms: int = 700
    min_confidence: float = 0.80
    latency_ms: float = 0.0
    fallback_used: bool = False
    fallback_reason: str | None = None

    def to_dict(self) -> JsonDict:
        payload = asdict(self)
        for key in ("requested_dimensions", "accepted_dimensions", "rejected_dimensions"):
            payload[key] = list(payload[key])
        return payload


@dataclass(frozen=True, slots=True)
class InterruptibilityFrame:
    turn_id: str
    correlation_id: str
    availability: str
    response_obligation: str
    expansion_permission: str
    topic_shift_permission: str
    follow_up_permission: str
    initiative_permission: str
    state_surface_permission: StateSurfacePermission
    response_shape: str
    decision_path: str
    model_assist: ModelAssistTrace
    budgets: ResponseBudgets
    hard_overrides: tuple[str, ...]
    evidence: tuple[PolicyEvidence, ...]
    reason_codes: tuple[str, ...]
    confidence: float
    policy_profile: str
    policy_version: str
    trace_id: str
    control_receipts: tuple[Mapping[str, Any], ...] = ()
    filtered_feature_ids: tuple[str, ...] = ()
    mode: str = "text_first"
    schema_version: str = "interruptibility_frame.v1"
    expires_after_turn: bool = True

    def to_dict(self) -> JsonDict:
        return {
            "schema_version": self.schema_version,
            "turn_id": self.turn_id,
            "correlation_id": self.correlation_id,
            "mode": self.mode,
            "availability": self.availability,
            "response_obligation": self.response_obligation,
            "expansion_permission": self.expansion_permission,
            "topic_shift_permission": self.topic_shift_permission,
            "follow_up_permission": self.follow_up_permission,
            "initiative_permission": self.initiative_permission,
            "state_surface_permission": self.state_surface_permission.to_dict(),
            "response_shape": self.response_shape,
            "decision_path": self.decision_path,
            "model_assist": self.model_assist.to_dict(),
            "budgets": self.budgets.to_dict(),
            "hard_overrides": list(self.hard_overrides),
            "evidence": [item.to_dict() for item in self.evidence],
            "reason_codes": list(self.reason_codes),
            "confidence": self.confidence,
            "policy_profile": self.policy_profile,
            "policy_version": self.policy_version,
            "trace_id": self.trace_id,
            "control_receipts": [dict(item) for item in self.control_receipts],
            "filtered_feature_ids": list(self.filtered_feature_ids),
            "expires_after_turn": self.expires_after_turn,
        }


@dataclass(frozen=True, slots=True)
class BoundedResolutionRequest:
    request_id: str
    turn_interpretation: Mapping[str, Any]
    unresolved_dimensions: tuple[str, ...]
    allowed_choices: Mapping[str, tuple[str, ...]]
    permission_ceiling: Mapping[str, str]
    conservative_defaults: Mapping[str, str]
    evidence: tuple[PolicyEvidence, ...]
    policy_version: str
    deadline_ms: int
    min_confidence: float


@dataclass(frozen=True, slots=True)
class BoundedResolution:
    request_id: str
    decisions: Mapping[str, str]
    evidence_refs: tuple[str, ...]
    reason_codes: tuple[str, ...]
    confidence: float
    provider: str | None = None
    schema_version: str = "bounded_resolution.v1"


class InterruptibilityResolutionProvider(Protocol):
    def resolve(self, request: BoundedResolutionRequest) -> BoundedResolution:
        ...
