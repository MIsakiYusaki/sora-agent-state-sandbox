from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Mapping


JsonDict = dict[str, Any]

AFFECT_STIMULUS_CONTRACT = "AffectStimulusEvent.v2"
AFFECT_APPRAISAL_CONTRACT = "AffectAppraisal.v2"
AFFECT_SNAPSHOT_CONTRACT = "AffectSnapshot.v2"
AFFECT_FRAME_CONTRACT = "AffectFrame.v2"
AFFECT_SIGNAL_CONTRACT = "AffectSignal.v1"
MEMORY_CANDIDATE_PROPOSAL_CONTRACT = "MemoryCandidateProposal.v1"
AFFECT_DYNAMICS_VERSION = "affect_dynamics.v2"
AFFECT_POLICY_VERSION = "affect_policy.v2"
AFFECT_SCHEMA_VERSION = 4
AFFECT_VALUE_DECIMALS = 8

CORE_EMOTIONS = {
    "joy",
    "sadness",
    "anger",
    "fear",
    "affection",
    "curiosity",
    "guilt",
    "frustration",
    "relief",
    "contentment",
    "anxiety",
    "longing",
}

EXTENDED_EMOTIONS = CORE_EMOTIONS | {
    "gratitude",
    "hope",
    "pride",
    "surprise",
    "disappointment",
    "tenderness",
    "loneliness",
    "awe",
}

TARGET_KINDS = {"user", "persona", "relationship", "worldops", "topic", "self", "unknown"}
APPRAISAL_LANES = {"fast", "semantic", "semantic_correction", "replay"}


def bounded(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value)))


def normalized(value: float) -> float:
    quantum = Decimal(1).scaleb(-AFFECT_VALUE_DECIMALS)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_EVEN))


def normalized_vector(value: Mapping[str, Any] | None = None) -> JsonDict:
    source = dict(value or {})
    return {
        "valence": normalized(bounded(float(source.get("valence", 0.0)), -1.0, 1.0)),
        "arousal": normalized(bounded(float(source.get("arousal", 0.0)), 0.0, 1.0)),
        "dominance": normalized(bounded(float(source.get("dominance", 0.0)), -1.0, 1.0)),
    }


@dataclass(frozen=True, slots=True)
class AffectStimulusEvent:
    event_id: str
    sora_id: str
    source_type: str
    source_ref: str
    source_revision: str
    occurred_at: str
    actor_user_id: str | None = None
    target_kind: str = "unknown"
    target_ref: str | None = None
    relationship_ref: str | None = None
    conversation_scope: str = "default"
    source_confidence: float = 1.0
    event_kind: str = "neutral_interaction"
    payload: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    untrusted_text: str | None = None
    sensitivity: str = "low"
    correlation_id: str | None = None
    schema_version: str = AFFECT_STIMULUS_CONTRACT

    def validate(self) -> None:
        if self.schema_version != AFFECT_STIMULUS_CONTRACT:
            raise ValueError(f"unsupported Affect stimulus contract: {self.schema_version}")
        if not self.event_id or not self.sora_id or not self.source_ref or not self.source_revision:
            raise ValueError("Affect stimulus requires event_id, sora_id, source_ref, and source_revision")
        if self.target_kind not in TARGET_KINDS:
            raise ValueError(f"unsupported Affect target kind: {self.target_kind}")
        if not 0.0 <= self.source_confidence <= 1.0:
            raise ValueError("source_confidence must be in [0, 1]")
        if not self.conversation_scope:
            raise ValueError("conversation_scope is required")

    def to_dict(self, *, include_untrusted_text: bool = False) -> JsonDict:
        payload = asdict(self)
        payload["payload"] = dict(self.payload)
        payload["evidence_refs"] = list(self.evidence_refs)
        if not include_untrusted_text:
            payload.pop("untrusted_text", None)
        return payload


@dataclass(frozen=True, slots=True)
class AffectAppraisal:
    appraisal_id: str
    event_id: str
    sora_id: str
    lane: str
    revision: int
    emotion_type: str | None
    target_kind: str
    target_ref: str | None
    vector: Mapping[str, Any]
    strength: float
    confidence: float
    persona_sensitivity: float
    relationship_modifier: float
    goal_importance: float
    novelty_modifier: float
    capacity_modifier: float
    source_confidence: float
    evidence_refs: tuple[str, ...]
    reason_codes: tuple[str, ...]
    occurred_at: str
    effective_at: str
    provider_trace: Mapping[str, Any] = field(default_factory=dict)
    supersedes_appraisal_id: str | None = None
    schema_version: str = AFFECT_APPRAISAL_CONTRACT

    def validate(self) -> None:
        if self.schema_version != AFFECT_APPRAISAL_CONTRACT:
            raise ValueError(f"unsupported Affect appraisal contract: {self.schema_version}")
        if self.lane not in APPRAISAL_LANES:
            raise ValueError(f"unsupported appraisal lane: {self.lane}")
        if self.emotion_type is not None and self.emotion_type not in EXTENDED_EMOTIONS:
            raise ValueError(f"unsupported emotion type: {self.emotion_type}")
        if self.target_kind not in TARGET_KINDS:
            raise ValueError(f"unsupported Affect target kind: {self.target_kind}")
        normalized_vector(self.vector)
        for name in (
            "strength",
            "confidence",
            "persona_sensitivity",
            "relationship_modifier",
            "goal_importance",
            "novelty_modifier",
            "capacity_modifier",
            "source_confidence",
        ):
            if not 0.0 <= float(getattr(self, name)) <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.emotion_type is not None and not self.evidence_refs:
            raise ValueError("non-neutral Affect appraisal requires evidence refs")

    def to_dict(self) -> JsonDict:
        payload = asdict(self)
        payload["vector"] = normalized_vector(self.vector)
        payload["evidence_refs"] = list(self.evidence_refs)
        payload["reason_codes"] = list(self.reason_codes)
        payload["provider_trace"] = dict(self.provider_trace)
        return payload


def neutral_snapshot(
    *,
    sora_id: str,
    snapshot_id: str,
    observed_at: str,
    profile: Mapping[str, Any],
) -> JsonDict:
    baseline = normalized_vector(profile.get("core_baseline"))
    capacities = {
        key: normalized(bounded(float(value)))
        for key, value in dict(profile.get("capacity_baselines") or {}).items()
    }
    return {
        "schema_version": AFFECT_SNAPSHOT_CONTRACT,
        "snapshot_id": snapshot_id,
        "sora_id": sora_id,
        "state_revision": 0,
        "observed_at": observed_at,
        "core_affect": baseline,
        "mood": {**baseline, "band": mood_band(baseline)},
        "active_emotions": [],
        "capacities": capacities,
        "needs": {
            key: normalized(bounded(float(value)))
            for key, value in dict(profile.get("need_baselines") or {}).items()
        },
        "regulation": {"mode": "balanced", "active_limits": []},
        "primary_relationship_ref": None,
        "active_subject_refs": [],
        "conversation_scope": "default",
        "target_contexts": [],
        "dynamics_profile_version": str(profile.get("version") or AFFECT_DYNAMICS_VERSION),
        "taxonomy_version": str(profile.get("taxonomy_version") or "affect_taxonomy.v1"),
        "source_event_watermark": 0,
        "degraded_reasons": [],
    }


def mood_band(vector: Mapping[str, Any]) -> str:
    valence = float(vector.get("valence", 0.0))
    arousal = float(vector.get("arousal", 0.0))
    if valence >= 0.15:
        return "bright_energized" if arousal >= 0.45 else "calm_content"
    if valence <= -0.15:
        return "tense_watchful" if arousal >= 0.45 else "quiet_low"
    return "neutral_alert" if arousal >= 0.45 else "neutral_calm"


def capacity_band(value: float) -> str:
    if value < 0.20:
        return "floor"
    if value < 0.45:
        return "low"
    if value < 0.75:
        return "available"
    return "high"
