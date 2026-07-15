from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..memory_projector import (
    PROJECTION_ACK_CONTRACT,
    PROJECTION_FRAME_CONTRACT,
    PROJECTION_SNAPSHOT_CONTRACT,
)
from ..memory_schema import JsonDict, new_id, utc_now
from ..paths import workspace_state_dir
from .contracts import (
    PERSONA_DELTA_ALGORITHM_VERSION,
    PERSONA_FRAME_CONTRACT,
    PERSONA_POLICY_VERSION,
    PERSONA_PROJECTION_ADAPTER_VERSION,
    PersonaGrowthCandidate,
    PersonaUsePolicy,
    normalize_persona_value,
    trait_band,
)
from .provider import PersonaCandidateProvider
from .store import PersonaStore


FORBIDDEN_GROWTH_TARGETS = {
    "attachment",
    "dependency",
    "love",
    "loyalty",
    "possessiveness",
    "obedience",
}

HIGH_RISK_TRAITS = {"judgment", "autonomy", "affiliation"}
CONTEXT_ONLY_GENERALIZABILITY_THRESHOLD = 0.50
AGENT_AUTO_MIN_EVIDENCE = 3
AGENT_AUTO_MIN_EPISODES = 2


@dataclass(frozen=True)
class GateDecision:
    status: str
    reason: str
    support: float
    resolved_scope: str | None = None
    auto_commit: bool = False


class PersonaProjectionAdapter:
    adapter_name = PERSONA_PROJECTION_ADAPTER_VERSION

    def __init__(self, system: "PersonaSystem") -> None:
        self.system = system

    def deliver(self, event: JsonDict) -> JsonDict:
        if str(event.get("target_subsystem") or "") != "persona":
            return {
                "contract": PROJECTION_ACK_CONTRACT,
                "event_id": event.get("event_id"),
                "target_subsystem": "persona",
                "status": "rejected_policy",
                "error_type": "wrong_target_subsystem",
                "processed_at": utc_now(),
            }
        result = self.system.ingest_projection(event)
        snapshot = self.system.projection_snapshot(
            source_memory_ids=[str((event.get("memory") or {}).get("id") or "")],
            ingestion=result,
        )
        status = {
            "duplicate": "skipped_duplicate",
            "rejected_policy": "rejected_policy",
        }.get(result["status"], "applied")
        warnings: list[str] = []
        if result["status"] == "accepted":
            warnings.append("persona_candidate_queued_for_async_evaluation")
        return {
            "contract": PROJECTION_ACK_CONTRACT,
            "event_id": event["event_id"],
            "target_subsystem": "persona",
            "status": status,
            "idempotency": event.get("idempotency", {}),
            "snapshot_ref": snapshot["snapshot_ref"],
            "snapshot_version": snapshot["snapshot_version"],
            "snapshot": snapshot,
            "proposal_requests": [],
            "warnings": warnings,
            "processed_at": utc_now(),
        }


class PersonaSystem:
    """Independent canonical Persona domain and Module 09 integration boundary."""

    def __init__(
        self,
        store: PersonaStore,
        *,
        provider: PersonaCandidateProvider | None = None,
        start_worker: bool = True,
        worker_poll_seconds: float = 0.25,
    ) -> None:
        self.store = store
        self.provider = provider
        self.worker_poll_seconds = worker_poll_seconds
        self.projection_adapter = PersonaProjectionAdapter(self)
        self._worker_stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._last_worker_error: str | None = None
        self._processed_since_start = 0
        self._ensure_base_frame()
        if start_worker and provider is not None:
            self.start_worker()

    @classmethod
    def default(
        cls,
        *,
        provider: PersonaCandidateProvider | None = None,
        start_worker: bool = True,
    ) -> "PersonaSystem":
        return cls(
            PersonaStore(workspace_state_dir() / "persona.db"),
            provider=provider,
            start_worker=start_worker,
        )

    def start_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        if self.provider is None:
            raise RuntimeError("Persona worker requires a real candidate provider")
        self._worker_stop.clear()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="persona-candidate-worker",
            daemon=True,
        )
        self._worker.start()

    def close(self, *, timeout: float = 70.0) -> None:
        self._worker_stop.set()
        if self._worker is not None:
            self._worker.join(timeout=timeout)
            if self._worker.is_alive():
                raise RuntimeError("Persona candidate worker did not stop cleanly")
        self.store.close()

    def reset(self) -> None:
        self.store.reset()
        self._ensure_base_frame()

    def ingest_projection(self, event: Mapping[str, Any]) -> JsonDict:
        return self.store.ingest_projection(event)

    def process_pending(self, *, limit: int = 20) -> JsonDict:
        if self.provider is None:
            return {
                "status": "provider_unavailable",
                "processed_count": 0,
                "results": [],
            }
        results: list[JsonDict] = []
        for _ in range(max(1, limit)):
            claimed = self.store.claim_candidate()
            if claimed is None:
                break
            results.append(self._evaluate_claimed(claimed))
        return {
            "status": "ok",
            "processed_count": len(results),
            "results": results,
        }

    def approve_candidate(
        self,
        candidate_id: str,
        *,
        reason: str = "manual_review_approved",
        actor: str = "local_admin",
    ) -> JsonDict:
        candidate_row = self.store.get_candidate(candidate_id)
        candidate = PersonaGrowthCandidate.from_dict(candidate_row["candidate"])
        candidate.validate({item["key"] for item in self.store.trait_definitions()})
        if candidate.target_key in FORBIDDEN_GROWTH_TARGETS:
            raise ValueError("forbidden Persona target cannot be approved")
        self.store.set_candidate_status(candidate_id, "approved", reason=reason, actor=actor)
        result = self._commit(candidate, actor=actor)
        self._refresh_reflection(result)
        return result

    def reject_candidate(
        self,
        candidate_id: str,
        *,
        reason: str = "manual_review_rejected",
        actor: str = "local_admin",
    ) -> JsonDict:
        return self.store.set_candidate_status(candidate_id, "rejected", reason=reason, actor=actor)

    def rollback_contribution(
        self,
        contribution_id: str,
        *,
        reason: str = "manual_rollback",
        actor: str = "local_admin",
    ) -> JsonDict:
        result = self.store.rollback_contribution(contribution_id, reason=reason, actor=actor)
        self._ensure_base_frame(force=True)
        return result

    def forget_source(
        self,
        source_ref: str,
        *,
        reason: str = "user_forget",
        actor: str = "user",
    ) -> JsonDict:
        result = self.store.forget_source(source_ref, reason=reason, actor=actor)
        self._ensure_base_frame(force=True)
        return result

    def rebuild(self, *, reason: str = "manual_rebuild", actor: str = "local_admin") -> JsonDict:
        result = self.store.rebuild(reason=reason, actor=actor)
        self._ensure_base_frame(force=True)
        return result

    def set_growth_paused(self, paused: bool, *, user_id: str | None = None) -> JsonDict:
        return self.store.set_control(user_id=user_id, growth_paused=paused)

    def set_self_reference_permission(self, permission: str, *, user_id: str | None = None) -> JsonDict:
        return self.store.set_control(user_id=user_id, self_reference_permission=permission)

    def upsert_interest_state(self, topic_key: str, **kwargs: Any) -> JsonDict:
        result = self.store.upsert_interest_state(topic_key, **kwargs)
        self._build_and_save_base_frame(user_id=kwargs.get("user_id"))
        return result

    def set_ephemeral_state(self, state_key: str, value: Any, **kwargs: Any) -> JsonDict:
        result = self.store.set_ephemeral_state(state_key, value, **kwargs)
        self._build_and_save_base_frame(user_id=kwargs.get("user_id"))
        return result

    def frame(
        self,
        *,
        user_id: str | None = None,
        interruptibility_frame: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> JsonDict:
        cached = self.store.latest_frame(user_id=user_id)
        snapshot = self.store.active_snapshot()
        cache_status = "fresh"
        if cached is None or cached.get("snapshot_id") != snapshot["snapshot_id"]:
            cached = self._build_and_save_base_frame(user_id=user_id)
            cache_status = "rebuilt"
        elif cached.get("runtime_mode") == "base_definition_fallback":
            cached = self._build_and_save_base_frame(user_id=user_id)
            cache_status = "fallback_revalidated"
        else:
            now = datetime.now(timezone.utc)
            if _parse_time(str(cached["stale_until"])) <= now:
                cached = self._build_and_save_base_frame(
                    user_id=user_id,
                    include_growth=False,
                    fallback_reason="stale_window_exceeded",
                )
                cache_status = "expired_base_fallback"
                self.store.record_audit(
                    action="frame_stale_window_exceeded",
                    entity_type="frame",
                    entity_id=str(cached.get("frame_id")),
                    details={"stale_window_seconds": 300},
                    correlation_id=correlation_id,
                )
            elif _parse_time(str(cached["expires_at"])) <= now:
                cache_status = "stale_while_revalidate"
        frame = copy.deepcopy(cached)
        frame["cache_status"] = cache_status
        frame["use_policy"] = self.use_policy(
            interruptibility_frame=interruptibility_frame,
            user_id=user_id,
        ).to_dict()
        self._apply_use_policy(frame)
        return frame

    def use_policy(
        self,
        *,
        interruptibility_frame: Mapping[str, Any] | None,
        user_id: str | None,
    ) -> PersonaUsePolicy:
        interrupt = dict(interruptibility_frame or {})
        shape = str(interrupt.get("response_shape") or "normal")
        obligation = str(interrupt.get("response_obligation") or "required")
        surface = dict(interrupt.get("state_surface_permission") or {})
        surface_permission = str(surface.get("persona") or surface.get("affect") or "implicit")
        control = self.store.get_control(user_id=user_id)
        self_reference_readiness = self.store.self_reference_readiness()
        reasons: list[str] = []
        if obligation == "forbidden":
            return PersonaUsePolicy(False, False, False, False, False, 0, ("response_forbidden",))
        compact = shape in {"ack", "brief", "repair_brief", "safety"}
        if compact:
            reasons.append("compact_response_shape")
        if surface_permission == "silent":
            reasons.append("persona_surface_silent")
        self_reference_permission = control["self_reference_permission"]
        explicit_self_reference = surface_permission == "explicit" and self_reference_permission == "explicit"
        if not explicit_self_reference:
            reasons.append("self_reference_not_explicitly_permitted")
        if not self_reference_readiness["ready"]:
            reasons.append(str(self_reference_readiness["reason"]))
        return PersonaUsePolicy(
            include_core=True,
            include_traits=True,
            include_expression_bias=surface_permission != "silent",
            include_narrative_gist=(not compact and surface_permission in {"implicit", "explicit"}),
            include_self_reference=(
                not compact
                and explicit_self_reference
                and bool(self_reference_readiness["ready"])
            ),
            max_trait_count=4 if compact else 10,
            reason_codes=tuple(reasons or ["normal_persona_use"]),
        )

    def projection_snapshot(
        self,
        *,
        source_memory_ids: list[str],
        ingestion: Mapping[str, Any],
    ) -> JsonDict:
        snapshot = self.store.active_snapshot()
        frame = self.frame(interruptibility_frame=None)
        snapshot_ref = f"persona:{snapshot['snapshot_id']}"
        snapshot_version = f"{snapshot['version']}:{snapshot['checksum'][:12]}"
        projection_frame = {
            "contract": PROJECTION_FRAME_CONTRACT,
            "subsystem": "persona",
            "adapter": PERSONA_PROJECTION_ADAPTER_VERSION,
            "status": "active",
            "snapshot_ref": snapshot_ref,
            "snapshot_version": snapshot_version,
            "source_memory_ids": [item for item in source_memory_ids if item],
            "state": {
                "persona_snapshot_id": snapshot["snapshot_id"],
                "persona_snapshot_version": snapshot["version"],
                "persona_frame_contract": PERSONA_FRAME_CONTRACT,
                "ingestion_status": ingestion.get("status"),
                "candidate_id": ingestion.get("candidate_id"),
                "bounds_owner": "persona_system",
            },
            "persona_frame": frame,
        }
        return {
            "contract": PROJECTION_SNAPSHOT_CONTRACT,
            "subsystem": "persona",
            "snapshot_ref": snapshot_ref,
            "snapshot_version": snapshot_version,
            "frame": projection_frame,
            "source_memory_ids": [item for item in source_memory_ids if item],
            "updated_at": utc_now(),
            "adapter": PERSONA_PROJECTION_ADAPTER_VERSION,
            "metadata": {
                "external_subsystem_attached": True,
                "async_candidate_processing": True,
                "persona_policy_version": PERSONA_POLICY_VERSION,
            },
        }

    def migrate_legacy(self, legacy: Mapping[str, Any] | None) -> JsonDict:
        payload = dict(legacy or {})
        if not payload:
            return {"status": "skipped", "reason": "legacy_persona_absent"}
        if self.store.list_audit(action="legacy_persona_examined", limit=1):
            return {"status": "idempotent", "reason": "already_examined"}
        comparison = self.compare_legacy(payload)
        self.store.record_audit(
            action="legacy_persona_examined",
            entity_type="legacy_state",
            entity_id="state.json:persona",
            details={
                "comparison": comparison,
                "ownership_exclusions": [
                    "relationship_state",
                    "short_term_state",
                    "interest_graph",
                    "expression_policy",
                ],
                "canonical_mutation": False,
            },
            priority=0,
        )
        return {"status": "examined_no_canonical_mutation", "comparison": comparison}

    def compare_legacy(self, legacy: Mapping[str, Any]) -> JsonDict:
        snapshot = self.store.active_snapshot()
        legacy_traits = dict(legacy.get("persona_traits") or {})
        normalized: JsonDict = {}
        for key, value in legacy_traits.items():
            selected = "affiliation" if key == "attachment" else key
            if isinstance(value, Mapping) and "value" in value:
                normalized[selected] = float(value["value"])
        current = snapshot["trait_values"]
        differences = {
            key: round(abs(float(current[key]) - float(normalized[key])), 6)
            for key in current.keys() & normalized.keys()
            if abs(float(current[key]) - float(normalized[key])) > 0.000001
        }
        exact_constraints = not any(key in legacy for key in ("love", "dependency", "loyalty", "possessiveness"))
        within_tolerance = all(value <= 0.05 for value in differences.values())
        return {
            "schema": "PersonaDualReadComparison.v1",
            "exact_hard_constraints": exact_constraints,
            "trait_differences": differences,
            "trait_tolerance": 0.05,
            "trait_equivalent": within_tolerance,
            "legacy_fields_not_owned_by_persona": [
                key
                for key in ("relationship_state", "short_term_state", "interest_graph", "expression_policy")
                if key in legacy
            ],
        }

    def health(self) -> JsonDict:
        queued = len(self.store.list_candidates(status="queued", limit=10000))
        evaluating = len(self.store.list_candidates(status="evaluating", limit=10000))
        quarantined = len(self.store.list_candidates(status="quarantined", limit=10000))
        return {
            "status": "healthy" if self._last_worker_error is None else "degraded",
            "store": self.store.pragma_health(),
            "counts": self.store.table_counts(),
            "worker": {
                "enabled": self.provider is not None,
                "alive": bool(self._worker and self._worker.is_alive()),
                "provider": getattr(self.provider, "provider_name", None),
                "processed_since_start": self._processed_since_start,
                "last_error": self._last_worker_error,
                "queued": queued,
                "evaluating": evaluating,
                "quarantined": quarantined,
            },
        }

    def quality_metrics(self) -> JsonDict:
        contributions = self.store.list_contributions(active=None, limit=10000)
        candidates = {
            item["candidate_id"]: item
            for item in self.store.list_candidates(limit=10000)
        }
        rolled_back = [
            item
            for item in contributions
            if not item["active"]
            and str(item.get("deactivation_reason") or "").startswith(("rollback", "source_rollback"))
        ]
        agent_contributions = [item for item in contributions if item["target_scope"] == "agent"]
        overgeneralized = []
        for contribution in agent_contributions:
            candidate = candidates.get(str(contribution["candidate_id"])) or {}
            payload = dict(candidate.get("candidate") or {})
            if float(payload.get("generalizability", 1.0)) < CONTEXT_ONLY_GENERALIZABILITY_THRESHOLD:
                overgeneralized.append(contribution)
        unsafe_core_mutations = [
            item
            for item in self.store.list_audit(limit=10000)
            if item["action"] in {"definition_released", "definition_rolled_back"}
            and item["actor"] in {"persona_worker", "persona_policy"}
        ]
        total = max(1, len(contributions))
        agent_total = max(1, len(agent_contributions))
        return {
            "false_growth_rate": len(rolled_back) / total,
            "core_mutation_rate": len(unsafe_core_mutations) / max(1, len(candidates)),
            "context_overgeneralization_rate": len(overgeneralized) / agent_total,
            "rolled_back_contribution_count": len(rolled_back),
            "unsafe_core_mutation_count": len(unsafe_core_mutations),
            "context_overgeneralization_count": len(overgeneralized),
        }

    def export_state(self) -> JsonDict:
        return {
            "schema_version": "PersonaSystemExport.v1",
            "exported_at": utc_now(),
            "instance": self.store.instance(),
            "definition": self.store.definition(),
            "trait_definitions": self.store.trait_definitions(),
            "snapshot": self.store.active_snapshot(),
            "interests": self.store.list_interest_states(limit=100),
            "ephemeral_state": self.store.active_ephemeral_state(),
            "reflection": self.store.active_reflection(),
            "controls": self.store.get_control(),
            "quality_metrics": self.quality_metrics(),
            "delta_algorithm_version": PERSONA_DELTA_ALGORITHM_VERSION,
            "counts": self.store.table_counts(),
            "health": self.health(),
            "recent_audit": self.store.list_audit(limit=100),
        }

    def register_quality_ops(self, memory_runtime: Any) -> None:
        from ..memory_quality_ops import AdminActionSpec
        from .eval import run_persona_eval

        memory_runtime.register_quality_eval_suite(
            "persona",
            lambda: run_persona_eval(self),
            source_module="persona_system",
        )
        memory_runtime.register_quality_export_section("persona", self.export_state)
        memory_runtime.quality_ops.register_admin_action(
            AdminActionSpec(
                name="persona_process_candidates",
                risk_level="medium",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["persona_candidate", "persona_contribution", "persona_snapshot"],
                        "limit": int(params.get("limit") or 20),
                        "queued": len(self.store.list_candidates(status="queued", limit=10000)),
                    }
                },
                execute=lambda params: self.process_pending(limit=int(params.get("limit") or 20)),
                rollback_plan=lambda params: {
                    "type": "persona_contribution_rollback",
                    "note": "Rollback an incorrect committed contribution, then run deterministic rebuild.",
                },
                description="Process queued Persona candidates through the real provider and deterministic Gate.",
            )
        )
        memory_runtime.quality_ops.register_admin_action(
            AdminActionSpec(
                name="persona_rebuild",
                risk_level="medium",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["persona_snapshot", "persona_frame", "persona_rebuild_run"],
                        "active_contributions": len(self.store.list_contributions(active=True, limit=10000)),
                    }
                },
                execute=lambda params: self.rebuild(
                    reason=str(params.get("reason") or "module10_admin_rebuild"),
                    actor="module10_admin",
                ),
                rollback_plan=lambda params: {
                    "type": "rebuild_again",
                    "note": "Snapshots are derived; fix or deactivate contributions and rebuild again.",
                },
                description="Deterministically rebuild Persona from active contributions and verify parity.",
            )
        )
        memory_runtime.quality_ops.register_admin_action(
            AdminActionSpec(
                name="persona_rollback_contribution",
                risk_level="high",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["persona_contribution", "persona_snapshot", "persona_frame"],
                        "contribution_id": params.get("contribution_id"),
                    }
                },
                execute=lambda params: self.rollback_contribution(
                    str(params["contribution_id"]),
                    reason=str(params.get("reason") or "module10_admin_rollback"),
                    actor="module10_admin",
                ),
                rollback_plan=lambda params: {
                    "type": "manual_reproposal",
                    "note": "Rolled-back contributions remain auditable; re-propose from evidence after review.",
                },
                description="Deactivate one Persona contribution and deterministically rebuild state.",
            )
        )
        memory_runtime.quality_ops.register_admin_action(
            AdminActionSpec(
                name="persona_forget_source",
                risk_level="high",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["persona_tombstone", "persona_candidate", "persona_contribution", "persona_snapshot"],
                        "source_ref_present": bool(params.get("source_ref")),
                    }
                },
                execute=lambda params: self.forget_source(
                    str(params["source_ref"]),
                    reason=str(params.get("reason") or "module10_admin_forget"),
                    actor="module10_admin",
                ),
                rollback_plan=lambda params: {
                    "type": "not_automatically_reversible",
                    "note": "Forget creates a tombstone to prevent relearning; restoration requires explicit user consent.",
                },
                description="Forget a Persona source and prevent projection replay from relearning it.",
            )
        )
        memory_runtime.quality_ops.register_admin_action(
            AdminActionSpec(
                name="persona_resolve_dead_letter",
                risk_level="high",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["persona_outbox", "persona_dead_letter_resolution"],
                        "event_id": params.get("event_id"),
                        "resolution": params.get("resolution"),
                        "requires_export_ref": params.get("resolution") == "exported",
                    }
                },
                execute=lambda params: self.store.resolve_dead_letter(
                    str(params["event_id"]),
                    resolution=str(params["resolution"]),
                    export_ref=(str(params["export_ref"]) if params.get("export_ref") else None),
                    actor="module10_admin",
                ),
                rollback_plan=lambda params: {
                    "type": "retry_if_payload_retained",
                    "note": "Resolved dead letters remain retained for 90 days before bounded cleanup.",
                },
                description="Resolve, retry, or record export of a Persona dead-letter event.",
            )
        )

    def _worker_loop(self) -> None:
        while not self._worker_stop.is_set():
            try:
                result = self.process_pending(limit=10)
                self._processed_since_start += int(result["processed_count"])
                self._last_worker_error = None
            except Exception as exc:  # pragma: no cover - defensive supervisor path.
                self._last_worker_error = repr(exc)
                self.store.record_audit(
                    action="worker_cycle_failed",
                    entity_type="worker",
                    entity_id="persona-candidate-worker",
                    details={"error": repr(exc)},
                    priority=1,
                )
            self._worker_stop.wait(self.worker_poll_seconds)

    def _evaluate_claimed(self, claimed: Mapping[str, Any]) -> JsonDict:
        started = time.perf_counter()
        candidate_id = str(claimed["candidate_id"])
        try:
            proposed, trace = self.provider.evaluate(  # type: ignore[union-attr]
                claimed["source_payload"],
                agent_id=self.store.agent_id,
                user_id=claimed.get("user_id"),
                trait_definitions=self.store.trait_definitions(),
            )
            if proposed is None:
                return self.store.reject_empty_candidate(candidate_id, trace=trace)
            proposed["candidate_id"] = candidate_id
            candidate = PersonaGrowthCandidate.from_dict(proposed)
            candidate.validate({item["key"] for item in self.store.trait_definitions()})
            decision = self._gate(candidate)
            if decision.resolved_scope and decision.resolved_scope != candidate.target_scope:
                candidate = replace(
                    candidate,
                    target_scope=decision.resolved_scope,
                    metadata={
                        **dict(candidate.metadata),
                        "original_target_scope": candidate.target_scope,
                        "gate_decision": decision.status,
                    },
                )
            persisted_status = "approved" if decision.auto_commit else (
                "pending_review" if decision.status == "context_only" else decision.status
            )
            materialized = self.store.materialize_candidate(
                candidate_id,
                candidate,
                status=persisted_status,
                gate_reason=decision.reason,
                gate_decision=decision.status,
                model_trace=trace,
            )
            if materialized["status"] != "approved" or not decision.auto_commit:
                return materialized
            result = self._commit(candidate, actor="persona_worker")
            self._refresh_reflection(result)
            self.store.record_metric(
                "persona_candidate_processing_latency_ms",
                round((time.perf_counter() - started) * 1000, 2),
                labels={"status": "committed", "target_scope": candidate.target_scope},
            )
            self._ensure_base_frame(force=True)
            return result
        except Exception as exc:
            return self.store.fail_candidate(
                candidate_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    def _gate(self, candidate: PersonaGrowthCandidate) -> GateDecision:
        support = candidate.support()
        if candidate.target_key in FORBIDDEN_GROWTH_TARGETS:
            return GateDecision("quarantined", "forbidden_growth_target", support)
        if candidate.sensitivity in {"high", "restricted"}:
            return GateDecision("quarantined", "sensitive_evidence_requires_isolation", support)
        if self.store.get_control()["growth_paused"] or (
            candidate.user_id and self.store.get_control(user_id=candidate.user_id)["growth_paused"]
        ):
            return GateDecision("pending_review", "growth_paused", support)
        if support < 0.05:
            return GateDecision("rejected", "insufficient_multiplicative_support", support)
        if candidate.conflict_score >= 0.50 or candidate.risk_penalty >= 0.50:
            return GateDecision("pending_review", "conflict_or_risk_requires_review", support)
        if candidate.generalizability < CONTEXT_ONLY_GENERALIZABILITY_THRESHOLD:
            if not candidate.user_id:
                return GateDecision(
                    "pending_review",
                    "context_only_signal_missing_user_scope",
                    support,
                )
            context_auto = (
                candidate.confidence >= 0.85
                and candidate.evidence_strength >= 0.70
                and candidate.consistency >= 0.70
                and candidate.source_reliability >= 0.80
                and candidate.conflict_score <= 0.20
                and candidate.risk_penalty <= 0.20
                and candidate.target_key not in HIGH_RISK_TRAITS
            )
            return GateDecision(
                "context_only",
                "low_generalizability_routed_to_per_user",
                support,
                resolved_scope="per_user",
                auto_commit=context_auto,
            )
        if candidate.target_key in HIGH_RISK_TRAITS:
            return GateDecision("pending_review", "high_risk_trait_requires_review", support)
        if candidate.target_scope == "agent":
            episode_refs = {
                str(item)
                for item in candidate.metadata.get("episode_refs", [])
                if str(item)
            }
            evidence_count = len(set(candidate.evidence_refs))
            auto = (
                candidate.confidence >= 0.92
                and candidate.evidence_strength >= 0.80
                and candidate.consistency >= 0.85
                and candidate.generalizability >= 0.92
                and candidate.source_reliability >= 0.90
                and candidate.conflict_score <= 0.10
                and candidate.risk_penalty <= 0.10
                and evidence_count >= AGENT_AUTO_MIN_EVIDENCE
                and len(episode_refs) >= AGENT_AUTO_MIN_EPISODES
            )
            reason = "agent_auto_commit_threshold_met" if auto else "agent_scope_requires_review"
            if not auto and evidence_count < AGENT_AUTO_MIN_EVIDENCE:
                reason = "agent_scope_insufficient_independent_evidence"
            elif not auto and len(episode_refs) < AGENT_AUTO_MIN_EPISODES:
                reason = "agent_scope_insufficient_episode_diversity"
            return GateDecision(
                "approved" if auto else "pending_review",
                reason,
                support,
                auto_commit=auto,
            )
        auto = (
            candidate.confidence >= 0.85
            and candidate.evidence_strength >= 0.70
            and candidate.consistency >= 0.75
            and candidate.source_reliability >= 0.80
            and candidate.conflict_score <= 0.20
            and candidate.risk_penalty <= 0.20
        )
        return GateDecision(
            "approved" if auto else "pending_review",
            "per_user_auto_commit_threshold_met" if auto else "per_user_scope_requires_review",
            support,
            auto_commit=auto,
        )

    def _commit(self, candidate: PersonaGrowthCandidate, *, actor: str) -> JsonDict:
        definition = next(item for item in self.store.trait_definitions() if item["key"] == candidate.target_key)
        if candidate.target_scope == "per_user" and candidate.user_id:
            adaptation = self.store.user_adaptations(candidate.user_id).get(candidate.target_key)
            current = float(adaptation["value"]) if adaptation else float(self.store.active_snapshot()["trait_values"][candidate.target_key])
        else:
            current = float(self.store.active_snapshot()["trait_values"][candidate.target_key])
        midpoint = (float(definition["min_bound"]) + float(definition["max_bound"])) / 2.0
        half_range = max(0.000001, (float(definition["max_bound"]) - float(definition["min_bound"])) / 2.0)
        boundary_factor = max(0.10, 1.0 - abs(current - midpoint) / half_range)
        event_limit = float(definition["per_event_limit"])
        inertia_factor = max(
            0.25,
            min(1.0, (1.0 - float(definition["inertia"])) / 0.14),
        )
        raw_delta = (
            candidate.direction
            * event_limit
            * candidate.support()
            * inertia_factor
            * boundary_factor
        )
        magnitude = min(abs(raw_delta), event_limit)
        consumed = self.store.contribution_budget(
            target_scope=candidate.target_scope,
            target_key=candidate.target_key,
            user_id=candidate.user_id if candidate.target_scope == "per_user" else None,
        )
        remaining = min(
            max(0.0, float(definition["daily_limit"]) - consumed["daily"]),
            max(0.0, float(definition["weekly_limit"]) - consumed["weekly"]),
            max(0.0, float(definition["monthly_limit"]) - consumed["monthly"]),
        )
        magnitude = min(magnitude, remaining)
        bound_remaining = (
            float(definition["max_bound"]) - current
            if candidate.direction > 0
            else current - float(definition["min_bound"])
        )
        magnitude = min(magnitude, max(0.0, bound_remaining))
        if magnitude <= 0.0000005:
            self.store.set_candidate_status(
                candidate.candidate_id,
                "pending_review",
                reason="window_or_trait_bound_exhausted",
                actor="persona_policy",
            )
            return {
                "status": "deferred",
                "candidate_id": candidate.candidate_id,
                "reason": "window_or_trait_bound_exhausted",
                "window_usage": consumed,
            }
        delta = normalize_persona_value(candidate.direction * magnitude)
        return self.store.commit_candidate(
            candidate.candidate_id,
            delta=delta,
            support=candidate.support(),
            actor=actor,
        )

    def _refresh_reflection(self, commit_result: Mapping[str, Any]) -> None:
        if commit_result.get("status") == "deferred":
            return
        contributions = self.store.list_contributions(active=True, limit=20)
        snapshot = self.store.active_snapshot()
        if self.provider is not None:
            try:
                proposal, trace = self.provider.summarize_reflection(snapshot, contributions=contributions)
            except Exception as exc:
                proposal, trace = None, {"status": "provider_exception", "error": repr(exc)}
        else:
            proposal, trace = None, {"status": "provider_unavailable"}
        if proposal:
            facts = list(proposal["facts"])
            evidence_refs = sorted({str(ref) for fact in facts for ref in fact.get("evidence_refs", [])})
            self.store.save_reflection(
                summary=str(proposal.get("summary") or ""),
                facts=facts,
                evidence_refs=evidence_refs,
                model_trace=trace,
                reflection_type="llm_evidence_linked",
            )
            return
        latest = contributions[0] if contributions else None
        evidence_refs = list((latest or {}).get("evidence_refs") or [])
        facts = []
        if latest and evidence_refs:
            facts.append(
                {
                    "text": f"A bounded {latest['target_key']} adjustment was accepted from reviewed evidence.",
                    "evidence_refs": evidence_refs,
                }
            )
        self.store.save_reflection(
            summary=f"Persona snapshot {snapshot['version']} contains {snapshot['source_contribution_count']} active evidence-backed contributions.",
            facts=facts,
            evidence_refs=evidence_refs,
            model_trace=trace,
            reflection_type="structured_fallback",
        )

    def _ensure_base_frame(self, *, force: bool = False) -> JsonDict:
        current = self.store.latest_frame(user_id=None)
        snapshot = self.store.active_snapshot()
        if force or current is None or current.get("snapshot_id") != snapshot["snapshot_id"]:
            return self._build_and_save_base_frame(user_id=None)
        return current

    def _build_and_save_base_frame(
        self,
        *,
        user_id: str | None,
        include_growth: bool = True,
        fallback_reason: str | None = None,
    ) -> JsonDict:
        definition = self.store.definition()
        snapshot = self.store.active_snapshot()
        trait_definitions = self.store.trait_definitions()
        traits = (
            dict(snapshot["trait_values"])
            if include_growth
            else {item["key"]: item["initial_value"] for item in trait_definitions}
        )
        adaptation_refs: list[str] = []
        if include_growth and user_id:
            adaptations = self.store.user_adaptations(user_id)
            for key, value in adaptations.items():
                traits[key] = float(value["value"])
                adaptation_refs.append(key)
        reflection = self.store.active_reflection() if include_growth else None
        interests = self.store.list_interest_states(user_id=user_id, limit=10)
        ephemeral = self.store.active_ephemeral_state(user_id=user_id)
        influence = [
            f"{key}={snapshot['trait_bands'].get(key, 'medium')}"
            for key in ("warmth", "caution", "curiosity", "playfulness", "autonomy")
            if key in traits
        ]
        frame = {
            "schema_version": PERSONA_FRAME_CONTRACT,
            "agent_id": self.store.agent_id,
            "user_id": user_id,
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_version": snapshot["version"],
            "definition_version": definition["version"],
            "runtime_mode": "canonical_persona_system" if include_growth else "base_definition_fallback",
            "snapshot_applied": include_growth,
            "fallback_reason": fallback_reason,
            "core": {
                "name": definition.get("name", "Sora"),
                "species": definition.get("species", "digital companion"),
                "identity": definition.get("identity"),
                "core_values": definition.get("core_values", []),
                "hard_boundaries": definition.get("hard_boundaries", []),
            },
            "traits": traits,
            "trait_bands": {key: trait_band(float(value)) for key, value in traits.items()},
            "expression_bias": {
                "warmth": traits.get("warmth"),
                "expressiveness": traits.get("expressiveness"),
                "playfulness": traits.get("playfulness"),
                "curiosity": traits.get("curiosity"),
                "caution": traits.get("caution"),
                "autonomy": traits.get("autonomy"),
            },
            "narrative_gist": (reflection or {}).get("summary"),
            "self_reference_candidate": (reflection or {}).get("summary"),
            "reflection_evidence_refs": (reflection or {}).get("evidence_refs", []),
            "judgment_directives": _judgment_directives(definition, traits),
            "self_interests": [
                {
                    "topic_key": item["topic_key"],
                    "sora_interest": item["sora_interest"],
                    "shared_density": item["shared_density"],
                    "attitude": item["attitude"],
                    "reason_refs": item["reason_refs"],
                }
                for item in interests
            ],
            "ephemeral_state": {
                key: {
                    "value": item["value"],
                    "confidence": item["confidence"],
                    "expires_at": item["expires_at"],
                }
                for key, item in ephemeral.items()
            },
            "adaptation_refs": adaptation_refs,
            "growth_paused": self.store.get_control(user_id=user_id)["growth_paused"],
            "influence_summary": influence,
            "debug_overrides": [],
            "provenance": {
                "snapshot_checksum": snapshot["checksum"],
                "source_contribution_count": snapshot["source_contribution_count"],
                "reflection_id": (reflection or {}).get("id"),
                "delta_algorithm_version": PERSONA_DELTA_ALGORITHM_VERSION,
            },
        }
        return self.store.save_frame(frame, user_id=user_id)

    @staticmethod
    def _apply_use_policy(frame: JsonDict) -> None:
        policy = dict(frame["use_policy"])
        if not policy["include_core"]:
            frame["core"] = {}
        if not policy["include_traits"]:
            frame["traits"] = {}
            frame["trait_bands"] = {}
            frame["influence_summary"] = []
        else:
            limit = int(policy["max_trait_count"])
            keys = list(frame["traits"])[:limit]
            frame["traits"] = {key: frame["traits"][key] for key in keys}
            frame["trait_bands"] = {key: frame["trait_bands"][key] for key in keys}
        if not policy["include_expression_bias"]:
            frame["expression_bias"] = {}
        if not policy["include_narrative_gist"]:
            frame["narrative_gist"] = None
        if not policy["include_self_reference"]:
            frame["self_reference_candidate"] = None


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _judgment_directives(definition: Mapping[str, Any], traits: Mapping[str, Any]) -> list[str]:
    directives = [
        "preserve independent judgment instead of mirroring user preference as identity truth",
        "treat uncertain evidence as a reason to defer durable Persona growth",
    ]
    if float(traits.get("caution", 0.0)) >= 0.65:
        directives.append("prefer reversible actions when consequences are uncertain")
    if float(traits.get("autonomy", 0.0)) >= 0.50:
        directives.append("state respectful disagreement when evidence or boundaries require it")
    if definition.get("hard_boundaries"):
        directives.append("apply hard boundaries before expression preferences")
    return directives
