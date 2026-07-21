from __future__ import annotations

from typing import Any, Mapping, Sequence

from .agreement import RelationshipAgreementService
from .dynamics import RelationshipDynamicsEngine
from .store import RelationshipStore


class RelationshipRepairService:
    """Explicit repair FSM that cannot declare repair from time passage or apology text alone."""

    def __init__(
        self,
        store: RelationshipStore,
        engine: RelationshipDynamicsEngine,
        agreement: RelationshipAgreementService,
    ) -> None:
        self.store = store
        self.engine = engine
        self.agreement = agreement

    def open(
        self,
        pair_id: str,
        *,
        trigger_ref: str,
        evidence_refs: Sequence[str],
        affected_dimensions: Sequence[str],
        responsibility: Mapping[str, Any] | None = None,
        requested_repair: Mapping[str, Any] | None = None,
        repeated_violation_key: str | None = None,
    ) -> dict[str, Any]:
        return self.store.create_repair(
            pair_id,
            trigger_ref=trigger_ref,
            evidence_refs=evidence_refs,
            affected_dimensions=affected_dimensions,
            responsibility=responsibility,
            requested_repair=requested_repair,
            repeated_violation_key=repeated_violation_key,
        )

    def transition(
        self,
        repair_id: str,
        to_state: str,
        *,
        actor: str,
        repair_action: Mapping[str, Any] | None = None,
        user_outcome: str | None = None,
        amendment: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = self.store.transition_repair(
            repair_id,
            to_state,
            engine=self.engine,
            actor=actor,
            repair_action=repair_action,
            user_outcome=user_outcome,
        )
        if to_state in {"accepted_difference", "repaired"} and amendment:
            repair = dict(result.get("repair") or {})
            result["agreement_amendment"] = self.agreement.propose(
                str(repair["pair_id"]),
                term_path=str(amendment["term_path"]),
                proposed_value=amendment.get("proposed_value"),
                reason_codes=list(amendment.get("reason_codes") or [to_state]),
                evidence_refs=list(amendment.get("evidence_refs") or repair.get("evidence_refs") or []),
                source_repair_episode_id=repair_id,
            )
        return result
