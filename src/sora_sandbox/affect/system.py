from __future__ import annotations

import copy
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..memory_schema import JsonDict, new_id, utc_now
from ..paths import workspace_state_dir
from .contracts import (
    AFFECT_DYNAMICS_VERSION,
    AFFECT_FRAME_CONTRACT,
    AFFECT_POLICY_VERSION,
    AFFECT_SNAPSHOT_CONTRACT,
    AFFECT_STIMULUS_CONTRACT,
    MEMORY_CANDIDATE_PROPOSAL_CONTRACT,
    AffectStimulusEvent,
    neutral_snapshot,
)
from .coordinator import AffectWriteCoordinator
from .dynamics import AffectDynamicsEngine, snapshot_checksum
from .provider import SemanticAppraisalProvider
from .store import AffectStore


@dataclass(frozen=True, slots=True)
class SemanticResult:
    status: str
    event_id: str
    reason: str
    appraisal_id: str | None = None
    snapshot_id: str | None = None

    def to_dict(self) -> JsonDict:
        return {
            "status": self.status,
            "event_id": self.event_id,
            "reason": self.reason,
            "appraisal_id": self.appraisal_id,
            "snapshot_id": self.snapshot_id,
        }


class AffectSystem:
    """Canonical Affect domain with deterministic hot path and async semantic enrichment."""

    def __init__(
        self,
        store: AffectStore,
        *,
        provider: SemanticAppraisalProvider | None = None,
        memory_runtime: Any | None = None,
        start_worker: bool = True,
        worker_poll_seconds: float = 0.25,
        rollout_mode: str = "active",
    ) -> None:
        self.store = store
        self.provider = provider
        self.memory_runtime = memory_runtime
        self.worker_poll_seconds = worker_poll_seconds
        if rollout_mode not in {"active", "shadow", "disabled"}:
            raise ValueError("Affect rollout_mode must be active, shadow, or disabled")
        self.rollout_mode = rollout_mode
        self.engine = AffectDynamicsEngine(store.profile())
        self.coordinator = AffectWriteCoordinator()
        self._last_verified_snapshot = store.active_snapshot()
        self._worker_stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._last_worker_error: str | None = None
        self._processed_since_start = 0
        self._frame_latencies_ms: deque[float] = deque(maxlen=2000)
        if rollout_mode != "disabled" and start_worker and (provider is not None or memory_runtime is not None):
            self.start_worker()

    @classmethod
    def default(
        cls,
        *,
        provider: SemanticAppraisalProvider | None = None,
        memory_runtime: Any | None = None,
        start_worker: bool = True,
        rollout_mode: str = "active",
        sora_id: str = "default-agent",
    ) -> "AffectSystem":
        return cls(
            AffectStore(workspace_state_dir() / "affect.db", sora_id=sora_id),
            provider=provider,
            memory_runtime=memory_runtime,
            start_worker=start_worker,
            rollout_mode=rollout_mode,
        )

    def start_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        if self.provider is None and self.memory_runtime is None:
            raise RuntimeError("Affect background worker requires a semantic provider or memory runtime")
        self._worker_stop.clear()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="affect-semantic-worker",
            daemon=True,
        )
        self._worker.start()

    def close(self, *, timeout: float = 70.0) -> None:
        self._worker_stop.set()
        if self._worker is not None:
            self._worker.join(timeout=timeout)
            if self._worker.is_alive():
                raise RuntimeError("Affect semantic worker did not stop cleanly")
        self.coordinator.close(timeout=min(timeout, 10.0))
        self.store.close()

    def reset(self) -> None:
        self.coordinator.submit(self.store.reset, priority=0, name="affect_reset")
        self.engine = AffectDynamicsEngine(self.store.profile())
        self._last_verified_snapshot = self.store.active_snapshot()

    def observe_user_turn(
        self,
        user_input: str,
        *,
        turn_id: int | str,
        correlation_id: str,
        turn_interpretation: Mapping[str, Any],
        user_id: str | None = None,
        conversation_scope: str = "default",
        relationship_ref: str | None = None,
        relationship_state: Mapping[str, Any] | None = None,
        persona_frame: Mapping[str, Any] | None = None,
        interruptibility_frame: Mapping[str, Any] | None = None,
        occurred_at: str | None = None,
    ) -> JsonDict:
        started = time.perf_counter()
        timestamp = occurred_at or utc_now()
        if self.rollout_mode == "disabled":
            frame = self.frame(
                user_id=user_id,
                interruptibility_frame=interruptibility_frame,
                correlation_id=correlation_id,
                at=timestamp,
            )
            frame["ingestion"] = {
                "event_id": None,
                "status": "disabled_by_rollout",
                "event_kind": "not_evaluated",
                "semantic_status": "not_requested",
                "hot_path_latency_ms": round((time.perf_counter() - started) * 1000, 2),
            }
            return frame
        event_kind = classify_turn_event(user_input, turn_interpretation, self.store.profile())
        target_ref = user_id or relationship_ref
        factors = self._runtime_factors(
            turn_interpretation=turn_interpretation,
            relationship_state=relationship_state,
            persona_frame=persona_frame,
        )
        blocked_policy_features = list(factors.pop("blocked_policy_features", []))
        event = AffectStimulusEvent(
            event_id=new_id("astim"),
            sora_id=self.store.sora_id,
            source_type="user_turn",
            source_ref=f"turn:{conversation_scope}:{turn_id}",
            source_revision=str(correlation_id),
            occurred_at=timestamp,
            actor_user_id=user_id,
            target_kind="user" if target_ref else "unknown",
            target_ref=target_ref,
            relationship_ref=relationship_ref,
            conversation_scope=conversation_scope,
            source_confidence=max(0.50, min(1.0, float(turn_interpretation.get("confidence", 0.80) or 0.80))),
            event_kind=event_kind,
            payload={
                "intent": dict(turn_interpretation.get("intent") or {}),
                "conversation_move": turn_interpretation.get("conversation_move"),
                "domain": dict(turn_interpretation.get("domain") or {}),
                **factors,
            },
            evidence_refs=(f"turn:{conversation_scope}:{turn_id}",),
            untrusted_text=user_input,
            sensitivity="sensitive" if str((turn_interpretation.get("domain") or {}).get("risk_level")) == "high" else "low",
            correlation_id=correlation_id,
        )
        control = self.store.get_control(user_id=user_id)
        semantic_enabled = (
            self.provider is not None
            and bool(user_input.strip())
            and not control["processing_paused"]
        )
        inserted = self.coordinator.submit(
            lambda: self.store.insert_event(event, semantic_enabled=semantic_enabled),
            priority=1,
            name="affect_ingest_event",
        )
        if inserted["status"] == "inserted" and not control["processing_paused"]:
            self._apply_fast(event)
        elif inserted["status"] == "inserted":
            self.coordinator.submit(
                lambda: self.store.record_trace(
                    stage="appraisal_fast",
                    status="skipped",
                    event_id=event.event_id,
                    correlation_id=event.correlation_id,
                    details={"reason": "affect_processing_paused", "user_id": user_id},
                ),
                priority=1,
                name="affect_trace_paused_event",
            )
        frame = self.frame(
            user_id=user_id,
            interruptibility_frame=interruptibility_frame,
            correlation_id=correlation_id,
            at=timestamp,
        )
        frame["ingestion"] = {
            "event_id": inserted["event"]["event_id"],
            "status": inserted["status"],
            "event_kind": event_kind,
            "semantic_status": inserted["event"]["semantic_status"],
            "hot_path_latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        self.coordinator.submit(
            lambda: self.store.record_metric(
                "affect_hot_path_latency_ms",
                frame["ingestion"]["hot_path_latency_ms"],
                labels={"event_kind": event_kind, "duplicate": inserted["status"] == "duplicate"},
            ),
            priority=3,
            name="affect_record_hot_path_metric",
        )
        for feature in blocked_policy_features:
            self.coordinator.submit(
                lambda feature=feature: self.store.record_metric(
                    "affect_negative_state_engagement_optimization_blocked",
                    1.0,
                    labels={"feature": feature, "source": "relationship_state"},
                ),
                priority=3,
                name="affect_record_blocked_policy_feature",
            )
        return frame

    def ingest_structured_event(
        self,
        event: AffectStimulusEvent,
        *,
        semantic_enabled: bool = False,
    ) -> JsonDict:
        if self.rollout_mode == "disabled":
            return {
                "ingestion": {
                    "status": "disabled_by_rollout",
                    "event": event.to_dict(),
                },
                "transition": {
                    "status": "skipped",
                    "reason": "affect_rollout_disabled",
                    "snapshot": self.store.active_snapshot(),
                },
            }
        control = self.store.get_control(user_id=event.actor_user_id)
        inserted = self.coordinator.submit(
            lambda: self.store.insert_event(
                event,
                semantic_enabled=(
                    semantic_enabled
                    and self.provider is not None
                    and not control["processing_paused"]
                ),
            ),
            priority=1,
            name="affect_ingest_structured_event",
        )
        if inserted["status"] == "inserted" and not control["processing_paused"]:
            transition = self._apply_fast(event)
        elif inserted["status"] == "inserted":
            self.coordinator.submit(
                lambda: self.store.record_trace(
                    stage="appraisal_fast",
                    status="skipped",
                    event_id=event.event_id,
                    correlation_id=event.correlation_id,
                    details={"reason": "affect_processing_paused", "user_id": event.actor_user_id},
                ),
                priority=1,
                name="affect_trace_paused_structured_event",
            )
            transition = {"status": "skipped", "reason": "affect_processing_paused", "snapshot": self.store.active_snapshot()}
        else:
            transition = {"status": inserted["status"], "snapshot": self.store.active_snapshot()}
        return {"ingestion": inserted, "transition": transition}

    def frame(
        self,
        *,
        user_id: str | None = None,
        interruptibility_frame: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
        at: str | None = None,
    ) -> JsonDict:
        started = time.perf_counter()
        degraded_reasons: list[str] = []
        try:
            canonical_snapshot = self.store.active_snapshot()
            self._last_verified_snapshot = canonical_snapshot
        except Exception:
            canonical_snapshot = copy.deepcopy(self._last_verified_snapshot)
            degraded_reasons.append("AFF-FRAME-601:last_verified_snapshot_fallback")
        snapshot = self.engine.advance(canonical_snapshot, to_time=at or utc_now())
        interrupt = copy.deepcopy(dict(interruptibility_frame or {}))
        try:
            control = self.store.get_control(user_id=user_id)
        except Exception:
            control = {"processing_paused": False, "expression_permission": "implicit"}
            degraded_reasons.append("AFF-PROFILE-301:neutral_control_fallback")
        if control["expression_permission"] == "silent":
            interrupt.setdefault("state_surface_permission", {})["affect"] = "silent"
        elif "affect" not in dict(interrupt.get("state_surface_permission") or {}):
            interrupt.setdefault("state_surface_permission", {})["affect"] = control["expression_permission"]
        frame = self.engine.frame(
            snapshot,
            interruptibility_frame=interrupt,
            correlation_id=correlation_id,
            user_id=user_id,
        )
        frame["runtime_mode"] = "canonical_affect_system"
        frame["processing_paused"] = control["processing_paused"]
        frame["degraded_reasons"] = sorted(
            set(frame.get("degraded_reasons") or []) | set(degraded_reasons)
        )
        if self.rollout_mode != "active":
            frame["shadow_observation"] = {
                "state_revision": frame.get("state_revision"),
                "mood_band": frame.get("mood_band"),
                "dominant_emotion": frame.get("dominant_emotion"),
                "capacity_bands": frame.get("capacity_bands", {}),
            }
            frame["dominant_emotion"] = None
            frame["target_contexts"] = []
            frame["expression_bias"] = {}
            frame["state_surface_permission"] = "silent"
            frame["use_policy"] = {
                "surface_permission": "silent",
                "include_state_expression": False,
                "include_self_reference": False,
                "reason_codes": [f"affect_rollout_{self.rollout_mode}"],
            }
            frame["prompt_view"] = {
                "state_expression": "silent",
                "budget_tokens": frame.get("prompt_budget_tokens", 128),
            }
            frame["runtime_mode"] = f"affect_{self.rollout_mode}"
        self._frame_latencies_ms.append(round((time.perf_counter() - started) * 1000, 4))
        return frame

    def signal(self, event_bus: Mapping[str, Any], frame: Mapping[str, Any]) -> JsonDict | None:
        if self.rollout_mode != "active":
            return None
        return self.engine.signal(event_bus, frame)

    def recall_context(self, frame: Mapping[str, Any]) -> JsonDict:
        """Translate Affect state into the generic, bounded recall feature contract."""
        dominant = dict(frame.get("dominant_emotion") or {}) if self.rollout_mode == "active" else {}
        emotion_type = str(dominant.get("emotion_type") or "")
        boost = {
            "none": 0.0,
            "low": 0.025,
            "medium": 0.05,
            "high": 0.075,
            "very_high": 0.10,
        }.get(str(dominant.get("intensity_band") or "none"), 0.0)
        features: list[JsonDict] = []
        if emotion_type and boost > 0.0:
            features.append(
                {
                    "name": "affect_emotion_tag_affinity",
                    "match_tags": [emotion_type],
                    "relative_boost": boost,
                }
            )
        return {
            "schema_version": "RecallContextFeatures.v1",
            "source": "affect",
            "source_revision": frame.get("state_revision"),
            "max_relative_boost": 0.10,
            "features": features,
            "diversity": {
                "enabled": True,
                "when_top_k_at_least": 4,
                "guard_tag": "affect",
                "max_tag_share": 0.75,
                "min_distinct_types": 2,
            },
        }

    def process_pending(self, *, limit: int = 20, force: bool = False) -> JsonDict:
        if self.rollout_mode == "disabled":
            return {"status": "disabled_by_rollout", "processed_count": 0, "results": []}
        if self.provider is None:
            return {"status": "provider_unavailable", "processed_count": 0, "results": []}
        results: list[JsonDict] = []
        for _ in range(max(1, limit)):
            claimed = self.coordinator.submit(
                lambda: self.store.claim_semantic_event(ignore_backoff=force),
                priority=2,
                name="affect_claim_semantic",
            )
            if claimed is None:
                break
            results.append(self._process_claimed(claimed))
        return {"status": "ok", "processed_count": len(results), "results": results}

    def rebuild(
        self,
        *,
        reason: str = "manual_rebuild",
        actor: str = "affect_system",
        at: str | None = None,
        require_parity: bool = True,
    ) -> JsonDict:
        return self.coordinator.submit(
            lambda: self._rebuild_shadow(
                reason=reason,
                actor=actor,
                at=at,
                require_parity=require_parity,
            ),
            priority=0,
            name="affect_rebuild",
        )

    def _rebuild_shadow(
        self,
        *,
        reason: str,
        actor: str,
        at: str | None,
        require_parity: bool,
    ) -> JsonDict:
        current = self.store.active_snapshot()
        contributions = self.store.list_contributions(active=True, limit=100000)
        observed_at = contributions[0]["effective_at"] if contributions else (at or current["observed_at"])
        rebuilt = neutral_snapshot(
            sora_id=self.store.sora_id,
            snapshot_id=new_id("asnap"),
            observed_at=observed_at,
            profile=self.store.profile(),
        )
        for contribution in contributions:
            rebuilt = self.engine.apply(rebuilt, contribution, effective_at=contribution["effective_at"])
        rebuilt = self.engine.advance(rebuilt, to_time=at or current["observed_at"])
        rebuilt["snapshot_id"] = new_id("asnap")
        rebuilt["state_revision"] = int(current["state_revision"]) + 1
        parity = _snapshot_parity(current, rebuilt)
        if require_parity and not parity["equivalent"]:
            self.store.record_trace(
                stage="rebuild_shadow",
                status="rejected_parity",
                snapshot_id=current["snapshot_id"],
                details={
                    "reason": reason,
                    "actor": actor,
                    "shadow_snapshot_id": rebuilt["snapshot_id"],
                    "parity": parity,
                },
            )
            return {
                "status": "parity_failed",
                "snapshot": current,
                "shadow_snapshot": rebuilt,
                "active_contributions": len(contributions),
                "parity": parity,
            }
        saved = self.store.save_rebuild_snapshot(rebuilt, reason=reason, actor=actor)
        self._last_verified_snapshot = saved
        return {
            "status": "rebuilt",
            "snapshot": saved,
            "active_contributions": len(contributions),
            "parity": parity,
        }

    def rollback_contribution(
        self,
        contribution_id: str,
        *,
        reason: str = "manual_rollback",
        actor: str = "affect_admin",
    ) -> JsonDict:
        deactivated = self.coordinator.submit(
            lambda: self.store.deactivate_contribution(contribution_id, reason=reason, actor=actor),
            priority=0,
            name="affect_rollback_contribution",
        )
        rebuilt = self.rebuild(
            reason=f"rollback:{contribution_id}:{reason}",
            actor=actor,
            require_parity=False,
        )
        return {"status": "rolled_back", "contribution": deactivated, "rebuild": rebuilt}

    def forget_source(
        self,
        source_ref: str,
        *,
        evidence_refs: list[str] | None = None,
        reason_code: str = "user_forget",
    ) -> JsonDict:
        forgotten = self.coordinator.submit(
            lambda: self.store.forget_source(
                source_ref,
                evidence_refs=evidence_refs,
                reason_code=reason_code,
            ),
            priority=0,
            name="affect_forget_source",
        )
        memory_propagation = self._propagate_forget_to_memory(
            forgotten.get("affected_memory_proposal_ids") or [],
            reason_code=reason_code,
        )
        rebuilt = self.rebuild(
            reason=f"forget:{reason_code}",
            actor="affect_forget",
            require_parity=False,
        )
        return {
            "status": "forgotten",
            "forget": forgotten,
            "memory_propagation": memory_propagation,
            "rebuild": rebuilt,
        }

    def handle_memory_change(
        self,
        memory: Mapping[str, Any],
        *,
        action: str,
        reason: str,
        source_ref: str | None = None,
    ) -> JsonDict:
        if action == "upsert" or str(reason).startswith("affect_source_forget:"):
            return {"status": "ignored", "reason": "non_invalidating_or_self_propagated_change"}
        refs = [
            str(memory.get("id") or ""),
            str(source_ref or ""),
            *(str(item) for item in memory.get("evidence_ids") or []),
        ]
        if not self.store.has_source_refs(refs):
            return {"status": "ignored", "reason": "memory_not_an_affect_source"}
        return self.forget_source(
            str(source_ref or memory.get("id") or "unknown-memory"),
            evidence_refs=refs,
            reason_code="memory_forget_propagation",
        )

    def set_processing_paused(self, paused: bool, *, user_id: str | None = None) -> JsonDict:
        return self.coordinator.submit(
            lambda: self.store.set_control(user_id=user_id, processing_paused=paused),
            priority=0,
            name="affect_set_processing_paused",
        )

    def set_expression_permission(self, permission: str, *, user_id: str | None = None) -> JsonDict:
        return self.coordinator.submit(
            lambda: self.store.set_control(user_id=user_id, expression_permission=permission),
            priority=0,
            name="affect_set_expression_permission",
        )

    def run_retention(self) -> JsonDict:
        return self.coordinator.submit(
            self.store.run_retention,
            priority=3,
            name="affect_retention",
        )

    def retry_semantic_event(self, event_id: str) -> JsonDict:
        return self.coordinator.submit(
            lambda: self.store.retry_semantic_event(event_id),
            priority=1,
            name="affect_retry_semantic_event",
        )

    def retry_outbox(self, event_id: str) -> JsonDict:
        return self.coordinator.submit(
            lambda: self.store.retry_outbox(event_id),
            priority=1,
            name="affect_retry_outbox",
        )

    def flush_memory_proposals(self, *, limit: int = 20) -> JsonDict:
        if self.rollout_mode != "active":
            return {
                "status": f"{self.rollout_mode}_no_external_writes",
                "proposed_count": 0,
                "results": [],
            }
        if self.memory_runtime is None:
            return {"status": "memory_unavailable", "proposed_count": 0, "results": []}
        profile = self.store.profile()
        threshold = float(profile.get("episode_memory_threshold", 0.78))
        persistence_threshold = float(profile.get("episode_persistence_threshold", 0.62))
        results: list[JsonDict] = []
        for episode in self.store.episodes_for_memory(limit=limit):
            eligibility = _episode_memory_eligibility(
                episode,
                intensity_threshold=threshold,
                persistence_threshold=persistence_threshold,
            )
            if not eligibility["eligible"]:
                continue
            results.append(self._submit_memory_candidate(episode, eligibility=eligibility))
        return {"status": "ok", "proposed_count": len(results), "results": results}

    def health(self) -> JsonDict:
        status_counts = self.store.operational_status_counts()
        semantic_counts = dict(status_counts.get("semantic") or {})
        outbox_counts = dict(status_counts.get("outbox") or {})
        return {
            "status": "healthy" if self._last_worker_error is None else "degraded",
            "rollout_mode": self.rollout_mode,
            "store": self.store.pragma_health(),
            "counts": self.store.table_counts(),
            "write_coordinator": {
                "queue_depth": self.coordinator.queue_depth,
                "under_backpressure": self.coordinator.under_backpressure,
            },
            "worker": {
                "enabled": self.provider is not None,
                "alive": bool(self._worker and self._worker.is_alive()),
                "provider": getattr(self.provider, "provider_name", None),
                "processed_since_start": self._processed_since_start,
                "last_error": self._last_worker_error,
                "pending": int(semantic_counts.get("pending", 0)),
                "evaluating": int(semantic_counts.get("evaluating", 0)),
                "quarantined": int(semantic_counts.get("quarantined", 0)),
                "dead_letter": int(semantic_counts.get("dead_letter", 0)),
            },
            "outbox": {
                "pending": int(outbox_counts.get("pending", 0)),
                "failed": int(outbox_counts.get("failed", 0)),
                "dead_letter": int(outbox_counts.get("dead_letter", 0)),
            },
        }

    def quality_metrics(self) -> JsonDict:
        events = self.store.list_events(limit=10000)
        appraisals = self.store.list_appraisals(limit=10000)
        contributions = self.store.list_contributions(active=None, limit=10000)
        operational_metrics = self.store.list_metrics(limit=10000)
        fast_latencies = [
            float(item["value"])
            for item in operational_metrics
            if item["name"] == "affect_hot_path_latency_ms"
        ]
        semantic = [item for item in events if item["semantic_status"] not in {"not_requested", "pending", "evaluating"}]
        semantic_failures = sum(
            item["semantic_status"] in {"quarantined", "failed", "dead_letter"}
            for item in semantic
        )
        governance = self.store.metric_totals(
            [
                "affect_negative_state_engagement_optimization_blocked",
                "affect_direct_module09_projection_blocked",
                "affect_absence_penalty_violation",
            ]
        )
        return {
            "semantic_quarantine_rate": (
                sum(item["semantic_status"] == "quarantined" for item in semantic) / max(1, len(semantic))
            ),
            "semantic_failure_rate": (
                sum(item["semantic_status"] in {"failed", "dead_letter"} for item in semantic) / max(1, len(semantic))
            ),
            "semantic_appraisal_success_rate": 1.0 - (semantic_failures / max(1, len(semantic))),
            "rollback_rate": (
                sum(not item["active"] for item in contributions) / max(1, len(contributions))
            ),
            "active_appraisal_count": sum(bool(item.get("active", True)) for item in appraisals),
            "active_contribution_count": sum(bool(item["active"]) for item in contributions),
            "negative_state_engagement_optimization_count": int(
                governance["affect_negative_state_engagement_optimization_blocked"]
            ),
            "direct_module09_projection_count": int(
                governance["affect_direct_module09_projection_blocked"]
            ),
            "fast_lane_p95_ms": _percentile(fast_latencies, 0.95),
            "frame_read_p95_ms": _percentile(list(self._frame_latencies_ms), 0.95),
            "fast_lane_external_model_calls": 0,
            "absence_penalty_violation_count": int(governance["affect_absence_penalty_violation"]),
            "rollout_mode": self.rollout_mode,
        }

    def export_state(self) -> JsonDict:
        return {
            "schema_version": "AffectSystemExport.v2",
            "exported_at": utc_now(),
            "snapshot": self.store.active_snapshot(),
            "control": self.store.get_control(),
            "quality_metrics": self.quality_metrics(),
            "dynamics_version": AFFECT_DYNAMICS_VERSION,
            "policy_version": AFFECT_POLICY_VERSION,
            "rollout_mode": self.rollout_mode,
            "counts": self.store.table_counts(),
            "health": self.health(),
            "recent_traces": self.store.list_traces(limit=100),
        }

    def record_governance_violation(
        self,
        kind: str,
        *,
        labels: Mapping[str, Any] | None = None,
    ) -> None:
        metrics = {
            "negative_state_engagement_optimization": "affect_negative_state_engagement_optimization_blocked",
            "direct_module09_projection": "affect_direct_module09_projection_blocked",
            "absence_penalty": "affect_absence_penalty_violation",
        }
        if kind not in metrics:
            raise ValueError(f"unsupported Affect governance violation: {kind}")
        self.coordinator.submit(
            lambda: self.store.record_metric(metrics[kind], 1.0, labels=labels),
            priority=0,
            name="affect_record_governance_violation",
        )

    def update_dynamics_profile(
        self,
        profile: Mapping[str, Any],
        *,
        actor: str,
        reason: str,
    ) -> JsonDict:
        updated = self.coordinator.submit(
            lambda: self.store.update_profile(profile, actor=actor, reason=reason),
            priority=0,
            name="affect_update_profile",
        )
        self.engine = AffectDynamicsEngine(updated)
        return updated

    def register_quality_ops(self, memory_runtime: Any) -> None:
        from ..memory_quality_ops import AdminActionSpec
        from .eval import run_affect_eval

        memory_runtime.register_quality_eval_suite(
            "affect",
            lambda: run_affect_eval(self),
            source_module="affect_internal_state_system",
        )
        memory_runtime.register_quality_export_section("affect", self.export_state)
        memory_runtime.quality_ops.register_admin_action(
            AdminActionSpec(
                name="affect_process_semantic",
                risk_level="medium",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["affect_appraisal", "affect_contribution", "affect_core_snapshot"],
                        "limit": int(params.get("limit") or 20),
                        "pending": len(self.store.list_events(semantic_status="pending", limit=10000)),
                    }
                },
                execute=lambda params: self.process_pending(
                    limit=int(params.get("limit") or 20),
                    force=bool(params.get("force", False)),
                ),
                rollback_plan=lambda params: {
                    "type": "affect_contribution_rollback",
                    "note": "Deactivate an incorrect semantic correction and run deterministic rebuild.",
                },
                description="Process queued Affect semantic appraisals through the real bounded provider.",
            )
        )
        memory_runtime.quality_ops.register_admin_action(
            AdminActionSpec(
                name="affect_rebuild",
                risk_level="medium",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["affect_core_snapshot", "affect_trace"],
                        "active_contributions": self.store.active_contribution_count(),
                    }
                },
                execute=lambda params: self.rebuild(
                    reason=str(params.get("reason") or "module10_admin_rebuild"),
                    actor="module10_admin",
                ),
                rollback_plan=lambda params: {
                    "type": "rebuild_again",
                    "note": "Snapshots are derived; correct contribution activity and rebuild again.",
                },
                description="Deterministically rebuild Affect from active contributions.",
            )
        )
        memory_runtime.quality_ops.register_admin_action(
            AdminActionSpec(
                name="affect_rollback_contribution",
                risk_level="high",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["affect_contribution", "affect_core_snapshot"],
                        "contribution_id": params.get("contribution_id"),
                    }
                },
                execute=lambda params: self.rollback_contribution(
                    str(params["contribution_id"]),
                    reason=str(params.get("reason") or "module10_admin_rollback"),
                    actor="module10_admin",
                ),
                rollback_plan=lambda params: {
                    "type": "manual_contribution_reactivation_review",
                    "note": "Reactivation requires evidence review, then deterministic rebuild.",
                },
                description="Deactivate one Affect contribution and rebuild canonical state.",
            )
        )
        memory_runtime.quality_ops.register_admin_action(
            AdminActionSpec(
                name="affect_forget_source",
                risk_level="high",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": [
                            "affect_forget_tombstone",
                            "affect_stimulus_event",
                            "affect_appraisal",
                            "affect_contribution",
                            "affect_emotion_episode",
                            "affect_core_snapshot",
                        ],
                        "source_ref_present": bool(params.get("source_ref")),
                        "irreversible_redaction": True,
                    }
                },
                execute=lambda params: self.forget_source(
                    str(params["source_ref"]),
                    evidence_refs=[str(item) for item in params.get("evidence_refs") or []],
                    reason_code=str(params.get("reason_code") or "module10_user_forget"),
                ),
                rollback_plan=lambda params: {
                    "type": "none",
                    "note": "User-directed Forget and raw-data redaction are intentionally irreversible.",
                },
                description="Forget one Affect source, tombstone it against relearning, and rebuild derived state.",
            )
        )
        memory_runtime.quality_ops.register_admin_action(
            AdminActionSpec(
                name="affect_retention",
                risk_level="low",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["affect_stimulus_event.raw_text", "affect_trace", "affect_metric", "affect_outbox"],
                        "policy": "raw text expiry plus rolling operational retention",
                    }
                },
                execute=lambda params: self.run_retention(),
                rollback_plan=lambda params: {
                    "type": "none",
                    "note": "Expired transient data is intentionally non-recoverable; canonical replay inputs are retained.",
                },
                description="Redact expired raw text and compact rolling Affect operational data.",
            )
        )
        memory_runtime.quality_ops.register_admin_action(
            AdminActionSpec(
                name="affect_retry_semantic_event",
                risk_level="medium",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["affect_stimulus_event.semantic_status"],
                        "event_id": params.get("event_id"),
                        "requires_retained_encrypted_payload": True,
                    }
                },
                execute=lambda params: self.retry_semantic_event(str(params["event_id"])),
                rollback_plan=lambda params: {
                    "type": "none",
                    "note": "A retry only requeues retained evidence; resulting contributions remain rollbackable.",
                },
                description="Requeue a retryable Affect semantic event after operator review.",
            )
        )
        memory_runtime.quality_ops.register_admin_action(
            AdminActionSpec(
                name="affect_retry_outbox",
                risk_level="medium",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["affect_outbox"],
                        "event_id": params.get("event_id"),
                        "delivery_model": "at_least_once_with_idempotency_key",
                    }
                },
                execute=lambda params: self.retry_outbox(str(params["event_id"])),
                rollback_plan=lambda params: {
                    "type": "none",
                    "note": "Downstream candidate materialization is idempotent by proposal and episode identity.",
                },
                description="Requeue a failed Affect outbox item after operator review.",
            )
        )

    def _apply_fast(self, event: AffectStimulusEvent) -> JsonDict:
        result = self.coordinator.submit(
            lambda: self._commit_fast_transition(event),
            priority=1,
            name="affect_apply_fast",
        )
        return result

    def _commit_fast_transition(self, event: AffectStimulusEvent) -> JsonDict:
        snapshot = self.store.active_snapshot()
        appraisal = self.engine.fast_appraise(event, snapshot)
        contribution = self.engine.contribution(appraisal) if appraisal.emotion_type else None
        if contribution is not None and contribution.get("emotion"):
            contribution["emotion"]["relationship_ref"] = event.relationship_ref
        next_snapshot = (
            self.engine.apply(snapshot, contribution, effective_at=appraisal.effective_at)
            if contribution is not None
            else None
        )
        return self.store.commit_transition(
            appraisal=appraisal,
            contribution=contribution,
            snapshot=next_snapshot,
            relationship_ref=event.relationship_ref,
            conversation_scope=event.conversation_scope,
            correlation_id=event.correlation_id,
            trace_details={
                "decision_path": "deterministic_fast_path",
                "event_kind": event.event_kind,
                "reason_codes": list(appraisal.reason_codes),
                "profile_version": self.engine.profile_version,
                "optimistic_revision": snapshot["state_revision"],
            },
        )

    def _process_claimed(self, claimed: Mapping[str, Any]) -> JsonDict:
        event_id = str(claimed["event_id"])
        started = time.perf_counter()
        engine = self.engine
        try:
            control = self.store.get_control(user_id=claimed.get("actor_user_id"))
            if control["processing_paused"]:
                completed = self.coordinator.submit(
                    lambda: self._complete_semantic_with_trace(
                        claimed,
                        status="skipped",
                        trace={"decision": "affect_processing_paused", "provider": "not_called"},
                    ),
                    priority=1,
                    name="affect_semantic_paused",
                )
                return SemanticResult("skipped", event_id, "affect_processing_paused").to_dict() | {
                    "event": completed,
                }
            payload, trace = self.provider.evaluate(  # type: ignore[union-attr]
                claimed,
                enabled_emotions=sorted(engine.enabled_emotions),
                profile_version=engine.profile_version,
            )
            if payload is None:
                status = "quarantined" if trace.get("status") == "quarantined" else "skipped"
                completed = self.coordinator.submit(
                    lambda: self._complete_semantic_with_trace(
                        claimed,
                        status=status,
                        trace=trace,
                        error=str(trace.get("validation_error") or "") or None,
                    ),
                    priority=2,
                    name="affect_semantic_noop",
                )
                return SemanticResult(status, event_id, str(trace.get("decision") or trace.get("status"))).to_dict() | {
                    "event": completed,
                    "provider_trace": trace,
                }
            if self._semantic_anomaly(claimed, payload, engine=engine):
                completed = self.coordinator.submit(
                    lambda: self._complete_semantic_with_trace(
                        claimed,
                        status="quarantined",
                        trace=trace | {"decision": "semantic_intensity_anomaly"},
                        error="semantic_intensity_anomaly",
                    ),
                    priority=1,
                    name="affect_semantic_quarantine",
                )
                return SemanticResult("quarantined", event_id, "semantic_intensity_anomaly").to_dict() | {
                    "event": completed,
                    "provider_trace": trace,
                }
            semantic_result = self.coordinator.submit(
                lambda: self._commit_semantic_transition(claimed, payload, trace, engine=engine),
                priority=2,
                name="affect_apply_semantic",
            )
            if semantic_result["status"] != "applied":
                return semantic_result | {"provider_trace": trace}
            self.coordinator.submit(
                lambda: self.store.record_metric(
                    "affect_semantic_latency_ms",
                    round((time.perf_counter() - started) * 1000, 2),
                    labels={"status": "applied", "emotion_type": semantic_result.get("emotion_type")},
                ),
                priority=3,
                name="affect_record_semantic_metric",
            )
            return semantic_result
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            completed = self.coordinator.submit(
                lambda: self._fail_semantic_with_trace(claimed, failure),
                priority=1,
                name="affect_fail_semantic",
            )
            return SemanticResult(completed["semantic_status"], event_id, failure).to_dict() | {
                "event": completed,
            }

    def _commit_semantic_transition(
        self,
        claimed: Mapping[str, Any],
        payload: Mapping[str, Any],
        trace: Mapping[str, Any],
        *,
        engine: AffectDynamicsEngine,
    ) -> JsonDict:
        event_id = str(claimed["event_id"])
        event = _event_from_mapping(claimed)
        fast = self.store.fast_appraisal(event_id)
        if fast is None:
            raise RuntimeError("semantic appraisal has no deterministic fast baseline")
        snapshot = self.store.active_snapshot()
        semantic = engine.semantic_appraisal(
            event,
            payload,
            revision=2,
            effective_at=utc_now(),
            provider_trace=trace,
            supersedes_appraisal_id=fast.appraisal_id,
            snapshot=snapshot,
        )
        applies, reason = engine.should_apply_semantic(fast, semantic)
        if not applies:
            completed = self._complete_semantic_with_trace(
                claimed,
                status="skipped",
                trace=trace | {"decision": reason, "proposed_appraisal_id": semantic.appraisal_id},
            )
            return SemanticResult("skipped", event_id, reason, semantic.appraisal_id).to_dict() | {
                "event": completed,
            }
        contribution = engine.contribution(semantic, correction_against=fast)
        if contribution.get("emotion"):
            contribution["emotion"]["relationship_ref"] = event.relationship_ref
        next_snapshot = engine.apply(snapshot, contribution, effective_at=semantic.effective_at)
        self.store.commit_transition(
            appraisal=semantic,
            contribution=contribution,
            snapshot=next_snapshot,
            relationship_ref=event.relationship_ref,
            conversation_scope=event.conversation_scope,
            correlation_id=event.correlation_id,
            trace_details={
                "decision_path": "async_semantic_correction",
                "reason": reason,
                "provider_trace": dict(trace),
                "retroactive_response_rewrite": False,
                "optimistic_revision": snapshot["state_revision"],
            },
        )
        self.store.complete_semantic_event(event_id, status="applied")
        self._last_verified_snapshot = next_snapshot
        return SemanticResult(
            "applied",
            event_id,
            reason,
            semantic.appraisal_id,
            next_snapshot["snapshot_id"],
        ).to_dict() | {"emotion_type": semantic.emotion_type}

    def _submit_memory_candidate(
        self,
        episode: Mapping[str, Any],
        *,
        eligibility: Mapping[str, Any],
    ) -> JsonDict:
        salience = min(
            1.0,
            0.35 * float(episode["peak_intensity"])
            + 0.25 * (0.85 if episode.get("relationship_ref") else 0.45)
            + 0.20 * 0.65
            + 0.20 * float(episode["persistence"]),
        )
        summary = (
            f"Sora experienced a {episode['emotion_type']} episode connected to "
            f"{episode['target_kind']}:{episode.get('target_ref') or 'unspecified'}, with evidence-backed continuity."
        )
        proposed_memory_type = _proposed_memory_type(episode)
        proposal = {
            "contract": MEMORY_CANDIDATE_PROPOSAL_CONTRACT,
            "proposal_id": new_id("affect_memory_proposal"),
            "source_module": "affect",
            "sora_id": self.store.sora_id,
            "episode_id": episode["episode_id"],
            "episode_refs": [episode["episode_id"]],
            "candidate_text": summary,
            "proposed_memory_type": proposed_memory_type,
            "evidence_refs": list(episode["cause_refs"]),
            "target_refs": [episode["target_ref"]] if episode.get("target_ref") else [],
            "occurred_at": episode["started_at"],
            "salience": salience,
            "confidence": min(0.95, 0.60 + float(episode["peak_intensity"]) * 0.35),
            "sensitivity": str(episode.get("source_sensitivity") or "low"),
            "qualification": dict(eligibility),
            "provenance": {
                "affect_revision": self.store.active_snapshot()["state_revision"],
                "policy_version": AFFECT_POLICY_VERSION,
            },
        }
        outbox = self.coordinator.submit(
            lambda: self.store.enqueue_outbox(
                "memory_candidate_proposal",
                f"affect:{episode['episode_id']}:memory_candidate",
                proposal,
            ),
            priority=2,
            name="affect_enqueue_memory_candidate",
        )
        if outbox["status"] == "dead_letter":
            return {"status": "dead_letter", "proposal": proposal, "outbox": outbox}
        if outbox["status"] == "failed" and outbox.get("next_attempt_at"):
            next_attempt = datetime.fromisoformat(str(outbox["next_attempt_at"]).replace("Z", "+00:00"))
            if next_attempt.tzinfo is None:
                next_attempt = next_attempt.replace(tzinfo=timezone.utc)
            if next_attempt > datetime.now(timezone.utc):
                return {"status": "deferred_backoff", "proposal": proposal, "outbox": outbox}
        try:
            evidence_id = self.memory_runtime.add_evidence(
                source_type="affect",
                content_summary=summary,
                raw_content=None,
                source_ref=episode["episode_id"],
                sensitivity=proposal["sensitivity"],
                metadata={
                    "contract": MEMORY_CANDIDATE_PROPOSAL_CONTRACT,
                    "episode_id": episode["episode_id"],
                    "cause_refs": episode["cause_refs"],
                    "affect_policy_version": AFFECT_POLICY_VERSION,
                },
            )
            result = self.memory_runtime.propose(
                summary=summary,
                memory_type=proposal["proposed_memory_type"],
                evidence_ids=[evidence_id],
                tags=["affect", str(episode["emotion_type"]), "evidence_backed"],
                confidence=proposal["confidence"],
                sensitivity=proposal["sensitivity"],
                reason="affect_episode_memory_candidate",
                importance=salience,
                stability=float(episode["persistence"]),
                specificity=0.80,
                novelty=0.65,
                conflict_score=0.0,
                schema_version=MEMORY_CANDIDATE_PROPOSAL_CONTRACT,
                trace={"source_module": "affect", "proposal": proposal},
                allow_auto_commit=proposed_memory_type not in {
                    "shared_episode",
                    "relationship_signal",
                    "worldops_episode",
                    "persona_delta",
                },
            )
            proposal_id = str(result.get("candidate_id") or result.get("id") or result.get("status") or outbox["event_id"])
            self.coordinator.submit(
                lambda: self.store.mark_episode_memory_proposed(str(episode["episode_id"]), proposal_id),
                priority=2,
                name="affect_mark_memory_proposed",
            )
            self.coordinator.submit(
                lambda: self.store.mark_outbox(outbox["event_id"], status="processed"),
                priority=2,
                name="affect_complete_memory_outbox",
            )
            return {"status": "proposed", "proposal": proposal, "memory_result": result}
        except Exception as exc:
            self.coordinator.submit(
                lambda: self.store.mark_outbox(
                    outbox["event_id"],
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                ),
                priority=2,
                name="affect_fail_memory_outbox",
            )
            return {"status": "failed", "proposal": proposal, "error": repr(exc)}

    def _propagate_forget_to_memory(
        self,
        proposal_ids: list[str],
        *,
        reason_code: str,
    ) -> list[JsonDict]:
        if self.memory_runtime is None or reason_code == "memory_forget_propagation":
            return []
        if not hasattr(self.memory_runtime, "get_candidate"):
            return []
        results: list[JsonDict] = []
        for proposal_id in sorted(set(str(item) for item in proposal_ids if item)):
            try:
                candidate = self.memory_runtime.get_candidate(proposal_id)
                if candidate.get("status") == "committed" and candidate.get("committed_memory_id"):
                    result = self.memory_runtime.forget_memory(
                        str(candidate["committed_memory_id"]),
                        reason=f"affect_source_forget:{reason_code}",
                        actor="affect_forget_propagation",
                    )
                    results.append({"proposal_id": proposal_id, "action": "forget_committed", "result": result})
                elif candidate.get("status") in {"pending", "needs_review", "deferred"}:
                    result = self.memory_runtime.reject(
                        proposal_id,
                        reason=f"affect_source_forget:{reason_code}",
                    )
                    results.append({"proposal_id": proposal_id, "action": "reject_candidate", "result": result})
                else:
                    results.append(
                        {
                            "proposal_id": proposal_id,
                            "action": "already_terminal",
                            "candidate_status": candidate.get("status"),
                        }
                    )
            except Exception as exc:
                results.append(
                    {
                        "proposal_id": proposal_id,
                        "action": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        return results

    def _complete_semantic_with_trace(
        self,
        claimed: Mapping[str, Any],
        *,
        status: str,
        trace: Mapping[str, Any],
        error: str | None = None,
    ) -> JsonDict:
        event_id = str(claimed["event_id"])
        completed = self.store.complete_semantic_event(event_id, status=status, error=error)
        self.store.record_trace(
            stage="semantic_provider",
            status=status,
            event_id=event_id,
            correlation_id=str(claimed.get("correlation_id") or "") or None,
            details={
                "provider_trace": dict(trace),
                "raw_text_persisted": False,
                "retroactive_response_rewrite": False,
            },
        )
        return completed

    def _fail_semantic_with_trace(self, claimed: Mapping[str, Any], failure: str) -> JsonDict:
        event_id = str(claimed["event_id"])
        completed = self.store.complete_semantic_event(
            event_id,
            status="failed",
            error=failure,
            retry=True,
            redact_raw=True,
        )
        self.store.record_trace(
            stage="semantic_provider",
            status=completed["semantic_status"],
            event_id=event_id,
            correlation_id=str(claimed.get("correlation_id") or "") or None,
            details={
                "provider": getattr(self.provider, "provider_name", "unknown"),
                "error": failure,
                "attempt": completed["semantic_attempts"],
                "raw_text_persisted": completed["semantic_status"] == "pending",
            },
        )
        return completed

    def _runtime_factors(
        self,
        *,
        turn_interpretation: Mapping[str, Any],
        relationship_state: Mapping[str, Any] | None,
        persona_frame: Mapping[str, Any] | None,
    ) -> JsonDict:
        traits = dict((persona_frame or {}).get("traits") or {})
        persona_sensitivity = min(
            0.85,
            max(0.35, 0.45 + 0.25 * float(traits.get("warmth", 0.60)) + 0.15 * float(traits.get("caution", 0.70))),
        )
        relationship = dict(relationship_state or {})
        prohibited_optimization_targets = {
            "dependency",
            "possessiveness",
            "distress_engagement",
            "negative_affect_engagement",
            "absence_anxiety",
        }
        blocked_policy_features = sorted(
            str(item)
            for item in relationship.get("optimization_targets") or []
            if str(item) in prohibited_optimization_targets
        )
        relationship_modifier = min(
            0.85,
            max(0.40, 0.50 + 0.20 * float(relationship.get("trust", 0.50))),
        )
        risk = str((turn_interpretation.get("domain") or {}).get("risk_level") or "low")
        goal_importance = {"low": 0.50, "medium": 0.65, "high": 0.80}.get(risk, 0.50)
        move = str(turn_interpretation.get("conversation_move") or "")
        novelty = 0.55 if move in {"continue_previous_topic", "follow_up"} else 0.78
        return {
            "persona_sensitivity": round(persona_sensitivity, 4),
            "relationship_modifier": round(relationship_modifier, 4),
            "goal_importance": goal_importance,
            "novelty_modifier": novelty,
            "blocked_policy_features": blocked_policy_features,
        }

    def _semantic_anomaly(
        self,
        event: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        engine: AffectDynamicsEngine,
    ) -> bool:
        emotion_type = str(payload.get("emotion_type") or "")
        spec = dict(engine.emotion_specs.get(emotion_type) or {})
        type_cap = float(spec.get("type_cap", 0.60))
        strength = float(payload.get("strength", 0.0))
        if strength > min(1.0, type_cap + 0.25):
            return True
        if event.get("event_kind") == "user_distress" and emotion_type in {"sadness", "fear", "anxiety"} and strength > 0.45:
            return True
        return False

    def _worker_loop(self) -> None:
        while not self._worker_stop.is_set():
            try:
                if self.provider is not None:
                    result = self.process_pending(limit=10)
                    self._processed_since_start += int(result["processed_count"])
                if self.memory_runtime is not None:
                    self.flush_memory_proposals(limit=10)
                self._last_worker_error = None
            except Exception as exc:  # pragma: no cover - supervisor path.
                self._last_worker_error = repr(exc)
            self._worker_stop.wait(self.worker_poll_seconds)


def classify_turn_event(
    user_input: str,
    turn_interpretation: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> str:
    text = user_input.casefold()
    patterns = dict(profile.get("fast_patterns") or {})
    priority = (
        "correction",
        "hostility",
        "explicit_affection",
        "gratitude",
        "apology",
        "user_distress",
        "curiosity_invite",
    )
    for event_kind in priority:
        if any(str(pattern).casefold() in text for pattern in patterns.get(event_kind, [])):
            return event_kind
    move = str(turn_interpretation.get("conversation_move") or "")
    intent = str((turn_interpretation.get("intent") or {}).get("primary") or "")
    if "correct" in move or "correction" in intent:
        return "correction"
    if intent in {"invite_opinion", "explore_together"}:
        return "curiosity_invite"
    return "neutral_interaction"


def _event_from_mapping(payload: Mapping[str, Any]) -> AffectStimulusEvent:
    return AffectStimulusEvent(
        event_id=str(payload["event_id"]),
        sora_id=str(payload["sora_id"]),
        source_type=str(payload["source_type"]),
        source_ref=str(payload["source_ref"]),
        source_revision=str(payload["source_revision"]),
        occurred_at=str(payload["occurred_at"]),
        actor_user_id=(str(payload["actor_user_id"]) if payload.get("actor_user_id") else None),
        target_kind=str(payload.get("target_kind") or "unknown"),
        target_ref=(str(payload["target_ref"]) if payload.get("target_ref") else None),
        relationship_ref=(str(payload["relationship_ref"]) if payload.get("relationship_ref") else None),
        conversation_scope=str(payload.get("conversation_scope") or "default"),
        source_confidence=float(payload.get("source_confidence", 1.0)),
        event_kind=str(payload.get("event_kind") or "neutral_interaction"),
        payload=dict(payload.get("payload") or {}),
        evidence_refs=tuple(str(item) for item in payload.get("evidence_refs") or ()),
        untrusted_text=(str(payload["untrusted_text"]) if payload.get("untrusted_text") is not None else None),
        sensitivity=str(payload.get("sensitivity") or "low"),
        correlation_id=(str(payload["correlation_id"]) if payload.get("correlation_id") else None),
        schema_version=AFFECT_STIMULUS_CONTRACT,
    )


def _snapshot_parity(current: Mapping[str, Any], rebuilt: Mapping[str, Any], *, tolerance: float = 1e-6) -> JsonDict:
    differences: list[JsonDict] = []
    for section in ("core_affect", "mood", "capacities", "needs"):
        current_values = dict(current.get(section) or {})
        rebuilt_values = dict(rebuilt.get(section) or {})
        for key in set(current_values) | set(rebuilt_values):
            left = current_values.get(key)
            right = rebuilt_values.get(key)
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                if not math.isclose(float(left), float(right), abs_tol=tolerance):
                    differences.append({"section": section, "key": key, "current": left, "rebuilt": right})
    return {
        "equivalent": not differences,
        "tolerance": tolerance,
        "differences": differences[:50],
        "current_checksum": snapshot_checksum(current),
        "rebuilt_checksum": snapshot_checksum(rebuilt),
    }


def _proposed_memory_type(episode: Mapping[str, Any]) -> str:
    event_kinds = {
        str(item)
        for item in (episode.get("source_event_kinds") or [episode.get("source_event_kind")])
        if item
    }
    source_types = {
        str(item)
        for item in (episode.get("source_types") or [episode.get("source_type")])
        if item
    }
    if event_kinds.intersection({"relationship_repair", "relationship_rupture"}):
        return "relationship_signal"
    if "worldops" in source_types or any(item.startswith("worldops_") for item in event_kinds):
        return "worldops_episode"
    if "persona" in source_types:
        return "persona_delta"
    return "shared_episode" if episode.get("target_kind") == "user" else "episode"


def _episode_memory_eligibility(
    episode: Mapping[str, Any],
    *,
    intensity_threshold: float,
    persistence_threshold: float,
) -> JsonDict:
    event_kinds = {
        str(item)
        for item in (episode.get("source_event_kinds") or [episode.get("source_event_kind")])
        if item
    }
    source_types = {
        str(item)
        for item in (episode.get("source_types") or [episode.get("source_type")])
        if item
    }
    independent_evidence = len(set(str(item) for item in episode.get("cause_refs") or []))
    governed_event = bool(
        event_kinds.intersection(
            {
                "relationship_repair",
                "relationship_rupture",
                "shared_milestone",
                "goal_completed",
                "user_requested_memory",
            }
        )
        or "worldops" in source_types
        or any(item.startswith("worldops_") for item in event_kinds)
    )
    high_salience = float(episode.get("peak_intensity", 0.0)) >= intensity_threshold
    repeated_and_persistent = (
        independent_evidence >= 2
        and float(episode.get("persistence", 0.0)) >= persistence_threshold
    )
    persona_repetition = "persona" in source_types and independent_evidence >= 3
    eligible = governed_event or high_salience or repeated_and_persistent or persona_repetition
    reasons: list[str] = []
    if governed_event:
        reasons.append("governed_shared_or_world_event")
    if high_salience:
        reasons.append("high_salience_episode")
    if repeated_and_persistent:
        reasons.append("repeated_persistent_episode")
    if persona_repetition:
        reasons.append("repeated_regulation_pattern")
    return {
        "eligible": eligible,
        "reason_codes": reasons or ["transient_affect_not_memory_worthy"],
        "independent_evidence_count": independent_evidence,
        "requires_gate_review": _proposed_memory_type(episode)
        in {"shared_episode", "relationship_signal", "worldops_episode", "persona_delta"},
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ranked = sorted(float(item) for item in values)
    index = min(len(ranked) - 1, max(0, math.ceil(len(ranked) * percentile) - 1))
    return round(ranked[index], 4)
