from __future__ import annotations

from typing import Any, Sequence

from .dynamics import RelationshipDynamicsEngine
from .store import RelationshipStore


class RelationshipAgreementService:
    """Confirmation-only mutation boundary for relationship agreements."""

    def __init__(self, store: RelationshipStore, engine: RelationshipDynamicsEngine) -> None:
        self.store = store
        self.engine = engine

    def propose(
        self,
        pair_id: str,
        *,
        term_path: str,
        proposed_value: Any,
        reason_codes: Sequence[str],
        evidence_refs: Sequence[str],
        source_repair_episode_id: str | None = None,
        operation: str = "add_or_replace_term",
    ) -> dict[str, Any]:
        return self.store.propose_agreement_amendment(
            pair_id,
            operation=operation,
            term_path=term_path,
            proposed_value=proposed_value,
            reason_codes=reason_codes,
            evidence_refs=evidence_refs,
            source_repair_episode_id=source_repair_episode_id,
        )

    def propose_revoke(
        self,
        pair_id: str,
        *,
        reason_codes: Sequence[str],
        evidence_refs: Sequence[str],
    ) -> dict[str, Any]:
        return self.propose(
            pair_id,
            operation="revoke_agreement",
            term_path="agreement.status",
            proposed_value="revoked",
            reason_codes=reason_codes,
            evidence_refs=evidence_refs,
        )

    def confirm(self, pair_id: str, candidate_id: str, token: str, *, actor: str = "user") -> dict[str, Any]:
        return self.store.confirm_agreement_amendment(pair_id, candidate_id, token, engine=self.engine, actor=actor)

    def revoke(self, pair_id: str, *, actor: str = "user", reason: str = "user_revoked") -> dict[str, Any]:
        return self.store.revoke_agreement(pair_id, engine=self.engine, actor=actor, reason=reason)
