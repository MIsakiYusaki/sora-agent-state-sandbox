from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Mapping


JsonDict = dict[str, Any]

RELATIONSHIP_SCHEMA_VERSION = 1
RELATIONSHIP_EVENT_CONTRACT = "RelationshipInputEvent.v1"
RELATIONSHIP_CANDIDATE_CONTRACT = "RelationshipEvidenceCandidate.v1"
RELATIONSHIP_STATE_SNAPSHOT_CONTRACT = "RelationshipStateSnapshot.v1"
RELATIONSHIP_ESTIMATE_SNAPSHOT_CONTRACT = "RelationshipEstimateSnapshot.v1"
RELATIONSHIP_FRAME_CONTRACT = "RelationshipFrame.v1"
RELATIONSHIP_SIGNAL_CONTRACT = "RelationshipSignal.v1"
RELATIONSHIP_DOMAIN_EVENT_CONTRACT = "RelationshipDomainEvent.v1"
RELATIONSHIP_RECALL_HINTS_CONTRACT = "RelationshipRecallHints.v1"
RELATIONSHIP_SURFACE_HINTS_CONTRACT = "RelationshipSurfaceHints.v1"
RELATIONSHIP_AGREEMENT_CONTRACT = "RelationshipAgreement.v1"
RELATIONSHIP_AMENDMENT_CONTRACT = "AgreementAmendmentCandidate.v1"
RELATIONSHIP_REPAIR_CONTRACT = "RepairEpisode.v1"
RELATIONSHIP_STATUS_EVENT_CONTRACT = "RelationshipContributionStatusEvent.v1"
RELATIONSHIP_PROJECTION_ADAPTER_VERSION = "relationship_projection_adapter.v1"
RELATIONSHIP_POLICY_VERSION = "relationship_policy.v2"
RELATIONSHIP_ALGORITHM_VERSION = "relationship_aggregate.v1"
RELATIONSHIP_ESTIMATE_ALGORITHM_VERSION = "relationship_estimate.v2"
RELATIONSHIP_STAGE_POLICY_VERSION = "relationship_stage_policy.v1"
RELATIONSHIP_FRAME_POLICY_VERSION = "relationship_frame_policy.v2"
RELATIONSHIP_VALUE_DECIMALS = 8

DIRECTION_SCOPES = {"sora_to_user", "user_to_sora_estimate"}
DIRECTIONS = {-1, 1}
SENSITIVITY_LEVELS = {"low", "medium", "high", "restricted"}
CANDIDATE_STATUSES = {
    "queued",
    "processing",
    "auto_commit",
    "pending_confirmation",
    "pending_review",
    "deferred",
    "quarantined",
    "rejected",
    "committed",
    "forgotten",
}

BASE_DIMENSIONS = {
    "trust",
    "familiarity",
    "psychological_safety",
    "respect",
    "shared_history_density",
    "comfort_rhythm",
    "reciprocity_style",
    "boundary_preference",
}
BOND_DIMENSIONS = {
    "affection",
    "romantic_love",
    "attachment",
    "loyalty",
    "commitment",
    "dependency",
    "jealousy",
    "possessiveness",
    "autonomy_balance",
    "emotional_intimacy",
    "predictability",
    "repair_readiness",
}
RELATIONSHIP_DIMENSIONS = BASE_DIMENSIONS | BOND_DIMENSIONS
USER_ESTIMATE_DIMENSIONS = {
    "comfort",
    "affection",
    "attachment",
    "romantic_interest",
    "commitment",
    "jealousy",
    "trust",
    "autonomy_balance",
}
ALL_DIMENSIONS = RELATIONSHIP_DIMENSIONS | USER_ESTIMATE_DIMENSIONS

EVIDENCE_TYPES = {
    "explicit_declaration",
    "explicit_correction",
    "promise_fulfilled",
    "promise_broken",
    "boundary_respected",
    "boundary_violated",
    "shared_episode_completed",
    "vulnerable_disclosure_received_well",
    "repair_accepted",
    "repair_failed",
    "affectionate_expression",
    "cross_episode_interaction_pattern",
    "worldops_shared_outcome",
    "affect_sustained_outcome",
}

REPAIR_STATES = {
    "stable",
    "rupture_detected",
    "acknowledged",
    "cooling_off",
    "repair_in_progress",
    "repaired",
    "accepted_difference",
    "unresolved",
}
REPAIR_TRANSITIONS: dict[str, set[str]] = {
    "stable": {"rupture_detected"},
    "rupture_detected": {"acknowledged", "cooling_off"},
    "acknowledged": {"repair_in_progress"},
    "cooling_off": {"repair_in_progress"},
    "repair_in_progress": {"repaired", "accepted_difference", "unresolved"},
    "unresolved": {"repair_in_progress"},
    "repaired": {"stable", "rupture_detected"},
    "accepted_difference": {"stable", "rupture_detected"},
}

AGREEMENT_MODES = {
    "organic",
    "companion",
    "friend",
    "romantic_partner",
    "family_style",
    "mentor_peer",
}
AGREEMENT_STATUSES = {
    "draft",
    "proposed",
    "pending_confirmation",
    "active",
    "amended",
    "suspended",
    "revoked",
    "expired",
    "rejected",
}

OUTCOME_TYPES = {
    "accepted",
    "rejected",
    "corrected",
    "abandoned",
    "completed",
    "repair_acknowledged",
    "repair_accepted",
    "boundary_respected",
    "boundary_violated",
}

DOMAIN_EVENT_TYPES = {
    "state_snapshot_published",
    "estimate_snapshot_published",
    "agreement_changed",
    "agreement_confirmation_required",
    "repair_state_changed",
    "relationship_control_changed",
    "forget_applied",
    "frame_invalidated",
}


def bounded(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value)))


def normalized(value: float | Decimal) -> float:
    quantum = Decimal(1).scaleb(-RELATIONSHIP_VALUE_DECIMALS)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_EVEN))


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_payload(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def value_band(value: float) -> str:
    if value < 0.20:
        return "very_low"
    if value < 0.40:
        return "low"
    if value < 0.65:
        return "medium"
    if value < 0.82:
        return "high"
    return "very_high"


def geometric_mean(values: list[float]) -> float:
    if not values or any(value <= 0.0 for value in values):
        return 0.0
    return math.exp(sum(math.log(value) for value in values) / len(values))


@dataclass(frozen=True, slots=True)
class RelationshipInputEvent:
    event_id: str
    schema_version: str
    pair_id: str
    source_system: str
    source_revision: str
    action: str
    occurred_at: str
    evidence_refs: tuple[str, ...]
    origin_chain: tuple[str, ...]
    payload: Mapping[str, Any]
    correlation_id: str | None = None
    turn_id: str | None = None
    sensitivity: str = "low"

    def validate(self) -> None:
        if not all((self.event_id, self.schema_version, self.pair_id, self.source_system, self.source_revision, self.action)):
            raise ValueError("Relationship input event is missing required envelope fields")
        if self.sensitivity not in SENSITIVITY_LEVELS:
            raise ValueError(f"unsupported Relationship sensitivity: {self.sensitivity}")
        if len(self.evidence_refs) > 64 or len(self.origin_chain) > 16:
            raise ValueError("Relationship input envelope exceeds bounded list size")

    def to_dict(self) -> JsonDict:
        payload = asdict(self)
        payload["evidence_refs"] = list(self.evidence_refs)
        payload["origin_chain"] = list(self.origin_chain)
        payload["payload"] = dict(self.payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, pair_id: str | None = None) -> "RelationshipInputEvent":
        idempotency = dict(payload.get("idempotency") or {})
        source = dict(payload.get("source") or {})
        memory = dict(payload.get("memory") or {})
        event = cls(
            event_id=str(payload.get("event_id") or ""),
            schema_version=str(payload.get("schema_version") or payload.get("contract") or RELATIONSHIP_EVENT_CONTRACT),
            pair_id=str(pair_id or payload.get("pair_id") or ""),
            source_system=str(payload.get("source_system") or ("memory_projection" if payload.get("contract") == "ProjectionEvent.v1" else "unknown")),
            source_revision=str(payload.get("source_revision") or memory.get("updated_at") or idempotency.get("event_key") or "1"),
            action=str(payload.get("action") or "upsert"),
            occurred_at=str(payload.get("occurred_at") or memory.get("updated_at") or ""),
            evidence_refs=tuple(str(item) for item in payload.get("evidence_refs") or memory.get("evidence_ids") or ()),
            origin_chain=tuple(str(item) for item in payload.get("origin_chain") or ("memory",)),
            payload=dict(payload),
            correlation_id=(str(payload["correlation_id"]) if payload.get("correlation_id") else None),
            turn_id=(str(payload["turn_id"]) if payload.get("turn_id") else None),
            sensitivity=str(payload.get("sensitivity") or memory.get("sensitivity") or "low"),
        )
        event.validate()
        return event


@dataclass(frozen=True, slots=True)
class RelationshipEvidenceCandidate:
    candidate_id: str
    pair_id: str
    direction_scope: str
    dimension: str
    direction: int
    evidence_type: str
    source_kind: str
    source_ref: str
    evidence_refs: tuple[str, ...]
    confidence: float
    evidence_strength: float
    source_reliability: float
    direction_certainty: float
    consistency: float
    novelty: float = 1.0
    conflict_score: float = 0.0
    sensitivity: str = "low"
    subject_key: str = "general"
    context_key: str = "general"
    episode_ref: str | None = None
    reason_codes: tuple[str, ...] = ()
    operation: str = "add"
    supersedes: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = RELATIONSHIP_CANDIDATE_CONTRACT

    def validate(self) -> None:
        if self.schema_version != RELATIONSHIP_CANDIDATE_CONTRACT:
            raise ValueError(f"unsupported Relationship candidate contract: {self.schema_version}")
        if self.direction_scope not in DIRECTION_SCOPES:
            raise ValueError(f"unsupported Relationship direction scope: {self.direction_scope}")
        allowed = RELATIONSHIP_DIMENSIONS if self.direction_scope == "sora_to_user" else USER_ESTIMATE_DIMENSIONS
        if self.dimension not in allowed:
            raise ValueError(f"dimension {self.dimension} does not belong to {self.direction_scope}")
        if self.direction not in DIRECTIONS:
            raise ValueError("Relationship direction must be -1 or 1")
        if self.evidence_type not in EVIDENCE_TYPES:
            raise ValueError(f"unsupported Relationship evidence type: {self.evidence_type}")
        if not self.source_ref or not self.evidence_refs:
            raise ValueError("Relationship candidate requires source_ref and evidence_refs")
        if self.sensitivity not in SENSITIVITY_LEVELS:
            raise ValueError(f"unsupported Relationship sensitivity: {self.sensitivity}")
        for name in (
            "confidence",
            "evidence_strength",
            "source_reliability",
            "direction_certainty",
            "consistency",
            "novelty",
            "conflict_score",
        ):
            if not 0.0 <= float(getattr(self, name)) <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if len(self.evidence_refs) > 64 or len(self.reason_codes) > 24:
            raise ValueError("Relationship candidate exceeds bounded list size")

    def support(self, *, policy_damping: float) -> float:
        base = geometric_mean(
            [
                self.confidence,
                self.evidence_strength,
                self.source_reliability,
                self.direction_certainty,
                self.consistency,
            ]
        )
        return normalized(bounded(base * (1.0 - self.conflict_score) * bounded(policy_damping, 0.0, 1.0)))

    def candidate_hash(self) -> str:
        return hash_payload(
            {
                "operation": self.operation,
                "pair_id": self.pair_id,
                "direction_scope": self.direction_scope,
                "dimension": self.dimension,
                "direction": self.direction,
                "evidence_type": self.evidence_type,
                "subject_key": self.subject_key,
                "context_key": self.context_key,
                "source_ref": self.source_ref,
            }
        )

    def to_dict(self) -> JsonDict:
        payload = asdict(self)
        payload["evidence_refs"] = list(self.evidence_refs)
        payload["reason_codes"] = list(self.reason_codes)
        payload["supersedes"] = list(self.supersedes)
        payload["metadata"] = dict(self.metadata)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RelationshipEvidenceCandidate":
        direction = payload.get("direction", 0)
        if isinstance(direction, str):
            direction = 1 if direction.lower() in {"+", "positive", "increase", "up"} else -1
        candidate = cls(
            candidate_id=str(payload.get("candidate_id") or ""),
            pair_id=str(payload.get("pair_id") or ""),
            direction_scope=str(payload.get("direction_scope") or ""),
            dimension=str(payload.get("dimension") or ""),
            direction=int(direction),
            evidence_type=str(payload.get("evidence_type") or ""),
            source_kind=str(payload.get("source_kind") or "unknown"),
            source_ref=str(payload.get("source_ref") or ""),
            evidence_refs=tuple(str(item) for item in payload.get("evidence_refs") or ()),
            confidence=float(payload.get("confidence", 0.0)),
            evidence_strength=float(payload.get("evidence_strength", 0.0)),
            source_reliability=float(payload.get("source_reliability", 0.0)),
            direction_certainty=float(payload.get("direction_certainty", 0.0)),
            consistency=float(payload.get("consistency", 0.0)),
            novelty=float(payload.get("novelty", 1.0)),
            conflict_score=float(payload.get("conflict_score", 0.0)),
            sensitivity=str(payload.get("sensitivity") or "low"),
            subject_key=str(payload.get("subject_key") or "general")[:120],
            context_key=str(payload.get("context_key") or "general")[:120],
            episode_ref=(str(payload["episode_ref"]) if payload.get("episode_ref") else None),
            reason_codes=tuple(str(item)[:80] for item in payload.get("reason_codes") or ()),
            operation=str(payload.get("operation") or "add"),
            supersedes=tuple(str(item) for item in payload.get("supersedes") or ()),
            metadata=dict(payload.get("metadata") or {}),
            schema_version=str(payload.get("schema_version") or RELATIONSHIP_CANDIDATE_CONTRACT),
        )
        candidate.validate()
        return candidate


DEFAULT_STATE_VALUES: JsonDict = {
    "trust": 0.42,
    "familiarity": 0.18,
    "psychological_safety": 0.42,
    "respect": 0.62,
    "shared_history_density": 0.05,
    "comfort_rhythm": 0.35,
    "reciprocity_style": 0.35,
    "boundary_preference": 0.50,
    "affection": 0.28,
    "romantic_love": 0.05,
    "attachment": 0.16,
    "loyalty": 0.32,
    "commitment": 0.10,
    "dependency": 0.06,
    "jealousy": 0.04,
    "possessiveness": 0.03,
    "autonomy_balance": 0.72,
    "emotional_intimacy": 0.14,
    "predictability": 0.45,
    "repair_readiness": 0.50,
}

DEFAULT_ESTIMATE_VALUES: JsonDict = {
    "comfort": {"value": 0.35, "confidence": 0.10},
    "affection": {"value": 0.20, "confidence": 0.08},
    "attachment": {"value": 0.12, "confidence": 0.05},
    "romantic_interest": {"value": 0.05, "confidence": 0.03},
    "commitment": {"value": 0.08, "confidence": 0.04},
    "jealousy": {"value": 0.03, "confidence": 0.03},
    "trust": {"value": 0.35, "confidence": 0.08},
    "autonomy_balance": {"value": 0.72, "confidence": 0.10},
}

BASE_WEIGHTS: dict[str, dict[str, float]] = {
    "explicit_declaration": {"affection": 0.080, "romantic_love": 0.070, "attachment": 0.055, "commitment": 0.050, "comfort": 0.055, "romantic_interest": 0.050},
    "explicit_correction": {dimension: 0.120 for dimension in ALL_DIMENSIONS},
    "promise_fulfilled": {"trust": 0.055, "respect": 0.035, "commitment": 0.030},
    "promise_broken": {"trust": 0.070, "predictability": 0.045, "repair_readiness": 0.030},
    "boundary_respected": {"psychological_safety": 0.060, "respect": 0.050, "trust": 0.035},
    "boundary_violated": {"psychological_safety": 0.085, "respect": 0.065, "trust": 0.050},
    "shared_episode_completed": {"shared_history_density": 0.055, "familiarity": 0.030, "affection": 0.020},
    "vulnerable_disclosure_received_well": {"psychological_safety": 0.060, "emotional_intimacy": 0.050, "trust": 0.035},
    "repair_accepted": {"repair_readiness": 0.070, "trust": 0.035, "psychological_safety": 0.030},
    "repair_failed": {"repair_readiness": 0.080, "trust": 0.045},
    "affectionate_expression": {"affection": 0.025, "romantic_love": 0.020, "attachment": 0.018, "comfort": 0.018, "romantic_interest": 0.018},
    "cross_episode_interaction_pattern": {"comfort_rhythm": 0.030, "reciprocity_style": 0.028, "familiarity": 0.025, "comfort": 0.024},
    "worldops_shared_outcome": {"shared_history_density": 0.060, "familiarity": 0.030, "commitment": 0.020},
    "affect_sustained_outcome": {dimension: 0.012 for dimension in ALL_DIMENSIONS},
}

DEFAULT_POLICY_PROFILE: JsonDict = {
    "schema_version": "RelationshipPolicyProfile.v1",
    "version": RELATIONSHIP_POLICY_VERSION,
    "algorithm_version": RELATIONSHIP_ALGORITHM_VERSION,
    "stage_policy_version": RELATIONSHIP_STAGE_POLICY_VERSION,
    "frame_policy_version": RELATIONSHIP_FRAME_POLICY_VERSION,
    "novelty_window_days": 90,
    "conflict_window_days": 180,
    "novelty_floor": 0.20,
    "novelty_pool_limit": 64,
    "policy_damping": {"low": 1.00, "medium": 0.85, "high": 0.65, "restricted": 0.50},
    "per_event_cap": 0.08,
    "correction_cap": 0.15,
    "window_budgets": {"24h": 0.16, "7d": 0.32, "30d": 0.60},
    "negative_evidence_multiplier": 1.10,
    "deferred_ttl_days": 30,
    "soft_stale_seconds": 300,
    "hard_stale_seconds": 1800,
    "coalesce_milliseconds": 500,
    "write_queue_high_watermark": 500,
    "write_queue_max": 1000,
    "estimate_confidence_gain": 0.08,
    "estimate_confidence_evidence_prior": {
        "explicit_declaration": 1.00,
        "explicit_correction": 1.00,
        "promise_fulfilled": 0.90,
        "promise_broken": 0.90,
        "boundary_respected": 0.90,
        "boundary_violated": 0.95,
        "shared_episode_completed": 0.85,
        "vulnerable_disclosure_received_well": 0.85,
        "repair_accepted": 0.90,
        "repair_failed": 0.90,
        "affectionate_expression": 0.75,
        "cross_episode_interaction_pattern": 0.80,
        "worldops_shared_outcome": 0.90,
        "affect_sustained_outcome": 0.35,
    },
    "base_weights": BASE_WEIGHTS,
    "initial_state": DEFAULT_STATE_VALUES,
    "initial_estimate": DEFAULT_ESTIMATE_VALUES,
}
