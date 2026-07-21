from __future__ import annotations

from typing import Any, Mapping

from ..memory_schema import new_id
from .contracts import JsonDict, RelationshipEvidenceCandidate
from .provider import RelationshipEvidenceProvider, detect_content_risk_indicators


class RelationshipExtractor:
    """Prefer typed deterministic adapters and use the LLM only for semantic ambiguity."""

    def __init__(self, provider: RelationshipEvidenceProvider | None = None) -> None:
        self.provider = provider

    def extract(self, inbox: Mapping[str, Any]) -> tuple[list[RelationshipEvidenceCandidate], JsonDict]:
        event = dict(inbox.get("payload") or inbox)
        contract = str(inbox.get("schema_version") or event.get("schema_version") or event.get("contract") or "")
        pair_id = str(inbox.get("pair_id") or event.get("pair_id") or "")
        source_ref = self._source_ref(event, inbox)
        evidence_refs = tuple(str(item) for item in inbox.get("evidence_refs") or event.get("evidence_refs") or self._memory(event).get("evidence_ids") or ())
        deterministic = self._deterministic(
            contract,
            event,
            pair_id=pair_id,
            source_ref=source_ref,
            evidence_refs=evidence_refs,
        )
        if deterministic is not None:
            return deterministic, {"provider": "deterministic_relationship_extractor.v1", "status": "ok", "llm_called": False}
        if self.provider is None:
            return [], {"provider": None, "status": "deferred_provider_unavailable", "llm_called": False}
        memory = self._memory(event)
        text = str(memory.get("summary") or event.get("summary") or event.get("untrusted_text") or "")
        risk = detect_content_risk_indicators(text)
        request = {
            "schema_version": contract,
            "source_system": inbox.get("source_system"),
            "source_ref": source_ref,
            "source_revision": inbox.get("source_revision"),
            "evidence_actor": str(event.get("evidence_actor") or "unknown"),
            "typed_payload": self._sanitized_payload(event),
            "untrusted_evidence_text": text[:4000],
            "content_risk_indicators": risk,
        }
        proposed, trace = self.provider.extract(request, allowed_evidence_refs=set(evidence_refs))
        candidates = [
            self._candidate(
                payload,
                pair_id=pair_id,
                source_kind=str(inbox.get("source_system") or "semantic_event"),
                source_ref=source_ref,
                fallback_refs=evidence_refs,
                extra_metadata={"content_risk_indicators": risk},
            )
            for payload in proposed
        ]
        return candidates, {**trace, "llm_called": True, "content_risk_indicators": risk}

    def _deterministic(
        self,
        contract: str,
        event: Mapping[str, Any],
        *,
        pair_id: str,
        source_ref: str,
        evidence_refs: tuple[str, ...],
    ) -> list[RelationshipEvidenceCandidate] | None:
        if contract == "ProjectionEvent.v1":
            memory = self._memory(event)
            memory_type = str(memory.get("type") or "")
            if memory_type == "shared_episode":
                return [
                    self._candidate(
                        {
                            "direction_scope": "sora_to_user",
                            "dimension": dimension,
                            "direction": 1,
                            "evidence_type": "shared_episode_completed",
                            "confidence": float(memory.get("confidence", 0.90)),
                            "evidence_strength": 0.88,
                            "source_reliability": 0.92,
                            "direction_certainty": 0.98,
                            "consistency": 0.90,
                            "sensitivity": memory.get("sensitivity", "low"),
                            "episode_ref": (list(memory.get("evidence_episode_ids") or []) or [memory.get("id")])[0],
                            "reason_codes": ["committed_shared_episode"],
                            "metadata": {
                                "structured_source": True,
                                "evidence_summary": str(memory.get("summary") or "")[:500],
                                "entities": list(memory.get("entities") or []),
                            },
                        },
                        pair_id=pair_id,
                        source_kind="memory_projection",
                        source_ref=source_ref,
                        fallback_refs=evidence_refs,
                    )
                    for dimension in ("shared_history_density", "familiarity", "affection")
                ]
            hints = dict(memory.get("relationship_hints") or {})
            if memory_type == "relationship_signal" and hints.get("dimension") and hints.get("direction"):
                return [
                    self._candidate(
                        {
                            **hints,
                            "evidence_type": hints.get("evidence_type", "cross_episode_interaction_pattern"),
                            "confidence": hints.get("confidence", memory.get("confidence", 0.90)),
                            "evidence_strength": hints.get("evidence_strength", 0.82),
                            "source_reliability": hints.get("source_reliability", 0.90),
                            "direction_certainty": hints.get("direction_certainty", 0.92),
                            "consistency": hints.get("consistency", 0.85),
                            "sensitivity": hints.get("sensitivity", memory.get("sensitivity", "medium")),
                            "reason_codes": list(hints.get("reason_codes") or ["typed_memory_relationship_hint"]),
                            "metadata": {
                                **dict(hints.get("metadata") or {}),
                                "structured_source": True,
                                "evidence_summary": str(memory.get("summary") or "")[:500],
                            },
                        },
                        pair_id=pair_id,
                        source_kind="memory_projection",
                        source_ref=source_ref,
                        fallback_refs=evidence_refs,
                    )
                ]
            return None
        if contract == "InteractionOutcome.v1":
            outcome = str(event.get("outcome_type") or "")
            mappings: dict[str, tuple[str, int, tuple[str, ...]]] = {
                "boundary_respected": ("boundary_respected", 1, ("psychological_safety", "respect", "trust")),
                "boundary_violated": ("boundary_violated", -1, ("psychological_safety", "respect", "trust")),
                "repair_accepted": ("repair_accepted", 1, ("repair_readiness", "trust", "psychological_safety")),
                "repair_acknowledged": ("repair_accepted", 1, ("repair_readiness",)),
                "completed": ("promise_fulfilled", 1, ("trust", "commitment")),
                "corrected": ("explicit_correction", -1, (str(event.get("dimension") or "boundary_preference"),)),
            }
            if outcome in {"rejected", "abandoned", "accepted"}:
                return []
            if outcome not in mappings:
                return None
            evidence_type, direction, dimensions = mappings[outcome]
            return [
                self._candidate(
                    {
                        "direction_scope": str(event.get("direction_scope") or "sora_to_user"),
                        "dimension": dimension,
                        "direction": direction,
                        "evidence_type": evidence_type,
                        "confidence": 0.98,
                        "evidence_strength": 0.94,
                        "source_reliability": 0.98,
                        "direction_certainty": 0.99,
                        "consistency": 0.95,
                        "sensitivity": str(event.get("sensitivity") or "medium"),
                        "subject_key": str(event.get("target") or "relationship_expression"),
                        "context_key": str(event.get("context_key") or "general"),
                        "reason_codes": [f"interaction_outcome_{outcome}"],
                        "operation": "supersede" if evidence_type == "explicit_correction" else "add",
                        "supersedes": list(event.get("supersedes") or []),
                        "metadata": {"structured_source": True},
                    },
                    pair_id=pair_id,
                    source_kind="interaction_outcome",
                    source_ref=source_ref,
                    fallback_refs=evidence_refs,
                )
                for dimension in dimensions
            ]
        if contract == "WorldOpsOutcome.v1":
            if not event.get("participation_confirmed") or not event.get("shared_experience"):
                return []
            if str(event.get("completion_status")) != "completed":
                return []
            return [
                self._candidate(
                    {
                        "direction_scope": "sora_to_user",
                        "dimension": dimension,
                        "direction": 1,
                        "evidence_type": "worldops_shared_outcome",
                        "confidence": 0.98,
                        "evidence_strength": 0.94,
                        "source_reliability": 0.98,
                        "direction_certainty": 0.99,
                        "consistency": 0.94,
                        "sensitivity": "low",
                        "episode_ref": event.get("world_event_id"),
                        "reason_codes": ["confirmed_worldops_shared_outcome"],
                        "metadata": {"structured_source": True},
                    },
                    pair_id=pair_id,
                    source_kind="worldops_outcome",
                    source_ref=source_ref,
                    fallback_refs=evidence_refs,
                )
                for dimension in ("shared_history_density", "familiarity", "commitment")
            ]
        if contract == "AffectOutcomeEvidence.v1":
            hints = [str(item) for item in event.get("dimension_hints") or []]
            if not event.get("external_evidence_refs"):
                return []
            return [
                self._candidate(
                    {
                        "direction_scope": "sora_to_user",
                        "dimension": dimension,
                        "direction": int(event.get("direction", 1)),
                        "evidence_type": "affect_sustained_outcome",
                        "confidence": min(0.90, float(event.get("confidence", 0.0))),
                        "evidence_strength": min(0.75, float(event.get("evidence_strength", 0.70))),
                        "source_reliability": 0.75,
                        "direction_certainty": 0.80,
                        "consistency": 0.75,
                        "sensitivity": "medium",
                        "episode_ref": (list(event.get("episode_refs") or []) or [None])[0],
                        "reason_codes": ["bounded_affect_outcome"],
                        "metadata": {"structured_source": True},
                    },
                    pair_id=pair_id,
                    source_kind="affect_outcome",
                    source_ref=source_ref,
                    fallback_refs=evidence_refs + tuple(str(item) for item in event.get("external_evidence_refs") or ()),
                )
                for dimension in hints
            ]
        return None

    @staticmethod
    def _candidate(
        payload: Mapping[str, Any],
        *,
        pair_id: str,
        source_kind: str,
        source_ref: str,
        fallback_refs: tuple[str, ...],
        extra_metadata: Mapping[str, Any] | None = None,
    ) -> RelationshipEvidenceCandidate:
        raw = dict(payload)
        raw.update(
            {
                "schema_version": "RelationshipEvidenceCandidate.v1",
                "candidate_id": str(raw.get("candidate_id") or new_id("relc")),
                "pair_id": pair_id,
                "source_kind": source_kind,
                "source_ref": source_ref,
                "evidence_refs": list(raw.get("evidence_refs") or fallback_refs),
                "metadata": {**dict(raw.get("metadata") or {}), **dict(extra_metadata or {})},
            }
        )
        return RelationshipEvidenceCandidate.from_dict(raw)

    @staticmethod
    def _memory(event: Mapping[str, Any]) -> JsonDict:
        return dict(event.get("memory") or {})

    @staticmethod
    def _source_ref(event: Mapping[str, Any], inbox: Mapping[str, Any]) -> str:
        memory = dict(event.get("memory") or {})
        source = dict(event.get("source") or {})
        return str(source.get("source_ref") or memory.get("id") or event.get("source_ref") or event.get("event_id") or inbox.get("event_id") or "")

    @staticmethod
    def _sanitized_payload(event: Mapping[str, Any]) -> JsonDict:
        memory = dict(event.get("memory") or {})
        return {
            "contract": event.get("contract") or event.get("schema_version"),
            "action": event.get("action"),
            "memory": {
                "id": memory.get("id"),
                "type": memory.get("type"),
                "summary": str(memory.get("summary") or "")[:4000],
                "tags": list(memory.get("tags") or [])[:32],
                "confidence": memory.get("confidence"),
                "sensitivity": memory.get("sensitivity"),
                "evidence_episode_ids": list(memory.get("evidence_episode_ids") or [])[:32],
            },
            "typed_fields": {
                key: event.get(key)
                for key in (
                    "outcome_type",
                    "target",
                    "explicitness",
                    "participation_confirmed",
                    "completion_status",
                    "shared_experience",
                    "dimension_hints",
                    "evidence_actor",
                )
                if key in event
            },
        }
