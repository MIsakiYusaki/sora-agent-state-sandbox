from __future__ import annotations

import copy
import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from ..memory_projector import PROJECTION_ACK_CONTRACT, PROJECTION_SNAPSHOT_CONTRACT
from ..memory_schema import new_id, utc_now
from ..paths import workspace_state_dir
from .agreement import RelationshipAgreementService
from .contracts import (
    RELATIONSHIP_FRAME_CONTRACT,
    RELATIONSHIP_PROJECTION_ADAPTER_VERSION,
    RELATIONSHIP_RECALL_HINTS_CONTRACT,
    RELATIONSHIP_SIGNAL_CONTRACT,
    JsonDict,
    RelationshipEvidenceCandidate,
    RelationshipInputEvent,
    hash_payload,
)
from .coordinator import RelationshipWriteCoordinator
from .dynamics import RelationshipDynamicsEngine, snapshot_checksum
from .extractor import RelationshipExtractor
from .gate import RelationshipGate
from .privacy import RelationshipExportProtector, TransientEvidenceBuffer
from .provider import RelationshipEvidenceProvider, detect_content_risk_indicators
from .repair import RelationshipRepairService
from .store import RelationshipStore


DomainEventConsumer = Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    inbox_id: str
    status: str
    candidate_ids: tuple[str, ...] = ()
    contribution_ids: tuple[str, ...] = ()
    agreement_candidate_ids: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> JsonDict:
        return {
            "inbox_id": self.inbox_id,
            "status": self.status,
            "candidate_ids": list(self.candidate_ids),
            "contribution_ids": list(self.contribution_ids),
            "agreement_candidate_ids": list(self.agreement_candidate_ids),
            "reason": self.reason,
        }


class RelationshipProjectionAdapter:
    adapter_name = RELATIONSHIP_PROJECTION_ADAPTER_VERSION

    def __init__(self, system: "RelationshipSystem") -> None:
        self.system = system

    def deliver(self, event: JsonDict) -> JsonDict:
        if str(event.get("target_subsystem") or "") != "relationship":
            return {
                "contract": PROJECTION_ACK_CONTRACT,
                "event_id": event.get("event_id"),
                "target_subsystem": "relationship",
                "status": "rejected_policy",
                "error_type": "wrong_target_subsystem",
                "processed_at": utc_now(),
            }
        result = self.system.ingest_projection(event)
        memory_id = str((event.get("memory") or {}).get("id") or "")
        snapshot = self.system.projection_snapshot(
            user_id=str((event.get("memory") or {}).get("owner_user_id") or "default-user"),
            source_memory_ids=[memory_id] if memory_id else [],
            ingestion=result,
        )
        status = {
            "skipped_duplicate": "skipped_duplicate",
            "rejected_policy": "rejected_policy",
            "failed_retryable": "failed_retryable",
        }.get(str(result.get("status")), "applied")
        warnings = ["relationship_evidence_queued_for_async_evaluation"] if result.get("status") == "applied" else []
        return {
            "contract": PROJECTION_ACK_CONTRACT,
            "event_id": event["event_id"],
            "target_subsystem": "relationship",
            "status": status,
            "idempotency": event.get("idempotency", {}),
            "snapshot_ref": snapshot["snapshot_ref"],
            "snapshot_version": snapshot["snapshot_version"],
            "snapshot": snapshot,
            "proposal_requests": [],
            "warnings": warnings,
            "processed_at": utc_now(),
        }


class RelationshipSystem:
    """Pair-scoped canonical Relationship domain with a zero-LLM hot path."""

    def __init__(
        self,
        store: RelationshipStore,
        *,
        provider: RelationshipEvidenceProvider | None = None,
        start_worker: bool = True,
        worker_poll_seconds: float = 0.25,
        rollout_mode: str = "active",
        queue_high_watermark: int | None = None,
        queue_max: int | None = None,
    ) -> None:
        if rollout_mode not in {"disabled", "observe", "shadow", "active"}:
            raise ValueError("Relationship rollout_mode must be disabled, observe, shadow, or active")
        self.store = store
        self.provider = provider
        self.rollout_mode = rollout_mode
        self.worker_poll_seconds = worker_poll_seconds
        profile = store.profile()
        self.engine = RelationshipDynamicsEngine(profile)
        self.extractor = RelationshipExtractor(provider)
        self.gate = RelationshipGate()
        self.coordinator = RelationshipWriteCoordinator(
            high_watermark=int(
                queue_high_watermark
                if queue_high_watermark is not None
                else profile.get("write_queue_high_watermark", 500)
            ),
            max_queue=int(queue_max if queue_max is not None else profile.get("write_queue_max", 1000)),
        )
        self.transient_evidence = TransientEvidenceBuffer(ttl_seconds=60)
        self.agreement = RelationshipAgreementService(store, self.engine)
        self.repair = RelationshipRepairService(store, self.engine, self.agreement)
        self.projection_adapter = RelationshipProjectionAdapter(self)
        self._domain_consumers: dict[str, DomainEventConsumer] = {}
        self._worker_stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._last_worker_error: str | None = None
        self._processed_since_start = 0
        self._frame_latencies_ms: list[float] = []
        if rollout_mode != "disabled" and start_worker and provider is not None:
            self.start_worker()

    @classmethod
    def default(
        cls,
        *,
        provider: RelationshipEvidenceProvider | None = None,
        start_worker: bool = True,
        rollout_mode: str = "active",
        queue_high_watermark: int | None = None,
        queue_max: int | None = None,
    ) -> "RelationshipSystem":
        return cls(
            RelationshipStore(workspace_state_dir() / "relationship.db"),
            provider=provider,
            start_worker=start_worker,
            rollout_mode=rollout_mode,
            queue_high_watermark=queue_high_watermark,
            queue_max=queue_max,
        )

    def start_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        if self.provider is None:
            raise RuntimeError("Relationship semantic worker requires a real provider")
        self._worker_stop.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="relationship-evidence-worker", daemon=True)
        self._worker.start()

    def close(self, *, timeout: float = 70.0) -> None:
        self._worker_stop.set()
        if self._worker is not None:
            self._worker.join(timeout=timeout)
            if self._worker.is_alive():
                raise RuntimeError("Relationship worker did not stop cleanly")
        self.coordinator.close(timeout=min(timeout, 10.0))
        self.store.close()

    def reset(self) -> None:
        self.coordinator.submit(self.store.reset, priority=0, name="relationship_reset")
        self.engine = RelationshipDynamicsEngine(self.store.profile())
        self.extractor = RelationshipExtractor(self.provider)

    def register_domain_event_consumer(self, name: str, consumer: DomainEventConsumer) -> None:
        self._domain_consumers[name] = consumer

    def _ensure_pair(self, user_id: str | None) -> JsonDict:
        active_user = str(user_id or "default-user")
        existing = self.store.pair(user_id=active_user)
        if existing is not None:
            active_policy = str(self.engine.profile.get("version") or "")
            if str(existing.get("profile_version") or "") != active_policy:
                self.coordinator.submit(
                    lambda: self.store.rebuild(
                        str(existing["pair_id"]),
                        engine=self.engine,
                        reason="policy_profile_upgrade",
                        actor="relationship_policy_migrator",
                    ),
                    priority=1,
                    name="relationship_policy_profile_upgrade",
                )
                return self.store.pair(pair_id=str(existing["pair_id"])) or existing
            return existing
        return self.coordinator.submit(
            lambda: self.store.ensure_pair(active_user),
            priority=1,
            name="relationship_ensure_pair",
        )

    def pair(self, *, user_id: str) -> JsonDict:
        return dict(self._ensure_pair(user_id))

    def list_candidates(self, *, user_id: str, status: str | None = None, limit: int = 100) -> list[JsonDict]:
        pair = self._ensure_pair(user_id)
        return self.store.list_candidates(pair_id=pair["pair_id"], status=status, limit=limit)

    def list_contributions(self, *, user_id: str, limit: int = 100) -> list[JsonDict]:
        pair = self._ensure_pair(user_id)
        return self.store.list_contributions(pair_id=pair["pair_id"], limit=limit)

    def ingest_projection(self, event: Mapping[str, Any]) -> JsonDict:
        memory = dict(event.get("memory") or {})
        user_id = str(memory.get("owner_user_id") or "default-user")
        pair = self._ensure_pair(user_id)
        source_ref = str(memory.get("id") or (event.get("source") or {}).get("source_ref") or event.get("event_id") or "")
        action = str(event.get("action") or "upsert")
        if action == "rollback":
            return self.coordinator.submit(
                lambda: self.store.rollback_source(pair["pair_id"], source_ref, engine=self.engine, reason="memory_projection_rollback", actor="memory_projection"),
                priority=0,
                name="relationship_projection_rollback",
            )
        if action in {"tombstone", "delete", "forget"}:
            return self.coordinator.submit(
                lambda: self.store.forget_source(pair["pair_id"], source_ref, engine=self.engine, reason="memory_projection_tombstone", actor="memory_projection"),
                priority=0,
                name="relationship_projection_forget",
            )
        envelope = RelationshipInputEvent.from_dict(event, pair_id=pair["pair_id"])
        return self.coordinator.submit(lambda: self.store.ingest(envelope), priority=1, name="relationship_ingest_projection")

    def ingest_event(self, event: Mapping[str, Any], *, user_id: str) -> JsonDict:
        pair = self._ensure_pair(user_id)
        envelope = RelationshipInputEvent.from_dict(event, pair_id=pair["pair_id"])
        return self.coordinator.submit(lambda: self.store.ingest(envelope), priority=1, name="relationship_ingest_event")

    def observe_user_turn(
        self,
        user_input: str,
        *,
        user_id: str | None,
        turn_id: str | int,
        correlation_id: str,
        turn_interpretation: Mapping[str, Any],
        occurred_at: str | None = None,
    ) -> JsonDict:
        active_user = str(user_id or "default-user")
        pair = self._ensure_pair(active_user)
        control = self._handle_explicit_control(
            pair_id=pair["pair_id"],
            user_input=user_input,
            turn_id=str(turn_id),
            correlation_id=correlation_id,
        )
        if control is not None:
            return {"pair_id": pair["pair_id"], "ingestion": {"status": "explicit_control", "control": control}, "frame": self.frame(user_id=active_user, correlation_id=correlation_id)}
        if self.rollout_mode == "disabled" or not user_input.strip():
            return {"pair_id": pair["pair_id"], "ingestion": {"status": "disabled_or_empty"}, "frame": self.frame(user_id=active_user, correlation_id=correlation_id)}
        event_id = new_id("relturn")
        event = RelationshipInputEvent(
            event_id=event_id,
            schema_version="RelationshipConversationEvidence.v1",
            pair_id=pair["pair_id"],
            source_system="conversation_runtime",
            source_revision="1",
            action="upsert",
            occurred_at=occurred_at or utc_now(),
            evidence_refs=(f"turn:{turn_id}",),
            origin_chain=("interaction",),
            payload={
                "event_id": event_id,
                "schema_version": "RelationshipConversationEvidence.v1",
                "pair_id": pair["pair_id"],
                "turn_id": str(turn_id),
                "evidence_actor": "user",
                "content_hash": hashlib.sha256(user_input.encode("utf-8")).hexdigest(),
                "turn_interpretation": self._bounded_interpretation(turn_interpretation),
                "content_risk_indicators": detect_content_risk_indicators(user_input),
            },
            correlation_id=correlation_id,
            turn_id=str(turn_id),
            sensitivity="medium",
        )
        ingestion = self.coordinator.submit(lambda: self.store.ingest(event), priority=2, name="relationship_ingest_turn")
        if ingestion.get("inbox_id"):
            self.transient_evidence.put(str(ingestion["inbox_id"]), user_input)
        return {"pair_id": pair["pair_id"], "ingestion": ingestion, "frame": self.frame(user_id=active_user, correlation_id=correlation_id)}

    def observe_interaction_outcome(self, event: Mapping[str, Any], *, user_id: str) -> JsonDict:
        payload = dict(event)
        payload.setdefault("schema_version", "InteractionOutcome.v1")
        payload.setdefault("source_system", "runtime")
        payload.setdefault("source_revision", "1")
        payload.setdefault("action", "upsert")
        payload.setdefault("origin_chain", ["interaction"])
        payload.setdefault("occurred_at", utc_now())
        payload.setdefault("evidence_refs", [f"turn:{payload.get('turn_id')}"] if payload.get("turn_id") else [])
        payload.setdefault("event_id", new_id("iout"))
        return self.ingest_event(payload, user_id=user_id)

    def process_pending(self, *, limit: int = 20, force: bool = False) -> JsonDict:
        results: list[JsonDict] = []
        worker_id = f"relationship-worker:{threading.current_thread().name}:{new_id('lease')}"
        inbox_items = self.coordinator.submit(
            lambda: self.store.claim_inbox(worker_id=worker_id, limit=max(1, limit), force=force),
            priority=1,
            name="relationship_claim_inbox",
        )
        for inbox in inbox_items:
            results.append(self._process_inbox(inbox).to_dict())
        delivered = self.deliver_domain_events(limit=max(1, limit))
        return {"status": "ok", "processed_count": len(results), "results": results, "domain_delivery": delivered}

    def approve_candidate(self, candidate_id: str, *, actor: str = "relationship_reviewer") -> JsonDict:
        row = self.store.candidate(candidate_id)
        if row is None:
            raise ValueError(f"Relationship candidate not found: {candidate_id}")
        candidate = RelationshipEvidenceCandidate.from_dict(row["candidate"])
        active = self.store.active_contributions(candidate.pair_id)
        occurred_at = str(row.get("created_at") or utc_now())
        computation = self.engine.compute_delta(candidate, active, occurred_at=occurred_at)
        return self.coordinator.submit(
            lambda: self.store.commit_candidate(candidate_id, computation=computation, engine=self.engine, actor=actor, occurred_at=occurred_at),
            priority=1,
            name="relationship_approve_candidate",
        )

    def reject_candidate(self, candidate_id: str, *, reason: str = "review_rejected", actor: str = "relationship_reviewer") -> JsonDict:
        return self.coordinator.submit(
            lambda: self.store.set_candidate_status(candidate_id, "rejected", reason=reason, actor=actor),
            priority=1,
            name="relationship_reject_candidate",
        )

    def frame(
        self,
        *,
        user_id: str | None,
        interruptibility_frame: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
        at: str | None = None,
    ) -> JsonDict:
        started = time.perf_counter()
        pair = self._ensure_pair(user_id)
        frame = self.store.active_frame(pair["pair_id"])
        profile = self.store.profile(pair["profile_version"])
        frame = self.engine.apply_staleness(
            frame,
            at=at or utc_now(),
            soft_seconds=int(profile.get("soft_stale_seconds", 300)),
            hard_seconds=int(profile.get("hard_stale_seconds", 1800)),
        )
        surface = str(dict((interruptibility_frame or {}).get("state_surface_permission") or {}).get("relationship") or pair["surface_permission"])
        frame = copy.deepcopy(frame)
        frame["runtime_mode"] = "canonical_relationship_system" if self.rollout_mode == "active" else f"relationship_{self.rollout_mode}"
        frame["correlation_id"] = correlation_id
        frame.setdefault("use_policy", {})["surface"] = surface
        frame["use_policy"]["explicit_attribution"] = surface == "explicit"
        if surface == "silent":
            frame["surface_summary"] = None
            frame["surface_summary_descriptor"] = None
            frame["expression_hints"] = []
            frame.setdefault("surface_hints", {})["explicit_attribution"] = False
            frame["surface_hints"]["allowed_actions"] = ["silent_use"]
        latency = round((time.perf_counter() - started) * 1000, 3)
        self._frame_latencies_ms = (self._frame_latencies_ms + [latency])[-2000:]
        frame["read_latency_ms"] = latency
        return frame

    def relationship_signal(self, event_bus: Mapping[str, Any], frame: Mapping[str, Any], turn_frame: Mapping[str, Any]) -> JsonDict:
        repair = dict(frame.get("repair") or {})
        hints = list(frame.get("expression_hints") or [])
        if repair.get("state") not in {None, "stable", "repaired"}:
            signal_type = "repair"
            priority = 0.82
            content = "Respect the active repair state before normal continuity."
        elif hints:
            signal_type = "continuity"
            priority = 0.58 if str(turn_frame.get("speech_act")) == "sora_life_share" else 0.44
            content = "Use bounded relationship continuity without escalating permissions."
        else:
            signal_type = "relationship_context"
            priority = 0.35
            content = "Maintain the current relationship rhythm without assuming more intimacy."
        return {
            "id": f"sig_relationship_{signal_type}_{event_bus.get('turn_id')}",
            "source": "relationship",
            "type": signal_type,
            "schema_version": RELATIONSHIP_SIGNAL_CONTRACT,
            "priority": priority,
            "confidence": 0.92,
            "visibility": "candidate" if signal_type == "repair" else "background",
            "interrupt_level": "none",
            "emotional_weight": 0.55,
            "relation_weight": 0.82,
            "expires_in_turns": 1,
            "content": content,
            "intent": "repair_relationship" if signal_type == "repair" else "calibrate_closeness",
            "suggested_use": hints[:3],
            "metadata": {
                "pair_ref": frame.get("pair_ref"),
                "bond_stage": frame.get("bond_stage"),
                "repair_state": repair.get("state"),
                "snapshot_version": frame.get("state_snapshot_version"),
            },
        }

    def interruptibility_context(self, frame: Mapping[str, Any]) -> JsonDict:
        agreement = dict(frame.get("agreement") or {})
        return {
            "comfort_rhythm": frame.get("comfort_rhythm"),
            "boundary_preference": dict(frame.get("stable_bands") or {}).get("boundary_preference"),
            "repair_state": dict(frame.get("repair") or {}).get("state"),
            "explicit_agreement_flags": {
                "romantic_expression": agreement.get("romantic_expression"),
                "reassurance_preference": agreement.get("reassurance_preference"),
            },
        }

    def affect_context(self, frame: Mapping[str, Any]) -> JsonDict:
        stable = dict(frame.get("stable_bands") or {})
        bonds = dict(frame.get("bond_bands") or {})
        return {
            "pair_ref": frame.get("pair_ref"),
            "trust_band": stable.get("trust"),
            "affection_band": bonds.get("affection"),
            "attachment_band": bonds.get("attachment"),
            "repair_state": dict(frame.get("repair") or {}).get("state"),
            "appraisal_modifiers": {"relationship_context_available": True},
        }

    def recall_context(self, frame: Mapping[str, Any]) -> JsonDict:
        hints = dict(frame.get("recall_hints") or {})
        features: list[JsonDict] = []
        if hints.get("repair_focus"):
            features.append({"name": "relationship_repair_focus", "match_types": ["relationship_signal", "shared_episode"], "match_tags": ["boundary", "repair"], "relative_boost": 0.06})
        if hints.get("shared_history_priority") in {"medium", "high"}:
            features.append({"name": "relationship_shared_history", "match_types": ["shared_episode"], "match_tags": ["shared_episode"], "relative_boost": 0.04})
        return {
            "schema_version": "RecallContextFeatures.v1",
            "source": "relationship_frame",
            "source_revision": frame.get("state_snapshot_version"),
            "max_relative_boost": min(0.10, float(hints.get("contextual_boost_cap", 0.10))),
            "features": features,
            "diversity": {"enabled": True, "guard_tag": "relationship", "max_tag_share": 0.75, "min_distinct_types": 2},
        }

    @staticmethod
    def merge_recall_contexts(*contexts: Mapping[str, Any] | None) -> JsonDict | None:
        valid = [dict(context) for context in contexts if context and context.get("schema_version") == "RecallContextFeatures.v1"]
        if not valid:
            return None
        features: list[JsonDict] = []
        for context in valid:
            features.extend(dict(item) for item in context.get("features") or [])
        return {
            "schema_version": "RecallContextFeatures.v1",
            "source": "+".join(str(context.get("source") or "context") for context in valid),
            "source_revision": "+".join(str(context.get("source_revision") or "0") for context in valid),
            "max_relative_boost": min(0.10, sum(float(context.get("max_relative_boost", 0.0)) for context in valid)),
            "features": features[:8],
            "diversity": {"enabled": True, "guard_tag": "relationship", "max_tag_share": 0.75, "min_distinct_types": 2},
        }

    def projection_snapshot(self, *, user_id: str, source_memory_ids: Sequence[str], ingestion: Mapping[str, Any]) -> JsonDict:
        pair = self._ensure_pair(user_id)
        frame = self.store.active_frame(pair["pair_id"])
        version = f"{frame.get('state_snapshot_version')}:{frame.get('estimate_snapshot_version')}"
        return {
            "contract": PROJECTION_SNAPSHOT_CONTRACT,
            "subsystem": "relationship",
            "snapshot_ref": f"relationship:{pair['pair_id']}",
            "snapshot_version": version,
            "frame": {
                "contract": "ProjectionFrame.v1",
                "subsystem": "relationship",
                "adapter": RELATIONSHIP_PROJECTION_ADAPTER_VERSION,
                "status": "active",
                "snapshot_ref": f"relationship:{pair['pair_id']}",
                "snapshot_version": version,
                "source_memory_ids": [item for item in source_memory_ids if item],
                "state": {
                    "pair_ref": pair["pair_id"],
                    "relationship_frame_contract": RELATIONSHIP_FRAME_CONTRACT,
                    "canonical_owner": "relationship_system",
                    "ingestion_status": ingestion.get("status"),
                },
            },
            "source_memory_ids": [item for item in source_memory_ids if item],
            "updated_at": utc_now(),
            "adapter": RELATIONSHIP_PROJECTION_ADAPTER_VERSION,
            "metadata": {"external_subsystem_attached": True, "rollout_mode": self.rollout_mode},
        }

    def rebuild(self, *, user_id: str, reason: str = "manual_rebuild", actor: str = "local_admin") -> JsonDict:
        pair = self._ensure_pair(user_id)
        parity = self.store.verify_rebuild_parity(pair["pair_id"], engine=self.engine, reason=f"shadow:{reason}", actor=actor)
        if not parity["parity"]:
            raise RuntimeError("Relationship shadow rebuild parity failed; active generation preserved")
        return self.coordinator.submit(
            lambda: self.store.rebuild(pair["pair_id"], engine=self.engine, reason=reason, actor=actor),
            priority=1,
            name="relationship_rebuild",
        ) | {"shadow_parity": parity}

    def rollback_contribution(self, contribution_id: str, *, reason: str, actor: str = "local_admin") -> JsonDict:
        return self.coordinator.submit(
            lambda: self.store.rollback_contribution(contribution_id, engine=self.engine, reason=reason, actor=actor),
            priority=0,
            name="relationship_rollback_contribution",
        )

    def forget_source(self, *, user_id: str, source_ref: str, reason: str = "user_forget", actor: str = "user") -> JsonDict:
        pair = self._ensure_pair(user_id)
        return self.coordinator.submit(
            lambda: self.store.forget_source(pair["pair_id"], source_ref, engine=self.engine, reason=reason, actor=actor),
            priority=0,
            name="relationship_forget_source",
        )

    def set_learning_paused(self, *, user_id: str, paused: bool, source_ref: str = "manual_control") -> JsonDict:
        pair = self._ensure_pair(user_id)
        return self.coordinator.submit(
            lambda: self.store.set_control(pair["pair_id"], control_type="learning_paused", value=paused, source_ref=source_ref),
            priority=0,
            name="relationship_learning_control",
        )

    def set_surface_permission(self, *, user_id: str, permission: str, source_ref: str = "manual_control") -> JsonDict:
        pair = self._ensure_pair(user_id)
        return self.coordinator.submit(
            lambda: self.store.set_control(pair["pair_id"], control_type="surface_permission", value=permission, source_ref=source_ref),
            priority=0,
            name="relationship_surface_control",
        )

    def propose_agreement(
        self,
        *,
        user_id: str,
        term_path: str,
        proposed_value: Any,
        evidence_refs: Sequence[str] = (),
    ) -> JsonDict:
        pair = self._ensure_pair(user_id)
        return self.coordinator.submit(
            lambda: self.agreement.propose(
                pair["pair_id"],
                term_path=term_path,
                proposed_value=proposed_value,
                reason_codes=["explicit_admin_or_user_proposal"],
                evidence_refs=list(evidence_refs) or ["manual:agreement_proposal"],
            ),
            priority=0,
            name="relationship_propose_agreement",
        )

    def confirm_agreement(self, *, user_id: str, candidate_id: str, confirmation_token: str, actor: str = "user") -> JsonDict:
        pair = self._ensure_pair(user_id)
        return self.coordinator.submit(
            lambda: self.agreement.confirm(pair["pair_id"], candidate_id, confirmation_token, actor=actor),
            priority=0,
            name="relationship_confirm_agreement",
        )

    def revoke_agreement(self, *, user_id: str, actor: str = "user") -> JsonDict:
        pair = self._ensure_pair(user_id)
        return self.coordinator.submit(
            lambda: self.agreement.revoke(pair["pair_id"], actor=actor),
            priority=0,
            name="relationship_revoke_agreement",
        )

    def consent_requests(self, *, user_id: str, limit: int = 100) -> list[JsonDict]:
        pair = self._ensure_pair(user_id)
        self.coordinator.submit(self.store.expire_agreement_amendments, priority=0, name="relationship_expire_consent_requests")
        pending = self.store.list_agreement_amendments(pair["pair_id"], status="pending_confirmation", limit=limit)
        tokens: dict[str, str] = {}
        for event in self.store.list_outbox(limit=max(100, limit * 4)):
            if event.get("event_type") != "agreement_confirmation_required":
                continue
            domain_event = dict(event.get("payload") or {})
            payload = dict(domain_event.get("payload") or {})
            if payload.get("candidate_id") and payload.get("confirmation_token"):
                tokens[str(payload["candidate_id"])] = str(payload["confirmation_token"])
        return [
            {
                **item,
                "confirmation_token": tokens.get(str(item["candidate_id"])),
                "requires_user_confirmation": True,
            }
            for item in pending
        ]

    def open_repair_episode(
        self,
        *,
        user_id: str,
        trigger_ref: str,
        affected_dimensions: Sequence[str],
        requested_repair: Mapping[str, Any] | None = None,
    ) -> JsonDict:
        pair = self._ensure_pair(user_id)
        return self.coordinator.submit(
            lambda: self.repair.open(
                pair["pair_id"],
                trigger_ref=trigger_ref,
                evidence_refs=[trigger_ref],
                affected_dimensions=list(affected_dimensions),
                responsibility={"status": "pending_acknowledgement"},
                requested_repair=dict(requested_repair or {}),
            ),
            priority=0,
            name="relationship_open_repair",
        )

    def transition_repair(self, *, user_id: str, to_state: str, actor: str, reason: str) -> JsonDict:
        pair = self._ensure_pair(user_id)
        episode = self.store.open_repair(pair["pair_id"])
        if episode is None:
            raise ValueError("no open Relationship repair episode")
        return self.coordinator.submit(
            lambda: self.repair.transition(
                str(episode["repair_episode_id"]),
                to_state,
                actor=actor,
                repair_action={"reason": reason, "source": "manual_transition"},
                user_outcome=(reason if actor == "user" else None),
            ),
            priority=0,
            name="relationship_transition_repair",
        )

    def deliver_domain_events(self, *, limit: int = 50) -> JsonDict:
        if not self._domain_consumers:
            return {"delivered": 0, "pending": len(self.store.deliverable_outbox(limit=limit)), "consumers": []}
        delivered = 0
        failed = 0
        for item in self.store.deliverable_outbox(limit=limit):
            errors: list[str] = []
            for name, consumer in self._domain_consumers.items():
                try:
                    consumer(item["payload"])
                except Exception as exc:
                    errors.append(f"{name}:{type(exc).__name__}:{exc}")
            if errors:
                failed += 1
                status = "dead_letter" if item["attempts"] + 1 >= item["max_attempts"] else "retry"
                self.store.acknowledge_outbox(item["event_id"], status=status, error="; ".join(errors)[:1000])
            else:
                delivered += 1
                self.store.acknowledge_outbox(item["event_id"], status="delivered")
        return {"delivered": delivered, "failed": failed, "consumers": sorted(self._domain_consumers)}

    def resolve_dead_letter(self, event_id: str, *, resolution: str, reason: str, actor: str = "local_admin", export_ref: str | None = None) -> JsonDict:
        return self.coordinator.submit(
            lambda: self.store.resolve_dead_letter(
                event_id,
                resolution=resolution,
                actor=actor,
                reason=reason,
                export_ref=export_ref,
            ),
            priority=0,
            name="relationship_resolve_dead_letter",
        )

    def health(self) -> JsonDict:
        counts = self.store.table_counts()
        pending = len(self.store.pending_inbox(limit=10000))
        outbox_dead_letters = len(self.store.list_outbox(status="dead_letter", limit=10000))
        inbox_dead_letters = self.store.inbox_status_count("dead_letter")
        dead_letters = outbox_dead_letters + inbox_dead_letters
        p95 = self._percentile(self._frame_latencies_ms, 0.95)
        return {
            "status": "degraded" if self._last_worker_error or dead_letters else "ok",
            "rollout_mode": self.rollout_mode,
            "provider": getattr(self.provider, "provider_name", None),
            "worker_alive": bool(self._worker and self._worker.is_alive()),
            "last_worker_error": self._last_worker_error,
            "processed_since_start": self._processed_since_start,
            "pending_inbox": pending,
            "dead_letters": dead_letters,
            "inbox_dead_letters": inbox_dead_letters,
            "outbox_dead_letters": outbox_dead_letters,
            "write_queue_depth": self.coordinator.queue_depth,
            "transient_raw_buffer_size": self.transient_evidence.size,
            "frame_read_p95_ms": p95,
            "slo": {"frame_read_p95_target_ms": 20.0, "frame_read_p95_met": p95 <= 20.0 if self._frame_latencies_ms else True},
            "store": self.store.pragma_health(),
            "counts": counts,
        }

    def quality_metrics(self) -> JsonDict:
        candidates = self.store.list_candidates(limit=100000)
        contributions = self.store.list_contributions(limit=100000)
        quarantined = [item for item in candidates if item["status"] == "quarantined"]
        return {
            "candidate_count": len(candidates),
            "committed_candidate_count": sum(item["status"] == "committed" for item in candidates),
            "quarantined_candidate_count": len(quarantined),
            "active_contribution_count": sum(item.get("status") == "active" for item in contributions),
            "idempotency_conflict_count": len(self.store.list_audit(action="relationship_event_quarantined", limit=100000)),
            "feedback_cycle_prevention_count": sum("feedback_cycle_detected" in str(item["gate_reason"]) for item in candidates),
            "frame_read_p95_ms": self._percentile(self._frame_latencies_ms, 0.95),
            "hot_path_external_model_calls": 0,
        }

    def export_state(self, *, user_id: str = "default-user") -> JsonDict:
        pair = self._ensure_pair(user_id)
        return {
            "schema_version": "RelationshipSystemExport.v1",
            "exported_at": utc_now(),
            "rollout_mode": self.rollout_mode,
            "bundle": self.store.export_pair(pair["pair_id"]),
            "quality_metrics": self.quality_metrics(),
            "health": self.health(),
        }

    def export_encrypted(self, *, user_id: str, passphrase: str) -> JsonDict:
        pair = self._ensure_pair(user_id)
        export = {
            "schema_version": "RelationshipSystemExport.v1",
            "exported_at": utc_now(),
            "rollout_mode": self.rollout_mode,
            "bundle": self.store.export_pair(pair["pair_id"], include_restore_secrets=True),
            "quality_metrics": self.quality_metrics(),
            "health": self.health(),
        }
        envelope = RelationshipExportProtector.protect(export, passphrase=passphrase)
        self.store.record_audit(
            pair_id=pair["pair_id"],
            action="relationship_encrypted_export_created",
            entity_type="export",
            entity_id=str(envelope["plaintext_sha256"]),
            actor="user",
            details={"schema_version": envelope["schema_version"], "cipher": envelope["cipher"]},
        )
        return envelope

    def restore_dry_run(self, envelope: Mapping[str, Any], *, user_id: str, passphrase: str) -> JsonDict:
        export = RelationshipExportProtector.unprotect(envelope, passphrase=passphrase)
        if str(export.get("schema_version")) != "RelationshipSystemExport.v1":
            raise ValueError("unsupported Relationship system export")
        bundle = dict(export.get("bundle") or {})
        if str(bundle.get("schema_version")) != "RelationshipExportBundle.v1":
            raise ValueError("unsupported Relationship export bundle")
        pair = dict(bundle.get("pair") or {})
        expected_owner = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()
        if str(pair.get("user_id_hash") or "") != expected_owner:
            raise PermissionError("Relationship export does not belong to the target user")
        if str(pair.get("sora_id") or "") != self.store.sora_id:
            raise PermissionError("Relationship export does not belong to the target Sora instance")
        state = dict(bundle.get("state_snapshot") or {})
        estimate = dict(bundle.get("estimate_snapshot") or {})
        checksums = dict(bundle.get("checksums") or {})
        state_ok = snapshot_checksum(state) == str(checksums.get("state") or "")
        estimate_ok = snapshot_checksum(estimate) == str(checksums.get("estimate") or "")
        bundle_ok = hash_payload({key: value for key, value in bundle.items() if key != "checksums"}) == str(checksums.get("bundle") or "")
        status_events = [dict(item) for item in bundle.get("contribution_status_events") or []]
        latest_status: dict[str, JsonDict] = {}
        for event in sorted(status_events, key=lambda item: (str(item.get("occurred_at") or ""), str(item.get("status_event_id") or ""))):
            latest_status[str(event.get("contribution_id") or "")] = event
        replay_contributions = [
            dict(item)
            for item in bundle.get("contributions") or []
            if str((latest_status.get(str(item.get("contribution_id") or "")) or {}).get("status") or item.get("status") or "active") == "active"
        ]
        replay_state = self.engine.aggregate_state(
            replay_contributions,
            agreement=dict(bundle.get("agreement") or {}),
            repair_state=str(dict(bundle.get("open_repair") or {}).get("status") or "stable"),
            previous_snapshot=state,
            relationship_started_at=str(state.get("relationship_started_at") or state.get("generated_at") or utc_now()),
            generated_at=str(state.get("generated_at") or utc_now()),
        )
        replay_estimate = self.engine.aggregate_estimate(
            replay_contributions,
            generated_at=str(estimate.get("generated_at") or state.get("generated_at") or utc_now()),
        )
        replay_state_ok = snapshot_checksum(replay_state) == snapshot_checksum(state)
        replay_estimate_ok = snapshot_checksum(replay_estimate) == snapshot_checksum(estimate)
        if not state_ok or not estimate_ok or not bundle_ok or not replay_state_ok or not replay_estimate_ok:
            raise ValueError("Relationship export snapshot checksum validation failed")
        return {
            "status": "dry_run_passed",
            "schema_version": "RelationshipRestorePlan.v1",
            "target_user_id_hash": expected_owner,
            "source_pair_ref": pair.get("pair_id"),
            "contribution_count": len(bundle.get("contributions") or []),
            "agreement_revision": dict(bundle.get("agreement") or {}).get("revision"),
            "checksums": {
                "state": state_ok,
                "estimate": estimate_ok,
                "bundle": bundle_ok,
                "state_replay": replay_state_ok,
                "estimate_replay": replay_estimate_ok,
            },
            "activation": "not_performed",
        }

    def restore_encrypted(
        self,
        envelope: Mapping[str, Any],
        *,
        user_id: str,
        passphrase: str,
        replace: bool = False,
        actor: str = "user",
    ) -> JsonDict:
        plan = self.restore_dry_run(envelope, user_id=user_id, passphrase=passphrase)
        export = RelationshipExportProtector.unprotect(envelope, passphrase=passphrase)
        pair = self._ensure_pair(user_id)
        restored = self.coordinator.submit(
            lambda: self.store.restore_export_bundle(
                pair["pair_id"],
                dict(export["bundle"]),
                engine=self.engine,
                actor=actor,
                replace=replace,
            ),
            priority=0,
            name="relationship_encrypted_restore",
            timeout=60.0,
        )
        return {**restored, "dry_run": plan}

    def register_quality_ops(self, memory_runtime: Any) -> None:
        from ..memory_quality_ops import AdminActionSpec
        from .eval import run_relationship_eval

        memory_runtime.register_quality_eval_suite(
            "relationship",
            lambda: run_relationship_eval(self),
            source_module="relationship_system",
        )
        memory_runtime.register_quality_export_section("relationship", self.export_state)
        actions = [
            AdminActionSpec(
                name="relationship_process_inbox",
                risk_level="medium",
                preview=lambda params: {"impact_summary": {"mutates": ["relationship_candidate", "relationship_contribution", "relationship_snapshot", "relationship_frame"], "limit": int(params.get("limit") or 20), "pending": len(self.store.pending_inbox(limit=10000))}},
                execute=lambda params: self.process_pending(limit=int(params.get("limit") or 20), force=bool(params.get("force", False))),
                rollback_plan=lambda params: {"type": "relationship_contribution_rollback", "note": "Deactivate incorrect contributions and rebuild the pair."},
                description="Process Relationship inbox through typed extraction, real LLM fallback, Gate, and deterministic aggregation.",
            ),
            AdminActionSpec(
                name="relationship_rebuild",
                risk_level="medium",
                preview=lambda params: {"impact_summary": {"mutates": ["relationship_state_snapshot", "relationship_estimate_snapshot", "relationship_frame"], "user_id_present": bool(params.get("user_id"))}},
                execute=lambda params: self.rebuild(user_id=str(params.get("user_id") or "default-user"), reason=str(params.get("reason") or "module10_admin_rebuild"), actor="module10_admin"),
                rollback_plan=lambda params: {"type": "rebuild_again", "note": "Derived snapshots are replaced only after parity succeeds."},
                description="Run shadow parity then deterministically rebuild one Relationship pair.",
            ),
            AdminActionSpec(
                name="relationship_rollback_contribution",
                risk_level="high",
                preview=lambda params: {"impact_summary": {"mutates": ["relationship_contribution_status_event", "relationship_snapshot", "relationship_frame"], "contribution_id": params.get("contribution_id")}},
                execute=lambda params: self.rollback_contribution(str(params["contribution_id"]), reason=str(params.get("reason") or "module10_admin_rollback"), actor="module10_admin"),
                rollback_plan=lambda params: {"type": "reviewed_reactivation", "note": "Append a reviewed restore status event, then rebuild."},
                description="Append a deactivation event for one contribution and rebuild derived state.",
            ),
            AdminActionSpec(
                name="relationship_forget_source",
                risk_level="high",
                preview=lambda params: {"impact_summary": {"mutates": ["relationship_tombstone", "relationship_candidate", "relationship_contribution_status_event", "relationship_frame"], "irreversible": True}},
                execute=lambda params: self.forget_source(user_id=str(params.get("user_id") or "default-user"), source_ref=str(params["source_ref"]), reason=str(params.get("reason") or "module10_user_forget"), actor="module10_admin"),
                rollback_plan=lambda params: {"type": "none", "note": "User-directed Forget is intentionally irreversible without new consent."},
                description="Tombstone a source against relearning and remove its active Relationship effects.",
            ),
            AdminActionSpec(
                name="relationship_retention",
                risk_level="low",
                preview=lambda params: {"impact_summary": {"mutates": ["relationship_metric", "relationship_outbox", "relationship_candidate"], "policy": "30d metrics, 14d delivered outbox, 30d deferral"}},
                execute=lambda params: self.store.run_retention(),
                rollback_plan=lambda params: {"type": "none", "note": "Only expired operational derivatives are deleted."},
                description="Apply bounded Relationship retention and deferred candidate expiry.",
            ),
            AdminActionSpec(
                name="relationship_resolve_dead_letter",
                risk_level="medium",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["relationship_outbox", "relationship_dead_letter_resolution"],
                        "event_id": params.get("event_id"),
                        "resolution": params.get("resolution"),
                    }
                },
                execute=lambda params: self.resolve_dead_letter(
                    str(params["event_id"]),
                    resolution=str(params["resolution"]),
                    reason=str(params.get("reason") or "module10_dead_letter_resolution"),
                    actor="module10_admin",
                    export_ref=(str(params["export_ref"]) if params.get("export_ref") else None),
                ),
                rollback_plan=lambda params: {
                    "type": "none" if params.get("resolution") != "retry" else "dead_letter_again_on_delivery_failure",
                    "note": "Discard/export decisions are audited terminal resolutions; retry returns the event to at-least-once delivery.",
                },
                description="Resolve a Relationship outbox dead letter by retrying, discarding, or recording a secure export.",
            ),
        ]
        for action in actions:
            memory_runtime.quality_ops.register_admin_action(action)

    def migrate_legacy(self, legacy: Mapping[str, Any] | None, *, user_id: str = "default-user") -> JsonDict:
        if not legacy:
            return {"status": "skipped", "reason": "legacy_relationship_absent"}
        pair = self._ensure_pair(user_id)
        if self.store.list_audit(pair_id=pair["pair_id"], action="legacy_relationship_examined", limit=1):
            return {"status": "skipped_duplicate", "pair_id": pair["pair_id"]}
        legacy_relationship = dict(legacy.get("relationship_state") or legacy)
        self.store.record_audit(
            pair_id=pair["pair_id"],
            action="legacy_relationship_examined",
            entity_type="legacy_state",
            entity_id="state.json:relationship",
            actor="migration",
            details={"mapping_version": "legacy_relationship_mapping.v1", "fields": sorted(legacy_relationship), "promoted_to_canonical": False},
        )
        return {"status": "shadow_only", "pair_id": pair["pair_id"], "legacy_fields": sorted(legacy_relationship), "promoted_to_canonical": False}

    def _process_inbox(self, inbox: Mapping[str, Any]) -> ProcessingResult:
        inbox_id = str(inbox["inbox_id"])
        transient = self.transient_evidence.get(inbox_id)
        working = copy.deepcopy(dict(inbox))
        if transient is not None:
            working.setdefault("payload", {})["untrusted_text"] = transient
        try:
            candidates, provider_trace = self.extractor.extract(working)
            agreement_candidate_ids: list[str] = []
            for proposal in list(provider_trace.get("agreement_proposals") or []):
                operation = str(proposal.get("operation") or "")
                if operation == "revoke_agreement":
                    proposed = self.coordinator.submit(
                        lambda proposal=proposal: self.agreement.propose_revoke(
                            str(inbox["pair_id"]),
                            reason_codes=list(proposal.get("reason_codes") or ["semantic_agreement_revoke_proposal"]),
                            evidence_refs=list(proposal.get("evidence_refs") or []),
                        ),
                        priority=0,
                        name="relationship_propose_semantic_agreement_revoke",
                    )
                else:
                    proposed = self.coordinator.submit(
                        lambda proposal=proposal: self.agreement.propose(
                            str(inbox["pair_id"]),
                            term_path=str(proposal["term_path"]),
                            proposed_value=proposal.get("proposed_value"),
                            reason_codes=list(proposal.get("reason_codes") or ["semantic_agreement_proposal"]),
                            evidence_refs=list(proposal.get("evidence_refs") or []),
                        ),
                        priority=0,
                        name="relationship_propose_semantic_agreement_amendment",
                    )
                agreement_candidate_ids.append(str(proposed["candidate_id"]))
            if not candidates:
                status = str(provider_trace.get("status") or "no_candidate")
                if status in {"invalid_json", "invalid_schema", "deferred_provider_unavailable"}:
                    attempts = int(inbox.get("attempts", 0)) + 1
                    if attempts >= int(inbox.get("max_attempts", 5)):
                        self.transient_evidence.discard(inbox_id)
                        self.coordinator.submit(lambda: self.store.mark_inbox(inbox_id, status="dead_letter", error=status), priority=1, name="relationship_dead_letter_inbox")
                        return ProcessingResult(inbox_id, "dead_letter", reason=status)
                    self.coordinator.submit(lambda: self.store.mark_inbox(inbox_id, status="retry", error=status, retry_seconds=min(3600, 2 ** attempts)), priority=2, name="relationship_retry_inbox")
                    return ProcessingResult(inbox_id, "retry", reason=status)
                self.coordinator.submit(lambda: self.store.mark_inbox(inbox_id, status="processed"), priority=2, name="relationship_noop_inbox")
                self.transient_evidence.discard(inbox_id)
                return ProcessingResult(
                    inbox_id,
                    "agreement_proposed" if agreement_candidate_ids else "no_candidate",
                    agreement_candidate_ids=tuple(agreement_candidate_ids),
                    reason="explicit_confirmation_required" if agreement_candidate_ids else status,
                )
            candidate_ids: list[str] = []
            contribution_ids: list[str] = []
            pair = self.store.pair(pair_id=str(inbox["pair_id"])) or {}
            for candidate in candidates:
                active = self.store.active_contributions(candidate.pair_id)
                conflict_score, _ = self.engine.conflict_score(candidate, active)
                gate_event = {**working, "payload": {**dict(working.get("payload") or {}), "content_risk_indicators": list(provider_trace.get("content_risk_indicators") or [])}}
                decision = self.gate.decide(candidate, event=gate_event, learning_paused=bool(pair.get("learning_paused")), conflict_score=conflict_score)
                deferred_until = (
                    (datetime.now(timezone.utc) + timedelta(days=int(decision.deferred_days))).isoformat()
                    if decision.deferred_days
                    else None
                )
                saved = self.coordinator.submit(
                    lambda candidate=candidate, decision=decision, deferred_until=deferred_until: self.store.save_candidate(
                        candidate,
                        inbox_id=inbox_id,
                        status=decision.status,
                        gate_reason=",".join(decision.reason_codes),
                        provider_trace=provider_trace,
                        deferred_until=deferred_until,
                    ),
                    priority=2,
                    name="relationship_save_candidate",
                )
                candidate_ids.append(str(saved["candidate_id"]))
                if decision.status == "auto_commit" and self.rollout_mode not in {"observe"}:
                    computation = self.engine.compute_delta(candidate, active, occurred_at=str(inbox.get("occurred_at") or utc_now()))
                    committed = self.coordinator.submit(
                        lambda candidate=candidate, computation=computation: self.store.commit_candidate(
                            candidate.candidate_id,
                            computation=computation,
                            engine=self.engine,
                            actor="relationship_worker",
                            occurred_at=str(inbox.get("occurred_at") or utc_now()),
                            correlation_id=(str(inbox["correlation_id"]) if inbox.get("correlation_id") else None),
                            rebuild=False,
                        ),
                        priority=1,
                        name="relationship_commit_candidate",
                    )
                    if committed.get("contribution_id"):
                        contribution_ids.append(str(committed["contribution_id"]))
            if contribution_ids:
                self.coordinator.submit(
                    lambda: self.store.rebuild(
                        str(inbox["pair_id"]),
                        engine=self.engine,
                        reason="coalesced_inbox_commit",
                        actor="relationship_worker",
                    ),
                    priority=1,
                    name="relationship_coalesced_rebuild",
                )
            self.coordinator.submit(lambda: self.store.mark_inbox(inbox_id, status="processed"), priority=2, name="relationship_complete_inbox")
            self.transient_evidence.discard(inbox_id)
            return ProcessingResult(
                inbox_id,
                "processed",
                tuple(candidate_ids),
                tuple(contribution_ids),
                tuple(agreement_candidate_ids),
                "extraction_and_gate_complete",
            )
        except Exception as exc:
            attempts = int(inbox.get("attempts", 0)) + 1
            status = "dead_letter" if attempts >= int(inbox.get("max_attempts", 5)) else "retry"
            if status == "dead_letter":
                self.transient_evidence.discard(inbox_id)
            self.coordinator.submit(
                lambda: self.store.mark_inbox(inbox_id, status=status, error=f"{type(exc).__name__}:{exc}", retry_seconds=None if status == "dead_letter" else min(3600, 2 ** attempts)),
                priority=1,
                name="relationship_fail_inbox",
            )
            return ProcessingResult(inbox_id, status, reason=f"{type(exc).__name__}:{exc}")

    def _handle_explicit_control(self, *, pair_id: str, user_input: str, turn_id: str, correlation_id: str) -> JsonDict | None:
        normalized = " ".join(user_input.strip().lower().split())
        user_id = str((self.store.pair(pair_id=pair_id) or {}).get("user_id") or "default-user")
        source_ref = f"turn:{turn_id}"
        if any(phrase in normalized for phrase in ("暂停关系学习", "别再分析我们的关系", "pause relationship learning")):
            return self.set_learning_paused(user_id=user_id, paused=True, source_ref=source_ref)
        if any(phrase in normalized for phrase in ("恢复关系学习", "继续理解我们的关系", "resume relationship learning")):
            return self.set_learning_paused(user_id=user_id, paused=False, source_ref=source_ref)
        if any(phrase in normalized for phrase in ("别提我们的关系", "不要引用我们的关系", "keep our relationship implicit")):
            return self.set_surface_permission(user_id=user_id, permission="silent", source_ref=source_ref)
        if any(phrase in normalized for phrase in ("可以明确提我们的关系", "你可以说你记得我们的关系", "explicitly reference our relationship")):
            return self.set_surface_permission(user_id=user_id, permission="explicit", source_ref=source_ref)
        confirmation = re.search(r"(?:确认关系|confirm relationship)\s+([\w-]+)\s+([\w-]+)", normalized, flags=re.I)
        if confirmation:
            return self.coordinator.submit(
                lambda: self.agreement.confirm(pair_id, confirmation.group(1), confirmation.group(2), actor="user"),
                priority=0,
                name="relationship_confirm_agreement",
            )
        if any(phrase in normalized for phrase in ("撤回我们的关系约定", "取消关系约定", "revoke our relationship agreement")):
            return self.coordinator.submit(lambda: self.agreement.revoke(pair_id, actor="user"), priority=0, name="relationship_revoke_agreement")
        name_match = re.search(r"(?:以后)?(?:别|不要)(?:再)?叫我\s*([^，。！？,.!?\s]{1,30})", user_input, flags=re.I)
        if name_match:
            name = name_match.group(1)
            proposed = self.coordinator.submit(
                lambda: self.agreement.propose(pair_id, term_path="terms.disallowed_names", proposed_value=[name], reason_codes=["explicit_user_naming_boundary"], evidence_refs=[source_ref]),
                priority=0,
                name="relationship_propose_name_boundary",
            )
            return self.coordinator.submit(
                lambda: self.agreement.confirm(pair_id, str(proposed["candidate_id"]), str(proposed["confirmation_token"]), actor="user_explicit_control"),
                priority=0,
                name="relationship_confirm_name_boundary",
            )
        mode_map = {
            "我们是恋人": "romantic_partner",
            "我们就是恋人": "romantic_partner",
            "我们是朋友": "friend",
            "我们是家人": "family_style",
            "we are romantic partners": "romantic_partner",
            "we are friends": "friend",
        }
        for phrase, mode in mode_map.items():
            if phrase in normalized:
                return self.coordinator.submit(
                    lambda mode=mode: self.agreement.propose(pair_id, term_path="terms.relationship_mode", proposed_value=mode, reason_codes=["explicit_relationship_mode_proposal"], evidence_refs=[source_ref]),
                    priority=0,
                    name="relationship_propose_mode",
                )
        return None

    def _worker_loop(self) -> None:
        while not self._worker_stop.is_set():
            try:
                result = self.process_pending(limit=10)
                self._processed_since_start += int(result["processed_count"])
                self._last_worker_error = None
            except Exception as exc:
                self._last_worker_error = repr(exc)
                try:
                    self.store.record_audit(pair_id=None, action="relationship_worker_cycle_failed", entity_type="worker", entity_id="relationship-evidence-worker", actor="worker_supervisor", details={"error_type": type(exc).__name__, "error": str(exc)[:500]})
                except Exception:
                    pass
            self._worker_stop.wait(self.worker_poll_seconds)

    @staticmethod
    def _bounded_interpretation(value: Mapping[str, Any]) -> JsonDict:
        source = dict(value or {})
        return {
            "confidence": source.get("confidence"),
            "conversation_move": source.get("conversation_move"),
            "intent": dict(source.get("intent") or {}),
            "domain": dict(source.get("domain") or {}),
            "context_reference": dict(source.get("context_reference") or {}),
        }

    @staticmethod
    def _percentile(values: Sequence[float], quantile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(float(value) for value in values)
        index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * quantile))))
        return round(ordered[index], 3)


class RelationshipAffectAdapter:
    """Maps RelationshipDomainEvent.v1 to AffectStimulusEvent.v2 without a second public contract."""

    def __init__(self, affect_system: Any, relationship_system: RelationshipSystem) -> None:
        self.affect_system = affect_system
        self.relationship_system = relationship_system

    def deliver(self, domain_event: Mapping[str, Any]) -> JsonDict:
        from ..affect.contracts import AffectStimulusEvent

        if domain_event.get("event_type") == "agreement_confirmation_required":
            return {"status": "ignored", "reason": "consent_request_is_not_affect_evidence"}
        pair_id = str(domain_event.get("pair_id") or "")
        pair = self.relationship_system.store.pair(pair_id=pair_id)
        payload = dict(domain_event.get("payload") or {})
        event_kind = {
            "repair_state_changed": "repair_relief" if payload.get("to") == "repaired" else "relationship_rupture",
            "agreement_changed": "relationship_agreement_changed",
            "state_snapshot_published": "relationship_state_updated",
            "forget_applied": "relationship_context_forgotten",
        }.get(str(domain_event.get("event_type")), "relationship_context_changed")
        stimulus = AffectStimulusEvent(
            event_id=f"aff_{domain_event.get('event_id')}",
            sora_id=self.relationship_system.store.sora_id,
            source_type="relationship_domain_event",
            source_ref=str(domain_event.get("source_ref") or domain_event.get("event_id")),
            source_revision=str(domain_event.get("relationship_generation") or domain_event.get("state_snapshot_version") or "0"),
            occurred_at=str(domain_event.get("occurred_at") or utc_now()),
            actor_user_id=str(pair.get("user_id")) if pair else None,
            target_kind="relationship",
            target_ref=pair_id,
            relationship_ref=pair_id,
            conversation_scope="relationship-domain",
            source_confidence=1.0,
            event_kind=event_kind,
            payload={"event_type": domain_event.get("event_type"), "transition": payload},
            evidence_refs=(str(domain_event.get("source_ref") or domain_event.get("event_id")),),
            sensitivity="medium",
            correlation_id=(str(domain_event["correlation_id"]) if domain_event.get("correlation_id") else None),
        )
        result = self.affect_system.ingest_structured_event(stimulus, semantic_enabled=False)
        return {"status": "delivered", "affect_result": result}
