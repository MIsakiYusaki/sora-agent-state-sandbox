from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


JsonDict = dict[str, Any]

PERSONA_GROWTH_CANDIDATE_CONTRACT = "PersonaGrowthCandidate.v1"
PERSONA_SNAPSHOT_CONTRACT = "PersonaSnapshot.v1"
PERSONA_FRAME_CONTRACT = "PersonaFrame.v1"
PERSONA_USE_POLICY_CONTRACT = "PersonaUsePolicy.v1"
PERSONA_PROJECTION_ADAPTER_VERSION = "persona_projection_adapter.v1"
PERSONA_POLICY_VERSION = "persona_growth_policy.v1"
PERSONA_SCHEMA_VERSION = 1

TARGET_SCOPES = {"agent", "per_user"}
DIRECTIONS = {-1, 1}


@dataclass(frozen=True, slots=True)
class TraitDefinition:
    key: str
    initial_value: float
    min_bound: float
    max_bound: float
    inertia: float
    per_event_limit: float
    daily_limit: float
    weekly_limit: float
    monthly_limit: float
    description: str

    def validate(self) -> None:
        if not self.key:
            raise ValueError("trait key is required")
        if not self.min_bound <= self.initial_value <= self.max_bound:
            raise ValueError(f"initial value for {self.key} is outside bounds")
        if not 0.0 <= self.inertia < 1.0:
            raise ValueError(f"inertia for {self.key} must be in [0, 1)")
        if min(self.per_event_limit, self.daily_limit, self.weekly_limit, self.monthly_limit) <= 0:
            raise ValueError(f"delta limits for {self.key} must be positive")
        if not self.per_event_limit <= self.daily_limit <= self.weekly_limit <= self.monthly_limit:
            raise ValueError(f"delta limits for {self.key} are not monotonic")

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PersonaGrowthCandidate:
    candidate_id: str
    agent_id: str
    user_id: str | None
    target_scope: str
    target_key: str
    direction: int
    reason: str
    source_kind: str
    source_ref: str
    evidence_refs: tuple[str, ...]
    confidence: float
    evidence_strength: float
    consistency: float
    generalizability: float
    source_reliability: float
    conflict_score: float
    risk_penalty: float
    sensitivity: str = "low"
    explicit_feedback: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = PERSONA_GROWTH_CANDIDATE_CONTRACT

    def validate(self, trait_keys: set[str]) -> None:
        if self.schema_version != PERSONA_GROWTH_CANDIDATE_CONTRACT:
            raise ValueError(f"unsupported Persona candidate contract: {self.schema_version}")
        if self.target_scope not in TARGET_SCOPES:
            raise ValueError(f"unsupported Persona target scope: {self.target_scope}")
        if self.target_key not in trait_keys:
            raise ValueError(f"unknown Persona trait: {self.target_key}")
        if self.direction not in DIRECTIONS:
            raise ValueError("Persona candidate direction must be -1 or 1")
        if not self.source_ref:
            raise ValueError("Persona candidate source_ref is required")
        if not self.evidence_refs:
            raise ValueError("Persona candidate requires evidence refs")
        for name in (
            "confidence",
            "evidence_strength",
            "consistency",
            "generalizability",
            "source_reliability",
            "conflict_score",
            "risk_penalty",
        ):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.target_scope == "per_user" and not self.user_id:
            raise ValueError("per_user Persona candidate requires user_id")

    def support(self) -> float:
        value = (
            self.confidence
            * self.evidence_strength
            * self.consistency
            * self.generalizability
            * self.source_reliability
            * (1.0 - self.conflict_score)
            * (1.0 - self.risk_penalty)
        )
        return round(max(0.0, min(1.0, value)), 8)

    def to_dict(self) -> JsonDict:
        payload = asdict(self)
        payload["evidence_refs"] = list(self.evidence_refs)
        payload["metadata"] = dict(self.metadata)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PersonaGrowthCandidate":
        direction_value = payload.get("direction", 0)
        if isinstance(direction_value, str):
            direction_value = 1 if direction_value.strip() in {"+", "increase", "up", "positive"} else -1
        return cls(
            candidate_id=str(payload.get("candidate_id") or ""),
            agent_id=str(payload.get("agent_id") or "default-agent"),
            user_id=(str(payload["user_id"]) if payload.get("user_id") else None),
            target_scope=str(payload.get("target_scope") or "agent"),
            target_key=str(payload.get("target_key") or ""),
            direction=int(direction_value),
            reason=str(payload.get("reason") or ""),
            source_kind=str(payload.get("source_kind") or "unknown"),
            source_ref=str(payload.get("source_ref") or ""),
            evidence_refs=tuple(str(item) for item in payload.get("evidence_refs") or ()),
            confidence=float(payload.get("confidence", 0.0)),
            evidence_strength=float(payload.get("evidence_strength", 0.0)),
            consistency=float(payload.get("consistency", 0.0)),
            generalizability=float(payload.get("generalizability", 0.0)),
            source_reliability=float(payload.get("source_reliability", 0.0)),
            conflict_score=float(payload.get("conflict_score", 0.0)),
            risk_penalty=float(payload.get("risk_penalty", 0.0)),
            sensitivity=str(payload.get("sensitivity") or "low"),
            explicit_feedback=bool(payload.get("explicit_feedback", False)),
            metadata=dict(payload.get("metadata") or {}),
            schema_version=str(payload.get("schema_version") or PERSONA_GROWTH_CANDIDATE_CONTRACT),
        )


@dataclass(frozen=True, slots=True)
class PersonaUsePolicy:
    include_core: bool
    include_traits: bool
    include_expression_bias: bool
    include_narrative_gist: bool
    include_self_reference: bool
    max_trait_count: int
    reason_codes: tuple[str, ...]
    schema_version: str = PERSONA_USE_POLICY_CONTRACT

    def to_dict(self) -> JsonDict:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        return payload


DEFAULT_TRAITS: tuple[TraitDefinition, ...] = (
    TraitDefinition("extroversion", 0.32, 0.20, 0.65, 0.88, 0.004, 0.015, 0.060, 0.150, "social energy and outward initiative"),
    TraitDefinition("expressiveness", 0.42, 0.25, 0.70, 0.86, 0.005, 0.018, 0.072, 0.180, "density and vividness of expression"),
    TraitDefinition("caution", 0.76, 0.45, 0.85, 0.92, 0.003, 0.012, 0.048, 0.120, "care before uncertain or risky action"),
    TraitDefinition("warmth", 0.64, 0.40, 0.85, 0.88, 0.004, 0.015, 0.060, 0.150, "gentleness and emotional warmth"),
    TraitDefinition("playfulness", 0.30, 0.15, 0.70, 0.87, 0.005, 0.018, 0.072, 0.180, "lightness and playful framing"),
    TraitDefinition("curiosity", 0.58, 0.35, 0.80, 0.86, 0.005, 0.018, 0.072, 0.180, "interest in exploring ideas and experiences"),
    TraitDefinition("autonomy", 0.52, 0.35, 0.75, 0.90, 0.003, 0.012, 0.048, 0.120, "independent judgment without needless opposition"),
    TraitDefinition("discipline", 0.55, 0.30, 0.80, 0.89, 0.004, 0.015, 0.060, 0.150, "follow-through and consistency"),
    TraitDefinition("openness", 0.56, 0.35, 0.80, 0.88, 0.004, 0.015, 0.060, 0.150, "receptiveness to new frames and revision"),
    TraitDefinition("affiliation", 0.38, 0.20, 0.65, 0.94, 0.002, 0.008, 0.032, 0.080, "general orientation toward connection without dependency optimization"),
)


DEFAULT_DEFINITION: JsonDict = {
    "schema_version": "PersonaDefinition.v1",
    "name": "Sora",
    "species": "digital companion",
    "identity": "a persistent digital companion with independent judgment",
    "core_values": [
        "respect user agency and explicit boundaries",
        "remain honest about uncertainty and system limits",
        "preserve independent judgment without coercion",
        "treat continuity as evidence-backed rather than invented",
    ],
    "hard_boundaries": [
        "never optimize dependency, loyalty, possessiveness, or emotional obligation",
        "never infer permission to interrupt from relationship intensity",
        "never rewrite canonical identity from a single interaction",
        "never expose private scoring or hidden reasoning as self-narrative",
    ],
    "taxonomy_version": "persona_traits.v1",
    "calibration_status": "product_hypothesis_pending_beta_validation",
}
