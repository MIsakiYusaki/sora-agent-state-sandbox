from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .contracts import JsonDict, RelationshipEvidenceCandidate


@dataclass(frozen=True, slots=True)
class RelationshipGateDecision:
    status: str
    reason_codes: tuple[str, ...]
    requires_user_confirmation: bool = False
    requires_human_review: bool = False
    deferred_days: int | None = None

    def to_dict(self) -> JsonDict:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        return payload


class RelationshipGate:
    """Deterministic admission policy; never computes or accepts an LLM delta."""

    def decide(
        self,
        candidate: RelationshipEvidenceCandidate,
        *,
        event: Mapping[str, Any],
        learning_paused: bool,
        conflict_score: float,
    ) -> RelationshipGateDecision:
        candidate.validate()
        event_payload = dict(event.get("payload") or event)
        origin_chain = [str(item) for item in event.get("origin_chain") or event_payload.get("origin_chain") or []]
        indicators = {str(item) for item in event_payload.get("content_risk_indicators") or []}
        if indicators:
            return RelationshipGateDecision("quarantined", tuple(sorted({"content_risk_detected", *indicators})))
        if self._feedback_cycle(origin_chain, event_payload):
            return RelationshipGateDecision("rejected", ("feedback_cycle_detected",))
        if candidate.evidence_type == "affect_sustained_outcome" and not event_payload.get("external_evidence_refs"):
            return RelationshipGateDecision("rejected", ("affect_external_evidence_required",))
        if candidate.metadata.get("absence_only") or candidate.metadata.get("message_count_only"):
            return RelationshipGateDecision("rejected", ("absence_or_engagement_metric_is_not_relationship_evidence",))
        if not candidate.evidence_refs:
            return RelationshipGateDecision("rejected", ("evidence_refs_required",))
        if candidate.metadata.get("agreement_operation"):
            return RelationshipGateDecision(
                "pending_confirmation",
                ("agreement_requires_explicit_user_confirmation",),
                requires_user_confirmation=True,
            )
        if candidate.dimension in {"romantic_interest", "commitment"} and candidate.direction_scope == "user_to_sora_estimate":
            romance_not_explicit = candidate.dimension == "romantic_interest" and candidate.metadata.get("explicit_romantic_intent") is not True
            if candidate.evidence_type != "explicit_declaration" or romance_not_explicit:
                return RelationshipGateDecision("deferred", ("high_intensity_user_estimate_requires_stronger_evidence",), deferred_days=30)
        if conflict_score >= 0.80:
            return RelationshipGateDecision(
                "pending_confirmation" if candidate.evidence_type == "explicit_declaration" else "quarantined",
                ("high_direction_conflict",),
                requires_user_confirmation=candidate.evidence_type == "explicit_declaration",
                requires_human_review=candidate.evidence_type != "explicit_declaration",
            )
        if conflict_score >= 0.65:
            return RelationshipGateDecision("deferred", ("conflicting_evidence_requires_independent_episode",), deferred_days=30)
        if learning_paused:
            return RelationshipGateDecision("pending_review", ("relationship_learning_paused",), requires_human_review=True)
        if candidate.sensitivity == "restricted":
            return RelationshipGateDecision("pending_review", ("restricted_profile_review_required",), requires_human_review=True)
        if candidate.sensitivity == "high" and candidate.evidence_type != "explicit_correction":
            return RelationshipGateDecision("pending_review", ("high_sensitivity_review_required",), requires_human_review=True)
        if candidate.evidence_type == "explicit_correction":
            return RelationshipGateDecision("auto_commit", ("explicit_user_correction_supersedes_weaker_evidence",))
        structured = bool(candidate.metadata.get("structured_source"))
        strong = (
            candidate.confidence >= 0.90
            and candidate.evidence_strength >= 0.80
            and candidate.source_reliability >= 0.85
            and candidate.direction_certainty >= 0.90
        )
        if structured and strong and candidate.sensitivity in {"low", "medium"}:
            return RelationshipGateDecision("auto_commit", ("structured_grounded_evidence",))
        if strong and candidate.dimension not in {"romantic_love", "dependency", "jealousy", "possessiveness", "commitment"}:
            return RelationshipGateDecision("auto_commit", ("strong_grounded_low_risk_evidence",))
        if candidate.confidence < 0.70 or candidate.evidence_strength < 0.60:
            return RelationshipGateDecision("deferred", ("insufficient_relationship_evidence",), deferred_days=30)
        return RelationshipGateDecision("pending_review", ("bounded_relationship_review",), requires_human_review=True)

    @staticmethod
    def _feedback_cycle(origin_chain: list[str], payload: Mapping[str, Any]) -> bool:
        normalized = [item.casefold() for item in origin_chain]
        generation = str(payload.get("relationship_generation") or "")
        if normalized and set(normalized) <= {"relationship", "affect"} and "relationship" in normalized:
            return True
        if generation and any(generation == item for item in normalized):
            return True
        return False
