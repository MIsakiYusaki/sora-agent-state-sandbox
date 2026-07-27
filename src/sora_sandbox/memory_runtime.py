from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .memory_canonical_store import MemoryCanonicalStore
from .memory_canonical_eval import MemoryCanonicalEvalService
from .memory_episode_ingestion import EpisodeIngestionService
from .memory_extractor_schema import (
    ExtractorResult,
    WriteOperation,
    candidate_to_dict,
)
from .memory_extraction import extract_memory_candidates
from .memory_gate import (
    MemoryGate,
    classify_memory_type as _classify_memory_type,
    detect_sensitivity as _detect_sensitivity,
)
from .memory_gate_engine import compute_candidate_hash, evaluate_candidate_gate
from .memory_gate_pipeline import MemoryGatePipeline
from .memory_gate_schema import GateDecisionAction
from .memory_context_packer import MemoryContextPacker
from .memory_embedding import EmbeddingProvider, create_embedding_provider_from_env
from .memory_index_fts import index_text as _index_text
from .memory_index_outbox import MemoryIndexOutbox
from .memory_hybrid_retrieval import MemoryRetrievalService
from .memory_lifecycle import MemoryLifecycleService
from .memory_lifecycle_eval import MemoryLifecycleEvalService
from .memory_observability import MemoryObservabilityService
from .memory_projector import MemoryProjectorBus
from .memory_quality_ops import AdminActionSpec, MemoryQualityOpsService
from .memory_recall_planner import RecallPlanner, create_recall_planner_from_env, plan_to_json
from .memory_recall_planner_eval import MemoryRecallPlannerEvalService
from .memory_recall_eval_service import MemoryRecallEvalService
from .memory_reranker import MemoryReranker, create_reranker_from_env
from .memory_retention import MemoryRetentionService
from .memory_schema import (
    JsonDict,
    json_dumps,
    json_loads,
    new_id as _new_id,
    utc_now as _utc_now,
)
from .memory_store import (
    evidence_from_row,
    init_memory_schema,
    revision_from_row,
    row_value,
)
from .memory_surface_eval import MemorySurfaceEvalService
from .memory_surface_policy import SurfacePolicyEngine, create_surface_policy_from_env
from .memory_vector_index import VectorIndex
from .paths import SEEDS_DIR, workspace_state_dir
from .state import load_jsonl


def _json(value: Any) -> str:
    return json_dumps(value)


def _loads(value: str | None, default: Any) -> Any:
    return json_loads(value, default)


def _content_hash(*parts: str | None) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update((part or "").encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _feedback_default_value(feedback_type: str) -> float:
    normalized = feedback_type.lower().strip()
    if normalized in {"helpful", "useful", "use_more", "good_surface", "relevant"}:
        return 1.0
    if normalized in {"too_much", "not_relevant", "bad_surface", "annoying", "wrong_context"}:
        return -1.0
    return 0.0


@dataclass
class MemoryRuntime:
    """Local canonical memory runtime used by the PoC.

    SQLite is the source of truth. The JSON state keeps only a compatibility
    projection for older debug commands and tests.
    """

    path: Path
    owner_user_id: str = "local-user"
    agent_id: str = "sora_default"
    recall_planner: RecallPlanner | None = None
    gate: MemoryGate | None = None
    embedding_provider: EmbeddingProvider | None = None
    reranker: MemoryReranker | None = None
    surface_policy: SurfacePolicyEngine | None = None
    vector_index_factory: Callable[[sqlite3.Connection, EmbeddingProvider], VectorIndex] | None = None

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._memory_change_listeners: dict[str, Callable[..., Any]] = {}
        self.gate = self.gate or MemoryGate()
        self.recall_planner = self.recall_planner or create_recall_planner_from_env()
        self.embedding_provider = self.embedding_provider or create_embedding_provider_from_env()
        self.reranker = self.reranker or create_reranker_from_env()
        self.surface_policy = self.surface_policy or create_surface_policy_from_env()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()
        self.ingestion = EpisodeIngestionService(self._conn)
        self.index_outbox = MemoryIndexOutbox(
            self._conn,
            lock=self._lock,
            get_memory=self.get_memory,
            evidence_for_memory=self.evidence_for_memory,
            sync_fts=self._sync_fts,
            remove_from_fts=self._remove_from_fts,
            embedding_provider=self.embedding_provider,
            vector_index_factory=self.vector_index_factory,
        )
        self.retrieval = MemoryRetrievalService(
            self._conn,
            self._lock,
            self.owner_user_id,
            self.agent_id,
            self.recall_planner,
            self.index_outbox.vector_index,
            MemoryContextPacker(self.surface_policy.provider),
            self.reranker,
            self.surface_policy,
        )
        self.projector_bus = MemoryProjectorBus(self._conn, lock=self._lock)
        self.canonical_store = MemoryCanonicalStore(
            self._conn,
            lock=self._lock,
            get_memory=self.get_memory,
            get_candidate=self.get_candidate,
            append_revision=self._append_revision,
            sync_fts=self._sync_fts,
            remove_from_fts=self._remove_from_fts,
            record_delete_propagation=self.ingestion.record_delete_propagation,
            publish_memory_change=self._publish_memory_change,
        )
        self.lifecycle = MemoryLifecycleService(
            self._conn,
            lock=self._lock,
            owner_user_id=self.owner_user_id,
            agent_id=self.agent_id,
            get_memory=self.get_memory,
            list_memories=self.list_memories,
            evidence_for_memory=self.evidence_for_memory,
            propose_candidate=self.propose,
            run_candidate_gate=self.run_candidate_gate,
            append_revision=self._append_revision,
        )
        self.retention = MemoryRetentionService(
            self._conn,
            lock=self._lock,
            delete_evidence=self.delete_evidence,
            set_memory_status=self._set_status,
            get_memory=self.get_memory,
            get_evidence=self.get_evidence,
            record_tombstone=self.lifecycle.record_tombstone,
        )
        self.lifecycle.attach_retention_process(self.retention.process_expired)
        self.observability = MemoryObservabilityService(
            self._conn,
            lock=self._lock,
            store_audit=self.store_audit,
            index_pending_count=self.index_outbox.pending_count,
            index_failed_count=self.index_outbox.failed_count,
            index_dead_letter_count=self.index_outbox.dead_letter_count,
            index_health=self.index_outbox.health,
            projector_pending_count=self.projector_bus.pending_count,
            projector_failed_count=self.projector_bus.failed_count,
            projector_dead_letter_count=self.projector_bus.dead_letter_count,
            retention_backlog_count=self.retention.backlog_count,
            lifecycle_health=self.lifecycle.lifecycle_health,
            projector_checkpoints=self.projector_bus.list_checkpoints,
        )
        self.canonical_eval = MemoryCanonicalEvalService(
            self._conn,
            lock=self._lock,
            store_health=self.store_health,
        )
        self.recall_eval = MemoryRecallEvalService(
            self._conn,
            lock=self._lock,
            recall=lambda query: self.recall(query, top_k=5),
            list_memories=self.list_memories,
        )
        self.planner_eval = MemoryRecallPlannerEvalService(
            self._conn,
            lock=self._lock,
            plan_recall=self.plan_recall,
        )
        self.surface_eval = MemorySurfaceEvalService(
            self._conn,
            lock=self._lock,
            surface_policy=self.surface_policy,
        )
        self.lifecycle_eval = MemoryLifecycleEvalService(
            self._conn,
            lock=self._lock,
            run_lifecycle=self.run_lifecycle,
            lifecycle_health=self.lifecycle.lifecycle_health,
            list_states=self.list_lifecycle_states,
            list_tombstones=self.list_tombstones,
        )
        self.gate_pipeline = MemoryGatePipeline(
            self._conn,
            lock=self._lock,
            get_candidate=self.get_candidate,
            candidate_evidence=self._candidate_evidence,
            commit_candidates=lambda candidate_ids, actor: self.commit(
                candidate_ids,
                actor=actor,
            ),
            reject_candidate=lambda candidate_id, reason: self.reject(
                candidate_id,
                reason=reason,
            ),
        )
        self.quality_ops = MemoryQualityOpsService(
            self._conn,
            lock=self._lock,
            store_health=self.store_health,
            module_versions=self._memory_module_versions,
        )
        self._register_quality_ops()

    @classmethod
    def default(cls) -> "MemoryRuntime":
        runtime = cls(workspace_state_dir() / "memory.db")
        runtime.seed_if_empty(load_jsonl(SEEDS_DIR / "memory.seed.jsonl"))
        return runtime

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            init_memory_schema(self._conn)

    def _memory_module_versions(self) -> JsonDict:
        return {
            "memory_quality_ops": "v1",
            "canonical_eval": "v1",
            "recall_eval": "v1",
            "planner_eval": "v1",
            "surface_eval": "v1",
            "lifecycle_eval": "v1",
            "projection_gateway": "v1",
        }

    def _register_quality_ops(self) -> None:
        self.quality_ops.register_eval_suite(
            "canonical",
            self.run_canonical_eval,
            source_module="memory_canonical_eval",
        )
        self.quality_ops.register_eval_suite(
            "recall",
            self.run_recall_eval,
            source_module="memory_recall_eval_service",
        )
        self.quality_ops.register_eval_suite(
            "planner",
            self.run_planner_eval,
            source_module="memory_recall_planner_eval",
        )
        self.quality_ops.register_eval_suite(
            "surface",
            self.run_surface_eval,
            source_module="memory_surface_eval",
        )
        self.quality_ops.register_eval_suite(
            "lifecycle",
            self.run_lifecycle_eval,
            source_module="memory_lifecycle_eval",
        )
        self.quality_ops.register_eval_suite(
            "projection_gateway",
            self._run_projection_gateway_eval,
            source_module="memory_projector",
        )
        self.quality_ops.register_admin_action(
            AdminActionSpec(
                name="process_index_outbox",
                risk_level="medium",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["memory_index_outbox", "memory_fts", "memory_index_document"],
                        "limit": int(params.get("limit") or 100),
                        "pending": self.index_outbox.pending_count(),
                    }
                },
                execute=lambda params: self.process_index_outbox(limit=int(params.get("limit") or 100)),
                rollback_plan=lambda params: {
                    "type": "rebuild_indexes",
                    "note": "Index outbox processing is idempotent; rebuild indexes if an index backend diverges.",
                },
                description="Process pending memory index outbox items.",
            )
        )
        self.quality_ops.register_admin_action(
            AdminActionSpec(
                name="process_projector_outbox",
                risk_level="medium",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["memory_projector_outbox", "memory_projection_snapshot"],
                        "limit": int(params.get("limit") or 100),
                        "pending": self.projector_bus.pending_count(),
                    }
                },
                execute=lambda params: self.process_projector_outbox(limit=int(params.get("limit") or 100)),
                rollback_plan=lambda params: {
                    "type": "projection_rebuild",
                    "note": "Projector events are idempotent; run projection rebuild for affected subsystems if needed.",
                },
                description="Process pending projection gateway outbox items.",
            )
        )
        self.quality_ops.register_admin_action(
            AdminActionSpec(
                name="forget_memory",
                risk_level="high",
                preview=lambda params: self._preview_forget("memory", str(params.get("memory_id") or "")),
                execute=lambda params: self.forget_memory(
                    str(params["memory_id"]),
                    mode=str(params.get("mode") or "delete"),
                    reason=str(params.get("reason") or "admin_forget_memory"),
                    actor=str(params.get("actor") or "admin_action"),
                ),
                rollback_plan=lambda params: {
                    "type": "not_fully_reversible",
                    "note": "Forget creates tombstones and delete propagation; restore requires explicit user approval and preserved backups.",
                },
                description="Request forget for a committed memory.",
            )
        )
        self.quality_ops.register_admin_action(
            AdminActionSpec(
                name="forget_evidence",
                risk_level="high",
                preview=lambda params: self._preview_forget("evidence", str(params.get("evidence_id") or "")),
                execute=lambda params: self.forget_evidence(
                    str(params["evidence_id"]),
                    mode=str(params.get("mode") or "delete"),
                    reason=str(params.get("reason") or "admin_forget_evidence"),
                    actor=str(params.get("actor") or "admin_action"),
                ),
                rollback_plan=lambda params: {
                    "type": "not_fully_reversible",
                    "note": "Evidence deletion may redact source material; use backups or retained raw pointers only with consent.",
                },
                description="Request forget for an evidence record.",
            )
        )
        self.quality_ops.register_admin_action(
            AdminActionSpec(
                name="rollback_candidate",
                risk_level="high",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["memory_record", "memory_revision", "memory_index_outbox"],
                        "candidate_id": params.get("candidate_id"),
                    }
                },
                execute=lambda params: self.rollback_committed_candidate(
                    str(params["candidate_id"]),
                    reason=str(params.get("reason") or "admin_rollback_candidate"),
                ),
                rollback_plan=lambda params: {
                    "type": "manual_review",
                    "note": "Rollback produces a revision trail; recommit the candidate only after review.",
                },
                description="Rollback a committed memory candidate.",
            )
        )
        self.quality_ops.register_admin_action(
            AdminActionSpec(
                name="force_commit_candidate",
                risk_level="high",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["memory_candidate", "memory_record", "memory_revision"],
                        "candidate_ids": params.get("candidate_ids") or [params.get("candidate_id")],
                    }
                },
                execute=lambda params: self.commit(
                    [
                        str(item)
                        for item in (
                            params.get("candidate_ids")
                            or [params.get("candidate_id")]
                        )
                        if item
                    ],
                    actor=str(params.get("actor") or "admin_action"),
                ),
                rollback_plan=lambda params: {
                    "type": "rollback_candidate",
                    "note": "Use rollback_candidate on each committed candidate if force commit was wrong.",
                },
                description="Force commit candidate ids through the canonical store.",
            )
        )
        self.quality_ops.register_admin_action(
            AdminActionSpec(
                name="force_reject_candidate",
                risk_level="high",
                preview=lambda params: {
                    "impact_summary": {
                        "mutates": ["memory_candidate"],
                        "candidate_id": params.get("candidate_id"),
                    }
                },
                execute=lambda params: self.reject(
                    str(params["candidate_id"]),
                    reason=str(params.get("reason") or "admin_force_reject"),
                ),
                rollback_plan=lambda params: {
                    "type": "manual_reproposal",
                    "note": "Rejected candidates are not auto-restored; repropose from evidence if needed.",
                },
                description="Force reject a candidate.",
            )
        )
        self.quality_ops.register_admin_action(
            AdminActionSpec(
                name="delete_evidence",
                risk_level="high",
                preview=lambda params: self._preview_forget("evidence", str(params.get("evidence_id") or "")),
                execute=lambda params: self.delete_evidence(
                    str(params["evidence_id"]),
                    reason=str(params.get("reason") or "admin_delete_evidence"),
                    actor=str(params.get("actor") or "admin_action"),
                ),
                rollback_plan=lambda params: {
                    "type": "not_fully_reversible",
                    "note": "Direct evidence deletion should be rare; prefer forget_evidence for governed propagation.",
                },
                description="Delete evidence directly with propagation.",
            )
        )

    def _preview_forget(self, target_type: str, target_id: str) -> JsonDict:
        exists = False
        if target_type == "memory" and target_id:
            with self._lock:
                exists = bool(
                    self._conn.execute(
                        "SELECT 1 FROM memory_record WHERE id = ?",
                        (target_id,),
                    ).fetchone()
                )
        if target_type == "evidence" and target_id:
            with self._lock:
                exists = bool(
                    self._conn.execute(
                        "SELECT 1 FROM evidence WHERE id = ?",
                        (target_id,),
                    ).fetchone()
                )
        return {
            "impact_summary": {
                "mutates": [
                    "memory_forget_request",
                    "memory_tombstone",
                    "delete_propagation",
                    "memory_record" if target_type == "memory" else "evidence",
                ],
                "target_type": target_type,
                "target_id": target_id,
                "target_exists": exists,
            }
        }

    def _run_projection_gateway_eval(self) -> JsonDict:
        health = self.store_health().get("projectors", {})
        cases = [
            {
                "case_name": "projector_outbox_has_no_failed_events",
                "status": "passed" if int(health.get("failed", 0)) == 0 else "failed",
                "details": {"failed": health.get("failed", 0)},
            },
            {
                "case_name": "projector_outbox_has_no_dead_letters",
                "status": "passed" if int(health.get("dead_letter", 0)) == 0 else "failed",
                "details": {"dead_letter": health.get("dead_letter", 0)},
            },
            {
                "case_name": "projector_checkpoints_are_readable",
                "status": "passed" if isinstance(health.get("checkpoints", []), list) else "failed",
                "details": {"checkpoint_count": len(health.get("checkpoints", []))},
            },
        ]
        failed = [case for case in cases if case["status"] != "passed"]
        return {
            "run_id": _new_id("projection_eval"),
            "status": "failed" if failed else "passed",
            "total_cases": len(cases),
            "passed_cases": len(cases) - len(failed),
            "failed_cases": len(failed),
            "summary": {"projector_health": health},
            "cases": cases,
        }

    def reset(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                DELETE FROM recall_trace_item;
                DELETE FROM memory_quality_metric_point;
                DELETE FROM memory_quality_case_result;
                DELETE FROM memory_quality_baseline;
                DELETE FROM memory_quality_run;
                DELETE FROM memory_trace_index;
                DELETE FROM memory_admin_action;
                DELETE FROM memory_quality_event;
                DELETE FROM memory_quality_setting;
                DELETE FROM memory_surface_policy_item;
                DELETE FROM memory_surface_policy_trace;
                DELETE FROM memory_surface_feedback;
                DELETE FROM memory_surface_consent;
                DELETE FROM memory_surface_override;
                DELETE FROM recall_trace;
                DELETE FROM memory_revision;
                DELETE FROM memory_recall_eval_case;
                DELETE FROM memory_recall_eval_run;
                DELETE FROM memory_planner_eval_case;
                DELETE FROM memory_planner_eval_run;
                DELETE FROM memory_surface_eval_case;
                DELETE FROM memory_surface_eval_run;
                DELETE FROM recall_plan_cache;
                DELETE FROM recall_plan_trace;
                DELETE FROM memory_canonical_eval_case;
                DELETE FROM memory_canonical_eval_run;
                DELETE FROM memory_forget_request;
                DELETE FROM memory_lifecycle_eval_case;
                DELETE FROM memory_lifecycle_eval_run;
                DELETE FROM memory_consolidation_plan;
                DELETE FROM memory_heat_snapshot;
                DELETE FROM memory_heat_run;
                DELETE FROM memory_lifecycle_state;
                DELETE FROM memory_tombstone;
                DELETE FROM memory_projection_audit;
                DELETE FROM memory_projection_proposal;
                DELETE FROM memory_projection_snapshot;
                DELETE FROM memory_projection_rebuild_run;
                DELETE FROM memory_projector_checkpoint;
                DELETE FROM memory_projector_outbox;
                DELETE FROM memory_index_rebuild_run;
                DELETE FROM memory_index_outbox;
                DELETE FROM memory_access_signal;
                DELETE FROM memory_graph_edge;
                DELETE FROM memory_vector_index;
                DELETE FROM memory_embedding;
                DELETE FROM memory_index_document;
                DELETE FROM memory_review_item;
                DELETE FROM memory_quarantine;
                DELETE FROM memory_gate_decision;
                DELETE FROM memory_candidate;
                DELETE FROM memory_evidence;
                DELETE FROM memory_fts;
                DELETE FROM memory_record;
                DELETE FROM evidence;
                DELETE FROM delete_propagation;
                DELETE FROM extraction_job;
                DELETE FROM memory_event;
                DELETE FROM memory_episode;
                """
            )
            self._conn.commit()
        self.seed_if_empty(load_jsonl(SEEDS_DIR / "memory.seed.jsonl"))

    def seed_if_empty(self, items: list[JsonDict]) -> None:
        with self._lock:
            count = self._conn.execute("SELECT COUNT(*) FROM memory_record").fetchone()[0]
        if count:
            return
        for item in items:
            evidence_id = self.add_evidence(
                source_type="seed",
                content_summary=item.get("content", ""),
                sensitivity=item.get("sensitivity", "low"),
                retention_policy="persistent",
                metadata={"seed_id": item.get("id")},
            )
            self._create_memory(
                memory_id=str(item.get("id") or _new_id("mem")),
                memory_type=item.get("type", "preference"),
                summary=item.get("content", ""),
                tags=item.get("tags", []),
                evidence_ids=[evidence_id],
                confidence=1.0,
                importance=0.65,
                sensitivity=item.get("sensitivity", "low"),
                reason="seed_import",
                actor="system_seed",
            )

    def import_legacy_memories(self, items: list[JsonDict]) -> int:
        """Import pre-runtime JSON memories once, preserving their public ids."""
        imported = 0
        for item in items:
            if item.get("_projection") == "memory_runtime":
                continue
            memory_id = str(item.get("id") or _new_id("mem"))
            with self._lock:
                exists = self._conn.execute(
                    "SELECT 1 FROM memory_record WHERE id = ?",
                    (memory_id,),
                ).fetchone()
            if exists:
                continue
            summary = str(item.get("content") or item.get("summary") or "").strip()
            sensitivity = str(item.get("sensitivity") or _detect_sensitivity(summary))
            if not summary or sensitivity == "forbidden":
                continue
            memory_type = str(item.get("type") or "preference")
            with self._lock:
                duplicate = self._conn.execute(
                    """
                    SELECT 1 FROM memory_record
                    WHERE owner_user_id = ?
                      AND agent_id = ?
                      AND type = ?
                      AND summary = ?
                      AND status = 'active'
                    """,
                    (self.owner_user_id, self.agent_id, memory_type, summary),
                ).fetchone()
            if duplicate:
                continue
            evidence_id = self.add_evidence(
                source_type="legacy_state_import",
                content_summary=summary,
                sensitivity=sensitivity,
                retention_policy=self.gate.retention_policy(sensitivity),
                metadata={"legacy_memory_id": memory_id},
            )
            self._create_memory(
                memory_id=memory_id,
                memory_type=memory_type,
                summary=summary,
                tags=list(item.get("tags") or []),
                evidence_ids=[evidence_id],
                confidence=float(item.get("confidence", 1.0)),
                importance=float(item.get("importance", 0.65)),
                sensitivity=sensitivity,
                reason="legacy_state_import",
                actor="migration",
                supersedes_id=item.get("supersedes_id"),
            )
            status = str(item.get("status") or "active")
            if status != "active":
                self._set_status(
                    memory_id,
                    status,
                    reason="legacy_state_status_import",
                    actor="migration",
                )
            imported += 1
        return imported

    def add_evidence(
        self,
        *,
        source_type: str,
        content_summary: str,
        raw_content: str | None = None,
        raw_pointer: str | None = None,
        source_ref: str | None = None,
        span: JsonDict | None = None,
        expires_at: str | None = None,
        sensitivity: str = "low",
        retention_policy: str = "persistent",
        visibility: str = "private",
        metadata: JsonDict | None = None,
    ) -> str:
        if sensitivity == "forbidden":
            raise ValueError("forbidden content must not be persisted")
        evidence_id = _new_id("ev")
        stored_raw_content = raw_content if raw_content is not None else content_summary
        content_hash = _content_hash(content_summary, stored_raw_content, raw_pointer)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO evidence (
                    id, source_type, content_summary, raw_content, raw_pointer, source_ref,
                    span_json, content_hash, visibility, sensitivity, retention_policy,
                    expires_at, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    source_type,
                    content_summary,
                    stored_raw_content,
                    raw_pointer,
                    source_ref,
                    _json(span or {}),
                    content_hash,
                    visibility,
                    sensitivity,
                    retention_policy,
                    expires_at,
                    _json(metadata or {}),
                    _utc_now(),
                ),
            )
            self._conn.commit()
        return evidence_id

    def propose(
        self,
        *,
        summary: str,
        memory_type: str,
        evidence_ids: list[str],
        tags: list[str] | None = None,
        confidence: float = 0.8,
        sensitivity: str | None = None,
        reason: str,
        explicit_user_request: bool = False,
        supersedes_id: str | None = None,
        operation: str = "ADD",
        target_memory_id: str | None = None,
        importance: float = 0.5,
        stability: float = 0.5,
        specificity: float = 0.5,
        novelty: float = 1.0,
        conflict_score: float = 0.0,
        extraction_job_id: str | None = None,
        schema_version: str = "legacy",
        trace: JsonDict | None = None,
        allow_auto_commit: bool = True,
    ) -> JsonDict:
        cleaned = summary.strip()
        operation = operation.upper()
        if target_memory_id and not supersedes_id and operation == "UPDATE":
            supersedes_id = target_memory_id
        detected = sensitivity or _detect_sensitivity(cleaned)
        if detected == "forbidden":
            return {
                "candidate_id": None,
                "status": "discarded",
                "reason": "forbidden_secret_not_persisted",
            }
        tombstone = self.lifecycle.find_tombstone_match(cleaned)
        if tombstone is not None:
            return {
                "candidate_id": None,
                "status": "discarded",
                "reason": "tombstone_prevents_relearning",
                "tombstone": tombstone,
            }
        candidate_id = _new_id("mc")
        local_only = detected == "sensitive"
        candidate_hash = compute_candidate_hash(
            {
                "operation": operation,
                "type": memory_type,
                "summary": cleaned,
                "target_memory_id": target_memory_id,
                "supersedes_id": supersedes_id,
            }
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_candidate (
                    id, type, summary, tags_json, status, confidence, sensitivity,
                    reason, evidence_ids_json, supersedes_id, explicit_user_request,
                    local_only, created_at, operation, target_memory_id, importance,
                    stability, specificity, novelty, conflict_score, extraction_job_id,
                    schema_version, candidate_hash, trace_json
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    memory_type,
                    cleaned,
                    _json(tags or []),
                    confidence,
                    detected,
                    reason,
                    _json(evidence_ids),
                    supersedes_id,
                    int(explicit_user_request),
                    int(local_only),
                    _utc_now(),
                    operation,
                    target_memory_id,
                    importance,
                    stability,
                    specificity,
                    novelty,
                    conflict_score,
                    extraction_job_id,
                    schema_version,
                    candidate_hash,
                    _json(trace or {}),
                ),
            )
            self._conn.commit()
        candidate = self.get_candidate(candidate_id)
        if allow_auto_commit:
            gate_result = self.run_candidate_gate(candidate_id, actor="explicit_user_request")
            if gate_result.get("committed"):
                return gate_result["committed"][0]
            return gate_result["candidate"]
        return candidate

    def observe_turn(self, user_input: str, reply: str) -> JsonDict | None:
        candidates = self.observe_turn_candidates(user_input, reply)
        return candidates[0] if candidates else None

    def observe_turn_candidates(self, user_input: str, reply: str) -> list[JsonDict]:
        if not user_input.strip():
            return []
        updates: list[JsonDict] = []
        with self._lock:
            ingest_trace = self.ingestion.ingest_turn(
                user_input=user_input,
                reply=reply,
                session_id="default",
            )
            self._conn.commit()
        episode = ingest_trace["episode"]
        event_ids = [event["event_id"] for event in ingest_trace["events"]]
        extraction_job = ingest_trace["extraction_job"]
        extraction_job_id = extraction_job["job_id"] if extraction_job else None
        for extraction in extract_memory_candidates(user_input, reply):
            sensitivity = _detect_sensitivity(extraction.content)
            if sensitivity == "forbidden":
                updates.append(
                    {
                        "candidate_id": None,
                        "status": "discarded",
                        "reason": "forbidden_secret_not_persisted",
                    }
                )
                continue
            memory_type = (
                _classify_memory_type(extraction.content)
                if extraction.memory_type == "auto"
                else extraction.memory_type
            )
            evidence_id = self.add_evidence(
                source_type="chat",
                content_summary=extraction.content,
                raw_content=user_input,
                source_ref=event_ids[0] if event_ids else episode["episode_id"],
                sensitivity=sensitivity,
                retention_policy=self.gate.retention_policy(sensitivity),
                metadata={
                    "episode_id": episode["episode_id"],
                    "event_ids": event_ids,
                    "extraction_job_id": extraction_job_id,
                    "pre_filter": ingest_trace.get("pre_filter"),
                    "reply_summary": reply[:160],
                    "extraction_reason": extraction.reason,
                    "explicit_user_request": extraction.explicit_user_request,
                },
            )
            updates.append(
                self.propose(
                    summary=extraction.content,
                    memory_type=memory_type,
                    evidence_ids=[evidence_id],
                    tags=list(extraction.tags),
                    confidence=extraction.confidence,
                    sensitivity=sensitivity,
                    reason=extraction.reason,
                    explicit_user_request=extraction.explicit_user_request,
                )
            )
        return updates

    def ingest_turn(
        self,
        user_input: str,
        reply: str,
        *,
        session_id: str = "default",
        turn_id: str | None = None,
        episode_strategy: str = "turn",
        seal: bool | None = None,
        now: str | None = None,
    ) -> JsonDict:
        with self._lock:
            result = self.ingestion.ingest_turn(
                user_input=user_input,
                reply=reply,
                session_id=session_id,
                turn_id=turn_id,
                episode_strategy=episode_strategy,
                seal=seal,
                now=now,
            )
            self._conn.commit()
            return result

    def observe_response_plan(self, response_plan: JsonDict) -> JsonDict | None:
        hook = response_plan.get("memory_hook_candidate")
        if not hook:
            return None
        summary = hook.get("episode_summary") or hook.get("content")
        if not summary:
            return None
        evidence_id = self.add_evidence(
            source_type=str(hook.get("source", "response_plan")),
            content_summary=summary,
            source_ref=str(hook.get("event_id")) if hook.get("event_id") else None,
            sensitivity="low",
            metadata={"hook_id": hook.get("candidate_id"), "event_id": hook.get("event_id")},
        )
        return self.propose(
            summary=summary,
            memory_type="worldops_episode"
            if hook.get("source") == "worldops_micro_quest"
            else "episode",
            evidence_ids=[evidence_id],
            tags=["memory_hook", str(hook.get("event_id") or "")],
            confidence=0.8,
            reason="response_plan_memory_hook",
        )

    def list_events(self, *, episode_id: str | None = None, limit: int = 50) -> list[JsonDict]:
        with self._lock:
            return self.ingestion.list_events(episode_id=episode_id, limit=limit)

    def list_episodes(self, *, status: str | None = None, limit: int = 50) -> list[JsonDict]:
        with self._lock:
            return self.ingestion.list_episodes(status=status, limit=limit)

    def list_extraction_jobs(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[JsonDict]:
        with self._lock:
            return self.ingestion.list_extraction_jobs(status=status, limit=limit)

    def list_delete_propagations(
        self,
        *,
        source_ref: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        with self._lock:
            return self.ingestion.list_delete_propagations(
                source_ref=source_ref,
                target_type=target_type,
                target_id=target_id,
                limit=limit,
            )

    def get_episode_trace(self, episode_id: str) -> JsonDict:
        with self._lock:
            trace = self.ingestion.get_episode_trace(episode_id)
            evidence_rows = self._conn.execute(
                """
                SELECT * FROM evidence
                WHERE metadata_json LIKE ?
                ORDER BY created_at ASC
                """,
                (f"%{episode_id}%",),
            ).fetchall()
            evidence = [evidence_from_row(row) for row in evidence_rows]
            evidence_ids = {item["id"] for item in evidence}
            if evidence_ids:
                candidate_rows = self._conn.execute(
                    "SELECT * FROM memory_candidate ORDER BY created_at ASC"
                ).fetchall()
                candidates = []
                for row in candidate_rows:
                    candidate = self._candidate_from_row(row)
                    if evidence_ids.intersection(candidate["evidence_ids"]):
                        candidates.append(candidate)
                trace["evidence"] = evidence
                trace["candidates"] = candidates
            else:
                trace["evidence"] = []
                trace["candidates"] = []
            memories: list[JsonDict] = []
            revisions: list[JsonDict] = []
            memory_ids = {
                candidate["committed_memory_id"]
                for candidate in trace["candidates"]
                if candidate.get("committed_memory_id")
            }
            if evidence_ids:
                rows = self._conn.execute(
                    """
                    SELECT DISTINCT memory_id
                    FROM memory_evidence
                    WHERE evidence_id IN (%s)
                    """
                    % ",".join("?" for _ in evidence_ids),
                    sorted(evidence_ids),
                ).fetchall()
                memory_ids.update(row["memory_id"] for row in rows)
            for memory_id in sorted(memory_ids):
                memory = self.get_memory(memory_id)
                memories.append(memory)
                revisions.extend(self.revisions_for_memory(memory_id))
            trace["memories"] = memories
            trace["revisions"] = revisions
            return trace

    def replay_episode(self, episode_id: str) -> JsonDict:
        trace = self.get_episode_trace(episode_id)
        trace["replay_contract"] = {
            "source_of_truth": "sqlite",
            "contains_raw_events": True,
            "contains_raw_evidence": True,
            "contains_delete_propagation": True,
        }
        return trace

    def seal_episode(self, episode_id: str, *, reason: str = "manual_seal") -> JsonDict:
        with self._lock:
            episode = self.ingestion.seal_episode(episode_id, reason=reason)
            episode, job, decision = self.ingestion.enqueue_extraction_if_eligible(
                episode_id,
                reason=reason,
            )
            self._conn.commit()
            return {
                "episode": episode,
                "extraction_job": job,
                "pre_filter": {
                    "should_extract": decision.should_extract,
                    "reason": decision.reason,
                    "priority": decision.priority,
                    "archived": decision.archived,
                },
            }

    def start_extraction_job(self, job_id: str) -> JsonDict:
        with self._lock:
            job = self.ingestion.start_extraction_job(job_id)
            self._conn.commit()
            return job

    def claim_next_extraction_job(self) -> JsonDict | None:
        with self._lock:
            job = self.ingestion.claim_next_extraction_job()
            self._conn.commit()
            return job

    def complete_extraction_job(
        self,
        job_id: str,
        *,
        result_summary: str | None = None,
    ) -> JsonDict:
        with self._lock:
            job = self.ingestion.complete_extraction_job(
                job_id,
                result_summary=result_summary,
            )
            self._conn.commit()
            return job

    def fail_extraction_job(
        self,
        job_id: str,
        *,
        error: str,
        retry: bool = False,
        retry_delay_seconds: int = 60,
    ) -> JsonDict:
        with self._lock:
            job = self.ingestion.fail_extraction_job(
                job_id,
                error=error,
                retry=retry,
                retry_delay_seconds=retry_delay_seconds,
            )
            self._conn.commit()
            return job

    def build_extraction_input(self, job_id: str, *, related_top_k: int = 8) -> JsonDict:
        with self._lock:
            job = self.ingestion.get_extraction_job(job_id)
            trace = self.get_episode_trace(job["episode_id"])
            query_text = self._episode_query_text(trace)
            related_memories = self.related_memories_for_extraction(query_text, top_k=related_top_k)
            return {
                "schema_version": "extractor_input.v1",
                "job": job,
                "episode": trace["episode"],
                "events": trace["events"],
                "related_memories": related_memories,
                "policy": {
                    "auto_commit": {
                        "confidence_min": 0.90,
                        "sensitivity": "low",
                        "allowed_operations": ["ADD", "MERGE"],
                        "excluded_types": [
                            "relationship_signal",
                            "shared_episode",
                            "persona_delta",
                        ],
                        "conflict_score_max": 0.30,
                    }
                },
                "trace": {
                    "delete_propagations": trace.get("delete_propagations", []),
                    "query_text": query_text,
                },
            }

    def related_memories_for_extraction(self, query_text: str, *, top_k: int = 8) -> list[JsonDict]:
        return self.retrieval.related_memories(query_text, top_k=top_k)

    def materialize_extractor_result(
        self,
        result: ExtractorResult,
        *,
        input_package: JsonDict,
        auto_commit: bool = True,
    ) -> JsonDict:
        materialized: list[JsonDict] = []
        committed: list[JsonDict] = []
        noop_candidates: list[JsonDict] = []
        event_by_id = {
            str(event.get("event_id") or event.get("id")): event
            for event in input_package.get("events", [])
        }
        for draft in result.candidates:
            if draft.operation == WriteOperation.NOOP:
                noop_candidates.append(candidate_to_dict(draft))
                continue
            sensitivity = draft.sensitivity or _detect_sensitivity(draft.summary)
            if sensitivity == "forbidden":
                noop_candidates.append({**candidate_to_dict(draft), "discarded_reason": "forbidden"})
                continue
            raw_content = draft.raw_quote or self._evidence_text_for_refs(draft, event_by_id)
            evidence_id = self.add_evidence(
                source_type="llm_extractor",
                content_summary=draft.summary,
                raw_content=raw_content,
                source_ref=self._source_ref_for_refs(draft) or result.episode_id,
                span=self._span_for_refs(draft),
                sensitivity=sensitivity,
                retention_policy=self.gate.retention_policy(sensitivity),
                visibility="private",
                metadata={
                    "episode_id": result.episode_id,
                    "extraction_job_id": result.job_id,
                    "operation": draft.operation.value,
                    "target_memory_id": draft.target_memory_id,
                    "evidence_refs": [
                        {
                            "event_id": ref.event_id,
                            "episode_id": ref.episode_id,
                            "speaker": ref.speaker,
                            "span": list(ref.span) if ref.span else None,
                        }
                        for ref in draft.evidence_refs
                    ],
                    "extractor_model": result.model,
                    "extractor_trace": result.trace,
                },
            )
            candidate = self.propose(
                summary=draft.summary,
                memory_type=draft.memory_type,
                evidence_ids=[evidence_id],
                tags=draft.tags,
                confidence=draft.scores.confidence,
                sensitivity=sensitivity,
                reason=draft.reason,
                explicit_user_request=False,
                supersedes_id=(
                    draft.target_memory_id if draft.operation == WriteOperation.UPDATE else None
                ),
                operation=draft.operation.value,
                target_memory_id=draft.target_memory_id,
                importance=draft.scores.importance,
                stability=draft.scores.stability,
                specificity=draft.scores.specificity,
                novelty=draft.scores.novelty,
                conflict_score=draft.scores.conflict_score,
                extraction_job_id=result.job_id,
                schema_version=result.schema_version,
                trace={
                    "extractor_model": result.model,
                    "extractor_trace": result.trace,
                    "raw_candidate": candidate_to_dict(draft),
                },
                allow_auto_commit=False,
            )
            materialized.append(candidate)
            if auto_commit:
                gate_result = self.run_candidate_gate(candidate["candidate_id"], actor="async_extractor")
                materialized[-1] = gate_result.get("candidate", candidate)
                committed.extend(gate_result.get("committed", []))
        return {
            "status": "materialized",
            "candidates": materialized,
            "committed": committed,
            "noop_candidates": noop_candidates,
        }

    def run_extraction_once(self, *, provider: Any | None = None, auto_commit: bool = True) -> JsonDict:
        from .memory_extractor_worker import AsyncMemoryExtractorWorker

        return AsyncMemoryExtractorWorker(self, provider=provider).run_once(auto_commit=auto_commit)

    def backfill_extraction_jobs(
        self,
        *,
        limit: int = 50,
        force: bool = False,
        reason: str = "backfill",
    ) -> JsonDict:
        with self._lock:
            if force:
                rows = self._conn.execute(
                    """
                    SELECT * FROM memory_episode
                    WHERE status IN ('sealed', 'extracted') AND deleted_at IS NULL
                    ORDER BY updated_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT e.*
                    FROM memory_episode e
                    WHERE e.status IN ('sealed', 'extracted')
                      AND e.deleted_at IS NULL
                      AND NOT EXISTS (
                        SELECT 1 FROM extraction_job j WHERE j.episode_id = e.id
                      )
                    ORDER BY e.updated_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            jobs = [
                self.ingestion.enqueue_extraction(
                    row["id"],
                    reason=reason if not force else f"{reason}:force",
                    priority=45,
                )
                for row in rows
            ]
            self._conn.commit()
        return {
            "status": "ok",
            "force": force,
            "requested_limit": limit,
            "created_jobs": jobs,
            "created_count": len(jobs),
        }

    def extraction_observability(self, *, limit: int = 10) -> JsonDict:
        with self._lock:
            job_status_rows = self._conn.execute(
                "SELECT status, COUNT(*) AS count FROM extraction_job GROUP BY status"
            ).fetchall()
            candidate_status_rows = self._conn.execute(
                """
                SELECT status, operation, COUNT(*) AS count
                FROM memory_candidate
                WHERE extraction_job_id IS NOT NULL
                GROUP BY status, operation
                """
            ).fetchall()
            recent_jobs = self._conn.execute(
                """
                SELECT * FROM extraction_job
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            trace_rows = self._conn.execute(
                """
                SELECT trace_json
                FROM memory_candidate
                WHERE extraction_job_id IS NOT NULL
                """
            ).fetchall()
            gate_rows = self._conn.execute(
                "SELECT decision, COUNT(*) AS count FROM memory_gate_decision GROUP BY decision"
            ).fetchall()
            review_rows = self._conn.execute(
                """
                SELECT status, queue, COUNT(*) AS count
                FROM memory_review_item
                GROUP BY status, queue
                """
            ).fetchall()
            quarantine_rows = self._conn.execute(
                """
                SELECT status, severity, COUNT(*) AS count
                FROM memory_quarantine
                GROUP BY status, severity
                """
            ).fetchall()
        total_tokens = 0
        estimated_cost = 0.0
        latencies: list[float] = []
        providers: dict[str, int] = {}
        for row in trace_rows:
            trace = _loads(row["trace_json"], {})
            extractor_trace = trace.get("extractor_trace", {})
            usage = extractor_trace.get("usage", {})
            cost = extractor_trace.get("cost", {})
            total_tokens += int(usage.get("total_tokens") or cost.get("total_tokens") or 0)
            estimated_cost += float(cost.get("estimated_cost") or 0.0)
            if extractor_trace.get("latency_ms") is not None:
                latencies.append(float(extractor_trace["latency_ms"]))
            provider = str(extractor_trace.get("provider") or trace.get("extractor_model", {}).get("provider") or "unknown")
            providers[provider] = providers.get(provider, 0) + 1
        return {
            "jobs_by_status": {row["status"]: row["count"] for row in job_status_rows},
            "candidates_by_status_operation": [
                {"status": row["status"], "operation": row["operation"], "count": row["count"]}
                for row in candidate_status_rows
            ],
            "llm_usage": {
                "total_tokens": total_tokens,
                "estimated_cost": round(estimated_cost, 8),
                "latency_ms_avg": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
                "providers": providers,
            },
            "gate_decisions_by_decision": {
                row["decision"]: row["count"] for row in gate_rows
            },
            "reviews_by_status_queue": [
                {"status": row["status"], "queue": row["queue"], "count": row["count"]}
                for row in review_rows
            ],
            "quarantine_by_status_severity": [
                {"status": row["status"], "severity": row["severity"], "count": row["count"]}
                for row in quarantine_rows
            ],
            "recent_jobs": [self.ingestion.get_extraction_job(row["id"]) for row in recent_jobs],
        }

    def propagate_event_delete(
        self,
        source_ref: str,
        *,
        reason: str = "source_deleted",
    ) -> JsonDict:
        with self._lock:
            result = self.ingestion.propagate_delete(source_ref, reason=reason)
            cascade = self._cascade_delete_from_event(result["deleted_event"], reason=reason)
            result.update(cascade)
            self._conn.commit()
            return result

    @staticmethod
    def _episode_query_text(trace: JsonDict) -> str:
        parts = [str(trace.get("episode", {}).get("gist") or "")]
        for event in trace.get("events", []):
            payload = event.get("payload", {})
            if isinstance(payload, dict):
                parts.append(str(payload.get("content") or ""))
            else:
                parts.append(str(payload))
        return "\n".join(part for part in parts if part)

    @staticmethod
    def _evidence_text_for_refs(draft: Any, event_by_id: dict[str, JsonDict]) -> str:
        parts: list[str] = []
        for ref in draft.evidence_refs:
            event = event_by_id.get(ref.event_id)
            if not event:
                continue
            payload = event.get("payload", {})
            text = str(payload.get("content") or payload if isinstance(payload, dict) else payload)
            if ref.span:
                start, end = ref.span
                text = text[start:end]
            parts.append(text)
        return "\n".join(part for part in parts if part) or draft.summary

    @staticmethod
    def _source_ref_for_refs(draft: Any) -> str | None:
        for ref in draft.evidence_refs:
            if ref.event_id:
                return ref.event_id
        return None

    @staticmethod
    def _span_for_refs(draft: Any) -> JsonDict:
        refs = [
            {
                "event_id": ref.event_id,
                "episode_id": ref.episode_id,
                "speaker": ref.speaker,
                "span": list(ref.span) if ref.span else None,
            }
            for ref in draft.evidence_refs
        ]
        return {"refs": refs} if refs else {}

    def _cascade_delete_from_event(self, event: JsonDict, *, reason: str) -> JsonDict:
        event_id = event["event_id"]
        episode_id = event.get("episode_id")
        evidence_rows = self._conn.execute(
            "SELECT * FROM evidence WHERE deleted_at IS NULL ORDER BY created_at ASC"
        ).fetchall()
        affected_evidence: list[JsonDict] = []
        for row in evidence_rows:
            evidence = evidence_from_row(row)
            metadata = evidence.get("metadata") or {}
            event_ids = set(str(item) for item in metadata.get("event_ids", []))
            metadata_episode_id = metadata.get("episode_id")
            source_ref = evidence.get("source_ref")
            if (
                event_id not in event_ids
                and episode_id != metadata_episode_id
                and source_ref not in {event_id, episode_id}
            ):
                continue
            self._conn.execute(
                "UPDATE evidence SET deleted_at = ?, delete_reason = ? WHERE id = ?",
                (_utc_now(), reason, evidence["id"]),
            )
            self.ingestion.record_delete_propagation(
                source_ref=event_id,
                source_type="memory_event",
                target_type="evidence",
                target_id=evidence["id"],
                action="soft_delete",
                reason=reason,
                metadata={"episode_id": episode_id},
            )
            affected_evidence.append(evidence_from_row(self._evidence_row(evidence["id"])))

        affected_ids = {item["id"] for item in affected_evidence}
        rejected_candidates: list[JsonDict] = []
        if affected_ids:
            for candidate in self.list_candidates():
                if not affected_ids.intersection(candidate["evidence_ids"]):
                    continue
                if candidate["status"] == "pending":
                    self._conn.execute(
                        """
                        UPDATE memory_candidate
                        SET status = 'rejected', resolved_at = ?, reason = ?
                        WHERE id = ?
                        """,
                        (_utc_now(), f"{reason}:source_evidence_deleted", candidate["candidate_id"]),
                    )
                    candidate = self.get_candidate(candidate["candidate_id"])
                    rejected_candidates.append(candidate)
                    self.ingestion.record_delete_propagation(
                        source_ref=event_id,
                        source_type="memory_event",
                        target_type="memory_candidate",
                        target_id=candidate["candidate_id"],
                        action="reject",
                        reason=reason,
                        metadata={"affected_evidence_ids": sorted(affected_ids)},
                    )

        disabled_memories: list[JsonDict] = []
        if affected_ids:
            rows = self._conn.execute(
                """
                SELECT DISTINCT memory_id
                FROM memory_evidence
                WHERE evidence_id IN (%s)
                """
                % ",".join("?" for _ in affected_ids),
                sorted(affected_ids),
            ).fetchall()
            for row in rows:
                memory = self.get_memory(row["memory_id"])
                active_evidence_count = self._conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM memory_evidence me
                    JOIN evidence e ON e.id = me.evidence_id
                    WHERE me.memory_id = ? AND e.deleted_at IS NULL
                    """,
                    (memory["id"],),
                ).fetchone()[0]
                if memory["status"] == "active" and active_evidence_count == 0:
                    disabled = self._set_status(
                        memory["id"],
                        "disabled",
                        reason=f"{reason}:all_source_evidence_deleted",
                        actor="delete_propagation",
                    )
                    disabled_memories.append(disabled)
                    self.ingestion.record_delete_propagation(
                        source_ref=event_id,
                        source_type="memory_event",
                        target_type="memory_record",
                        target_id=memory["id"],
                        action="disable",
                        reason=reason,
                        metadata={"affected_evidence_ids": sorted(affected_ids)},
                    )
                    self.ingestion.record_delete_propagation(
                        source_ref=event_id,
                        source_type="memory_event",
                        target_type="memory_fts",
                        target_id=memory["id"],
                        action="remove_index",
                        reason=reason,
                    )
                else:
                    self._append_revision(
                        memory["id"],
                        action="evidence_deleted",
                        before=memory,
                        after=self.get_memory(memory["id"]),
                        reason=reason,
                        actor="delete_propagation",
                    )
                    self._publish_memory_change(
                        self.get_memory(memory["id"]),
                        action="upsert",
                        reason=reason,
                        source_ref=event_id,
                    )
                    self.ingestion.record_delete_propagation(
                        source_ref=event_id,
                        source_type="memory_event",
                        target_type="memory_record",
                        target_id=memory["id"],
                        action="mark_evidence_deleted",
                        reason=reason,
                        metadata={"active_evidence_count": active_evidence_count},
                    )

        return {
            "deleted_evidence": affected_evidence,
            "rejected_candidates": rejected_candidates,
            "disabled_memories": disabled_memories,
            "delete_propagations": self.list_delete_propagations(source_ref=event_id),
        }

    def _evidence_row(self, evidence_id: str) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
        if not row:
            raise ValueError(f"evidence not found: {evidence_id}")
        return row

    def _eligible_for_auto_commit(self, candidate: JsonDict) -> bool:
        evidence = self._candidate_evidence(candidate)
        decision = evaluate_candidate_gate(candidate, evidence=evidence)
        return decision.decision == GateDecisionAction.AUTO_COMMIT

    def run_candidate_gate(self, candidate_id: str, *, actor: str = "memory_gate") -> JsonDict:
        return self.gate_pipeline.run_candidate_gate(candidate_id, actor=actor)

    def run_batch_gate(
        self,
        candidate_ids: list[str],
        *,
        actor: str = "memory_gate_batch",
    ) -> JsonDict:
        return self.gate_pipeline.run_batch_gate(candidate_ids, actor=actor)

    def _candidate_evidence(self, candidate: JsonDict) -> list[JsonDict]:
        evidence: list[JsonDict] = []
        for evidence_id in candidate.get("evidence_ids", []):
            try:
                row = self._evidence_row(str(evidence_id))
            except ValueError:
                continue
            item = evidence_from_row(row)
            if not item.get("deleted_at"):
                evidence.append(item)
        return evidence

    def commit(self, candidate_ids: list[str], *, actor: str = "manual_repl") -> JsonDict:
        committed: list[JsonDict] = []
        with self._lock:
            for candidate_id in candidate_ids:
                candidate = self.get_candidate(candidate_id)
                if candidate["status"] == "committed":
                    committed.append(self.get_memory(candidate["committed_memory_id"]))
                    continue
                if candidate["status"] not in {"pending", "needs_review", "deferred"}:
                    raise ValueError(f"candidate is not pending: {candidate_id}")

                operation = str(candidate.get("operation") or "ADD").upper()
                target_memory_id = candidate.get("target_memory_id") or candidate.get("supersedes_id")
                if operation in {"UPDATE", "MERGE", "DELETE"} and not target_memory_id:
                    raise ValueError(
                        f"{operation} candidate requires target_memory_id: {candidate_id}"
                    )
                idempotent_memory = self._idempotent_committed_memory(candidate)
                if idempotent_memory:
                    for evidence_id in candidate["evidence_ids"]:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO memory_evidence (memory_id, evidence_id) VALUES (?, ?)",
                            (idempotent_memory["id"], evidence_id),
                        )
                    self._append_revision(
                        idempotent_memory["id"],
                        action="idempotent_evidence_merged",
                        before=idempotent_memory,
                        after=self.get_memory(idempotent_memory["id"]),
                        reason=f"idempotent_candidate:{candidate_id}",
                        actor=actor,
                    )
                    memory = self.get_memory(idempotent_memory["id"])
                    self._publish_memory_change(
                        memory,
                        action="upsert",
                        reason=f"idempotent_candidate:{candidate_id}",
                        source_ref=candidate_id,
                    )
                elif operation == "DELETE":
                    memory = self._set_status(
                        target_memory_id,
                        "disabled",
                        reason=f"delete_candidate:{candidate_id}:{candidate['reason']}",
                        actor=actor,
                    )
                elif operation == "MERGE" and target_memory_id:
                    memory = self.get_memory(target_memory_id)
                    for evidence_id in candidate["evidence_ids"]:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO memory_evidence (memory_id, evidence_id) VALUES (?, ?)",
                            (target_memory_id, evidence_id),
                        )
                    self._append_revision(
                        target_memory_id,
                        action="evidence_merged",
                        before=memory,
                        after=self.get_memory(target_memory_id),
                        reason=candidate["reason"],
                        actor=actor,
                    )
                    memory = self.get_memory(target_memory_id)
                    self._publish_memory_change(
                        memory,
                        action="upsert",
                        reason=candidate["reason"],
                        source_ref=candidate_id,
                    )
                else:
                    duplicate = self._find_exact_active(candidate["type"], candidate["summary"])
                    if duplicate and not candidate.get("supersedes_id"):
                        for evidence_id in candidate["evidence_ids"]:
                            self._conn.execute(
                                "INSERT OR IGNORE INTO memory_evidence (memory_id, evidence_id) VALUES (?, ?)",
                                (duplicate["id"], evidence_id),
                            )
                        self._append_revision(
                            duplicate["id"],
                            action="evidence_merged",
                            before=duplicate,
                            after=duplicate,
                            reason=candidate["reason"],
                            actor=actor,
                        )
                        memory = self.get_memory(duplicate["id"])
                        self._publish_memory_change(
                            memory,
                            action="upsert",
                            reason=candidate["reason"],
                            source_ref=candidate_id,
                        )
                    else:
                        if candidate.get("supersedes_id"):
                            previous = self.get_memory(candidate["supersedes_id"])
                            self._set_status(
                                previous["id"],
                                "superseded",
                                reason=f"superseded_by_candidate:{candidate_id}",
                                actor=actor,
                            )
                        memory = self._create_memory(
                            memory_id=_new_id("mem"),
                            memory_type=candidate["type"],
                            summary=candidate["summary"],
                            tags=candidate["tags"],
                            evidence_ids=candidate["evidence_ids"],
                            confidence=candidate["confidence"],
                            importance=float(candidate.get("importance") or 0.65),
                            sensitivity=candidate["sensitivity"],
                            reason=candidate["reason"],
                            actor=actor,
                            supersedes_id=candidate.get("supersedes_id"),
                        )

                now = _utc_now()
                self._conn.execute(
                    """
                    UPDATE memory_candidate
                    SET status = 'committed', resolved_at = ?, committed_memory_id = ?
                    WHERE id = ?
                    """,
                    (now, memory["id"], candidate_id),
                )
                self._conn.execute(
                    """
                    UPDATE memory_review_item
                    SET status = 'resolved', resolved_at = ?, resolved_by = ?, resolution = 'committed'
                    WHERE candidate_id = ? AND status = 'open'
                    """,
                    (now, actor, candidate_id),
                )
                committed.append(memory)
            self._conn.commit()
        return {"status": "committed", "committed": committed}

    def _idempotent_committed_memory(self, candidate: JsonDict) -> JsonDict | None:
        candidate_hash = candidate.get("candidate_hash")
        if not candidate_hash:
            return None
        row = self._conn.execute(
            """
            SELECT * FROM memory_candidate
            WHERE candidate_hash = ?
              AND status = 'committed'
              AND committed_memory_id IS NOT NULL
              AND id != ?
            ORDER BY resolved_at DESC
            LIMIT 1
            """,
            (candidate_hash, candidate["candidate_id"]),
        ).fetchone()
        if not row:
            return None
        committed_memory_id = row_value(row, "committed_memory_id")
        if not committed_memory_id:
            return None
        try:
            return self.get_memory(committed_memory_id)
        except ValueError:
            return None

    def reject(self, candidate_id: str, *, reason: str = "manual_reject") -> JsonDict:
        with self._lock:
            candidate = self.get_candidate(candidate_id)
            if candidate["status"] == "committed":
                raise ValueError(f"candidate already committed: {candidate_id}")
            now = _utc_now()
            self._conn.execute(
                """
                UPDATE memory_candidate
                SET status = 'rejected', resolved_at = ?, reason = ?
                WHERE id = ?
                """,
                (now, reason, candidate_id),
            )
            self._conn.execute(
                """
                UPDATE memory_review_item
                SET status = 'resolved', resolved_at = ?, resolved_by = 'memory_runtime',
                    resolution = ?
                WHERE candidate_id = ? AND status = 'open'
                """,
                (now, f"rejected:{reason}", candidate_id),
            )
            self._conn.commit()
        return self.get_candidate(candidate_id)

    def rollback_committed_candidate(
        self,
        candidate_id: str,
        *,
        reason: str = "manual_rollback",
        actor: str = "manual_review",
    ) -> JsonDict:
        with self._lock:
            candidate = self.get_candidate(candidate_id)
            if candidate["status"] == "rolled_back":
                return {
                    "status": "already_rolled_back",
                    "candidate": candidate,
                    "affected_memories": [],
                }
            if candidate["status"] != "committed" or not candidate.get("committed_memory_id"):
                raise ValueError(f"candidate is not committed: {candidate_id}")

            memory = self.get_memory(candidate["committed_memory_id"])
            operation = str(candidate.get("operation") or "ADD").upper()
            affected: list[JsonDict] = []

            if operation == "DELETE":
                affected.append(self._restore_memory_for_rollback(memory["id"], reason=reason, actor=actor))
            elif operation == "MERGE":
                affected.append(
                    self._remove_candidate_evidence_for_rollback(
                        memory,
                        candidate,
                        reason=reason,
                        actor=actor,
                    )
                )
            elif operation == "UPDATE" or memory.get("supersedes_id"):
                before_new = self.get_memory(memory["id"])
                self._conn.execute(
                    "UPDATE memory_record SET status = 'disabled', updated_at = ? WHERE id = ?",
                    (_utc_now(), memory["id"]),
                )
                self._remove_from_fts(memory["id"])
                after_new = self.get_memory(memory["id"])
                self._append_revision(
                    memory["id"],
                    action="rollback_candidate",
                    before=before_new,
                    after=after_new,
                    reason=f"{reason}:{candidate_id}",
                    actor=actor,
                )
                self._publish_memory_change(
                    after_new,
                    action="rollback",
                    reason=f"{reason}:{candidate_id}",
                    source_ref=candidate_id,
                )
                affected.append(after_new)
                superseded_id = memory.get("supersedes_id") or candidate.get("supersedes_id")
                if superseded_id:
                    affected.append(
                        self._restore_memory_for_rollback(
                            superseded_id,
                            reason=reason,
                            actor=actor,
                        )
                    )
            elif self._memory_was_created_by_candidate(memory, candidate):
                before = self.get_memory(memory["id"])
                self._conn.execute(
                    "UPDATE memory_record SET status = 'disabled', updated_at = ? WHERE id = ?",
                    (_utc_now(), memory["id"]),
                )
                self._remove_from_fts(memory["id"])
                after = self.get_memory(memory["id"])
                self._append_revision(
                    memory["id"],
                    action="rollback_candidate",
                    before=before,
                    after=after,
                    reason=f"{reason}:{candidate_id}",
                    actor=actor,
                )
                self._publish_memory_change(
                    after,
                    action="rollback",
                    reason=f"{reason}:{candidate_id}",
                    source_ref=candidate_id,
                )
                affected.append(after)
            else:
                affected.append(
                    self._remove_candidate_evidence_for_rollback(
                        memory,
                        candidate,
                        reason=reason,
                        actor=actor,
                    )
                )

            self._conn.execute(
                """
                UPDATE memory_candidate
                SET status = 'rolled_back', resolved_at = ?, reason = ?
                WHERE id = ?
                """,
                (_utc_now(), f"{candidate['reason']};rollback:{reason}", candidate_id),
            )
            self._conn.commit()
        return {
            "status": "rolled_back",
            "candidate": self.get_candidate(candidate_id),
            "affected_memories": affected,
        }

    def revise(self, memory_id: str, new_summary: str, *, reason: str = "manual_revision") -> JsonDict:
        previous = self.get_memory(memory_id)
        sensitivity = _detect_sensitivity(new_summary)
        if sensitivity == "forbidden":
            raise ValueError("forbidden secrets cannot be persisted")
        evidence_id = self.add_evidence(
            source_type="manual_revision",
            content_summary=new_summary,
            sensitivity=sensitivity,
            retention_policy=self.gate.retention_policy(sensitivity),
            metadata={"supersedes_id": memory_id},
        )
        candidate = self.propose(
            summary=new_summary,
            memory_type=previous["type"],
            evidence_ids=[evidence_id],
            tags=previous["tags"],
            confidence=1.0,
            sensitivity=sensitivity,
            reason=reason,
            explicit_user_request=True,
            supersedes_id=memory_id,
            operation="UPDATE",
            target_memory_id=memory_id,
            allow_auto_commit=False,
        )
        return self.commit([candidate["candidate_id"]], actor="manual_revision")["committed"][0]

    def add_manual(
        self,
        summary: str,
        *,
        memory_type: str = "preference",
        tags: list[str] | None = None,
    ) -> JsonDict:
        sensitivity = _detect_sensitivity(summary)
        if sensitivity == "forbidden":
            raise ValueError("forbidden secrets cannot be persisted")
        evidence_id = self.add_evidence(
            source_type="manual_repl",
            content_summary=summary,
            sensitivity=sensitivity,
            retention_policy=self.gate.retention_policy(sensitivity),
        )
        candidate = self.propose(
            summary=summary,
            memory_type=memory_type,
            evidence_ids=[evidence_id],
            tags=tags or [],
            confidence=1.0,
            sensitivity=sensitivity,
            reason="manual_repl",
            explicit_user_request=True,
            allow_auto_commit=False,
        )
        return self.commit([candidate["candidate_id"]], actor="manual_repl")["committed"][0]

    def _create_memory(
        self,
        *,
        memory_id: str,
        memory_type: str,
        summary: str,
        tags: list[str],
        evidence_ids: list[str],
        confidence: float,
        importance: float,
        sensitivity: str,
        reason: str,
        actor: str,
        supersedes_id: str | None = None,
    ) -> JsonDict:
        now = _utc_now()
        retention = self.gate.retention_policy(sensitivity)
        disclosure = self.gate.disclosure_permission(sensitivity)
        self._conn.execute(
            """
            INSERT INTO memory_record (
                id, owner_user_id, agent_id, type, summary, tags_json, status,
                confidence, importance, sensitivity, supersedes_id, valid_from,
                retention_policy, retrieval_permission, use_permission,
                disclosure_permission, schema_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, 'allow',
                      'allow_implicit', ?, 1, ?, ?)
            """,
            (
                memory_id,
                self.owner_user_id,
                self.agent_id,
                memory_type,
                summary,
                _json(tags),
                confidence,
                importance,
                sensitivity,
                supersedes_id,
                now,
                retention,
                disclosure,
                now,
                now,
            ),
        )
        for evidence_id in evidence_ids:
            self._conn.execute(
                "INSERT OR IGNORE INTO memory_evidence (memory_id, evidence_id) VALUES (?, ?)",
                (memory_id, evidence_id),
            )
        memory = self.get_memory(memory_id)
        self._append_revision(
            memory_id,
            action="created",
            before=None,
            after=memory,
            reason=reason,
            actor=actor,
        )
        self._publish_memory_change(
            memory,
            action="upsert",
            reason=reason,
            source_ref=memory_id,
        )
        self._conn.commit()
        return self.get_memory(memory_id)

    def _append_revision(
        self,
        memory_id: str,
        *,
        action: str,
        before: JsonDict | None,
        after: JsonDict | None,
        reason: str,
        actor: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO memory_revision (
                id, memory_id, action, before_json, after_json, reason, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _new_id("rev"),
                memory_id,
                action,
                _json(before) if before is not None else None,
                _json(after) if after is not None else None,
                reason,
                actor,
                _utc_now(),
            ),
        )

    def _sync_fts(self, memory_id: str, summary: str, memory_type: str, tags: list[str]) -> None:
        self._conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
        self._conn.execute(
            "INSERT INTO memory_fts (memory_id, search_text) VALUES (?, ?)",
            (memory_id, _index_text(summary, memory_type, tags)),
        )

    def _remove_from_fts(self, memory_id: str) -> None:
        self._conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))

    def _publish_memory_change(
        self,
        memory: JsonDict,
        *,
        action: str,
        reason: str,
        source_ref: str | None = None,
        drain: bool = True,
    ) -> None:
        self.index_outbox.enqueue_memory(
            memory,
            action="upsert" if action == "upsert" else "tombstone",
            reason=reason,
            source_ref=source_ref,
        )
        self.projector_bus.enqueue_memory_change(
            memory,
            action=action,
            reason=reason,
            source_ref=source_ref,
        )
        if action != "upsert":
            for target_type in (
                "memory_fts",
                "memory_vector_index",
                "memory_graph_index",
                "relationship_projector",
                "worldops_projector",
                "persona_projector",
            ):
                self.ingestion.record_delete_propagation(
                    source_ref=source_ref or memory["id"],
                    source_type="memory_record",
                    target_type=target_type,
                    target_id=memory["id"],
                    action="tombstone",
                    reason=reason,
                    metadata={"memory_status": memory.get("status")},
                )
        if drain:
            self.index_outbox.process_pending(commit=False)
            self.projector_bus.process_pending(commit=False)
        for listener_name, listener in list(self._memory_change_listeners.items()):
            try:
                listener(
                    memory,
                    action=action,
                    reason=reason,
                    source_ref=source_ref,
                )
            except Exception as exc:
                self.ingestion.record_delete_propagation(
                    source_ref=source_ref or memory["id"],
                    source_type="memory_record",
                    target_type=f"{listener_name}_change_listener",
                    target_id=memory["id"],
                    action="listener_failed",
                    reason=reason,
                    metadata={"error": f"{type(exc).__name__}: {exc}", "memory_action": action},
                )

    def register_memory_change_listener(
        self,
        name: str,
        listener: Callable[..., Any],
    ) -> None:
        if not name.strip():
            raise ValueError("memory change listener name is required")
        self._memory_change_listeners[name] = listener

    def unregister_memory_change_listener(self, name: str) -> None:
        self._memory_change_listeners.pop(name, None)

    def _restore_memory_for_rollback(self, memory_id: str, *, reason: str, actor: str) -> JsonDict:
        before = self.get_memory(memory_id)
        self._conn.execute(
            "UPDATE memory_record SET status = 'active', updated_at = ? WHERE id = ?",
            (_utc_now(), memory_id),
        )
        after = self.get_memory(memory_id)
        self._append_revision(
            memory_id,
            action="rollback_restore",
            before=before,
            after=after,
            reason=reason,
            actor=actor,
        )
        self._publish_memory_change(after, action="upsert", reason=reason, source_ref=memory_id)
        return after

    def _remove_candidate_evidence_for_rollback(
        self,
        memory: JsonDict,
        candidate: JsonDict,
        *,
        reason: str,
        actor: str,
    ) -> JsonDict:
        before = self.get_memory(memory["id"])
        for evidence_id in candidate.get("evidence_ids", []):
            if self._evidence_used_by_other_committed_candidate(
                memory["id"],
                str(evidence_id),
                candidate["candidate_id"],
            ):
                continue
            self._conn.execute(
                "DELETE FROM memory_evidence WHERE memory_id = ? AND evidence_id = ?",
                (memory["id"], evidence_id),
            )
        after = self.get_memory(memory["id"])
        self._append_revision(
            memory["id"],
            action="rollback_evidence_removed",
            before=before,
            after=after,
            reason=f"{reason}:{candidate['candidate_id']}",
            actor=actor,
        )
        self._publish_memory_change(
            after,
            action="upsert",
            reason=f"{reason}:{candidate['candidate_id']}",
            source_ref=candidate["candidate_id"],
        )
        return after

    def _evidence_used_by_other_committed_candidate(
        self,
        memory_id: str,
        evidence_id: str,
        candidate_id: str,
    ) -> bool:
        rows = self._conn.execute(
            """
            SELECT id, evidence_ids_json
            FROM memory_candidate
            WHERE status = 'committed'
              AND committed_memory_id = ?
              AND id != ?
            """,
            (memory_id, candidate_id),
        ).fetchall()
        return any(evidence_id in _loads(row["evidence_ids_json"], []) for row in rows)

    def _memory_was_created_by_candidate(self, memory: JsonDict, candidate: JsonDict) -> bool:
        memory_evidence = set(memory.get("evidence_ids", []))
        candidate_evidence = set(candidate.get("evidence_ids", []))
        if memory_evidence and not memory_evidence.issubset(candidate_evidence):
            return False
        row = self._conn.execute(
            """
            SELECT 1
            FROM memory_revision
            WHERE memory_id = ?
              AND action = 'created'
              AND reason = ?
            LIMIT 1
            """,
            (memory["id"], candidate["reason"]),
        ).fetchone()
        return row is not None

    def _find_exact_active(self, memory_type: str, summary: str) -> JsonDict | None:
        row = self._conn.execute(
            """
            SELECT * FROM memory_record
            WHERE owner_user_id = ? AND agent_id = ? AND type = ? AND summary = ?
              AND status = 'active'
            LIMIT 1
            """,
            (self.owner_user_id, self.agent_id, memory_type, summary),
        ).fetchone()
        return self._memory_from_row(row) if row else None

    def _set_status(self, memory_id: str, status: str, *, reason: str, actor: str) -> JsonDict:
        with self._lock:
            before = self.get_memory(memory_id)
            now = _utc_now()
            self._conn.execute(
                "UPDATE memory_record SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, memory_id),
            )
            if status != "active":
                self._remove_from_fts(memory_id)
            after = self.get_memory(memory_id)
            self._append_revision(
                memory_id,
                action=status,
                before=before,
                after=after,
                reason=reason,
                actor=actor,
            )
            self._publish_memory_change(
                after,
                action="upsert" if status == "active" else "tombstone",
                reason=reason,
                source_ref=memory_id,
            )
            self._conn.commit()
        return after

    def disable(self, memory_id: str) -> JsonDict:
        return self._set_status(memory_id, "disabled", reason="manual_disable", actor="manual_repl")

    def delete(self, memory_id: str) -> JsonDict:
        return self._set_status(memory_id, "deleted", reason="manual_delete", actor="manual_repl")

    def restore(self, memory_id: str) -> JsonDict:
        before = self.get_memory(memory_id)
        with self._lock:
            self._conn.execute(
                "UPDATE memory_record SET status = 'active', updated_at = ? WHERE id = ?",
                (_utc_now(), memory_id),
            )
            after = self.get_memory(memory_id)
            self._append_revision(
                memory_id,
                action="restored",
                before=before,
                after=after,
                reason="manual_restore",
                actor="manual_repl",
            )
            self._publish_memory_change(
                after,
                action="upsert",
                reason="manual_restore",
                source_ref=memory_id,
            )
            self._conn.commit()
        return after

    def recall(
        self,
        query_text: str,
        *,
        turn_frame: JsonDict | None = None,
        context_use_policy: JsonDict | None = None,
        context_features: JsonDict | None = None,
        top_k: int = 3,
        token_budget: int | None = None,
    ) -> JsonDict:
        return self.retrieval.recall(
            query_text,
            turn_frame=turn_frame,
            context_use_policy=context_use_policy,
            context_features=context_features,
            top_k=top_k,
            token_budget=token_budget,
        )

    def plan_recall(
        self,
        query_text: str,
        *,
        turn_frame: JsonDict | None = None,
        context_use_policy: JsonDict | None = None,
        top_k: int = 5,
        planner_context: JsonDict | None = None,
    ) -> JsonDict:
        plan = self.recall_planner.plan(
            query_text,
            turn_frame=turn_frame,
            context_use_policy=context_use_policy,
            top_k=top_k,
            planner_context=planner_context,
        )
        return plan_to_json(plan)

    def prefetch_recall(
        self,
        query_text: str,
        *,
        turn_frame: JsonDict | None = None,
        context_use_policy: JsonDict | None = None,
        top_k: int = 5,
        ttl_seconds: int = 600,
    ) -> JsonDict:
        plan = self.recall_planner.plan(
            query_text,
            turn_frame=turn_frame,
            context_use_policy=context_use_policy,
            top_k=top_k,
            planner_context={"mode": "prefetch", "top_k": top_k},
        )
        plan_json = plan_to_json(plan)
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=max(ttl_seconds, 1))).isoformat()
        cache_key = _content_hash(
            query_text,
            plan.scene,
            json_dumps(turn_frame or {}),
            json_dumps(context_use_policy or {}),
            plan.planner_version,
        )
        with self._lock:
            existing = self._conn.execute(
                """
                SELECT * FROM recall_plan_cache
                WHERE cache_key = ?
                  AND status = ?
                  AND expires_at > ?
                """,
                (cache_key, "cached", now.isoformat()),
            ).fetchone()
            if existing is not None:
                return {
                    "status": "cache_hit",
                    "cache_key": existing["cache_key"],
                    "expires_at": existing["expires_at"],
                    "plan": json_loads(existing["plan_json"], {}),
                    "cached": True,
                }
            self._conn.execute(
                """
                INSERT INTO recall_plan_cache (
                    cache_key, plan_id, status, query_text, scene, plan_json,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    plan_id = excluded.plan_id,
                    status = excluded.status,
                    query_text = excluded.query_text,
                    scene = excluded.scene,
                    plan_json = excluded.plan_json,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (
                    cache_key,
                    plan.plan_id,
                    "cached" if plan.decision == "prefetch_only" else "skipped",
                    query_text,
                    plan.scene,
                    json_dumps(plan_json),
                    now.isoformat(),
                    expires_at,
                ),
            )
            self._conn.commit()
        return {
            "status": "cached" if plan.decision == "prefetch_only" else "skipped",
            "cache_key": cache_key,
            "expires_at": expires_at,
            "plan": plan_json,
        }

    def list_recall_plan_cache(self, *, limit: int = 20) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM recall_plan_cache
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "cache_key": row["cache_key"],
                "plan_id": row["plan_id"],
                "status": row["status"],
                "query_text": row["query_text"],
                "scene": row["scene"],
                "plan": json_loads(row["plan_json"], {}),
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
            }
            for row in rows
        ]

    def search(self, query_text: str, *, top_k: int = 10) -> list[JsonDict]:
        frame = self.recall(query_text, top_k=top_k)
        return [self.get_memory(memory_id) for memory_id in frame["retrieved_ids"]]

    def get_memory(self, memory_id: str | None) -> JsonDict:
        if not memory_id:
            raise ValueError("memory id is required")
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_record WHERE id = ?",
                (memory_id,),
            ).fetchone()
        if not row:
            raise ValueError(f"memory not found: {memory_id}")
        return self._memory_from_row(row)

    def list_memories(self, status: str | None = None) -> list[JsonDict]:
        sql = "SELECT * FROM memory_record WHERE owner_user_id = ? AND agent_id = ?"
        params: list[Any] = [self.owner_user_id, self.agent_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._memory_from_row(row) for row in rows]

    def get_candidate(self, candidate_id: str) -> JsonDict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_candidate WHERE id = ?",
                (candidate_id,),
            ).fetchone()
        if not row:
            raise ValueError(f"candidate not found: {candidate_id}")
        return self._candidate_from_row(row)

    def list_candidates(self, status: str | None = None) -> list[JsonDict]:
        sql = "SELECT * FROM memory_candidate"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._candidate_from_row(row) for row in rows]

    def find_candidate_by_evidence(self, evidence_id: str) -> JsonDict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM memory_candidate
                WHERE EXISTS (
                    SELECT 1 FROM json_each(memory_candidate.evidence_ids_json)
                    WHERE json_each.value = ?
                )
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (evidence_id,),
            ).fetchone()
        return self._candidate_from_row(row) if row is not None else None

    def evidence_for_memory(self, memory_id: str) -> list[JsonDict]:
        return self.canonical_store.evidence_for_memory(memory_id)

    def get_evidence(self, evidence_id: str) -> JsonDict:
        return self.canonical_store.get_evidence(evidence_id)

    def list_evidence(
        self,
        *,
        status: str | None = "active",
        source_type: str | None = None,
        sensitivity: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[JsonDict]:
        return self.canonical_store.list_evidence(
            status=status,
            source_type=source_type,
            sensitivity=sensitivity,
            limit=limit,
            offset=offset,
        )

    def find_evidence_by_source(self, *, source_type: str, source_ref: str) -> JsonDict | None:
        return self.canonical_store.find_evidence_by_source(
            source_type=source_type,
            source_ref=source_ref,
        )

    def link_evidence(
        self,
        memory_id: str,
        evidence_id: str,
        *,
        reason: str = "manual_link_evidence",
        actor: str = "manual_repl",
    ) -> JsonDict:
        return self.canonical_store.link_evidence(
            memory_id,
            evidence_id,
            reason=reason,
            actor=actor,
        )

    def unlink_evidence(
        self,
        memory_id: str,
        evidence_id: str,
        *,
        reason: str = "manual_unlink_evidence",
        actor: str = "manual_repl",
        disable_if_empty: bool = True,
    ) -> JsonDict:
        return self.canonical_store.unlink_evidence(
            memory_id,
            evidence_id,
            reason=reason,
            actor=actor,
            disable_if_empty=disable_if_empty,
        )

    def delete_evidence(
        self,
        evidence_id: str,
        *,
        reason: str = "manual_evidence_delete",
        actor: str = "manual_repl",
    ) -> JsonDict:
        return self.canonical_store.soft_delete_evidence(
            evidence_id,
            reason=reason,
            actor=actor,
        )

    def store_audit(self) -> JsonDict:
        return self.canonical_store.store_audit()

    def repair_fts_index(self, *, actor: str = "store_audit") -> JsonDict:
        return self.canonical_store.repair_fts_index(actor=actor)

    def process_index_outbox(self, *, limit: int = 100) -> JsonDict:
        return self.index_outbox.process_pending(limit=limit)

    def list_index_outbox(self, *, status: str | None = None, limit: int = 50) -> list[JsonDict]:
        return self.index_outbox.list_items(status=status, limit=limit)

    def index_health(self) -> JsonDict:
        return self.index_outbox.health()

    def rebuild_memory_indexes(
        self,
        *,
        backends: tuple[str, ...] = ("fts", "vector", "graph"),
        index_generation: str = "active",
        batch_size: int = 100,
        process: bool = True,
    ) -> JsonDict:
        return self.index_outbox.rebuild_indexes(
            backends=backends,
            index_generation=index_generation,
            batch_size=batch_size,
            process=process,
        )

    def list_index_rebuilds(self, *, limit: int = 20) -> list[JsonDict]:
        return self.index_outbox.list_rebuild_runs(limit=limit)

    def requeue_index_dead_letters(
        self,
        *,
        limit: int = 50,
        reason: str = "manual_requeue",
    ) -> JsonDict:
        return self.index_outbox.requeue_dead_letters(limit=limit, reason=reason)

    def cleanup_recall_traces(
        self,
        *,
        older_than_days: int = 30,
        keep_pinned: bool = True,
    ) -> JsonDict:
        return self.retrieval.cleanup_traces(
            older_than_days=older_than_days,
            keep_pinned=keep_pinned,
        )

    def process_projector_outbox(self, *, limit: int = 100) -> JsonDict:
        return self.projector_bus.process_pending(limit=limit)

    def list_projector_outbox(self, *, status: str | None = None, limit: int = 50) -> list[JsonDict]:
        return self.projector_bus.list_items(status=status, limit=limit)

    def list_projector_checkpoints(self) -> list[JsonDict]:
        return self.projector_bus.list_checkpoints()

    def list_projection_snapshots(
        self,
        *,
        subsystem: str | None = None,
        status: str | None = "active",
        limit: int = 50,
    ) -> list[JsonDict]:
        return self.projector_bus.list_snapshots(
            subsystem=subsystem,
            status=status,
            limit=limit,
        )

    def get_projection_frame(self, subsystem: str) -> JsonDict:
        return self.projector_bus.latest_frame(subsystem)

    def rebuild_projections(
        self,
        *,
        subsystems: list[str] | None = None,
        limit: int = 500,
    ) -> JsonDict:
        return self.projector_bus.rebuild_from_memories(
            self.list_memories(status="active"),
            subsystems=subsystems,
            limit=limit,
        )

    def list_projection_rebuilds(self, *, limit: int = 20) -> list[JsonDict]:
        return self.projector_bus.list_rebuild_runs(limit=limit)

    def list_projection_audits(
        self,
        *,
        action: str | None = None,
        subsystem: str | None = None,
        limit: int = 50,
    ) -> list[JsonDict]:
        return self.projector_bus.list_audit(
            action=action,
            subsystem=subsystem,
            limit=limit,
        )

    def record_projection_proposal(
        self,
        *,
        subsystem: str,
        proposal: JsonDict,
        source_event_id: str | None = None,
        source_snapshot_ref: str | None = None,
    ) -> JsonDict:
        return self.projector_bus.record_proposal_request(
            subsystem=subsystem,
            proposal=proposal,
            source_event_id=source_event_id,
            source_snapshot_ref=source_snapshot_ref,
        )

    def list_projection_proposals(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[JsonDict]:
        return self.projector_bus.list_proposal_requests(status=status, limit=limit)

    def process_retention(self, *, now: str | None = None, limit: int = 100) -> JsonDict:
        return self.retention.process_expired(now=now, limit=limit)

    def run_lifecycle(
        self,
        *,
        mode: str = "dry_run",
        now: str | None = None,
        limit: int = 200,
        actor: str = "lifecycle_worker",
        create_consolidation_candidates: bool | None = None,
    ) -> JsonDict:
        return self.lifecycle.run(
            mode=mode,
            now=now,
            limit=limit,
            actor=actor,
            create_consolidation_candidates=create_consolidation_candidates,
        )

    def score_memory_heat(self, memory_id: str, *, now: str | None = None) -> JsonDict:
        return self.lifecycle.score_memory_public(memory_id, now=now)

    def list_lifecycle_states(
        self,
        *,
        tier: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        return self.lifecycle.list_lifecycle_states(tier=tier, limit=limit)

    def get_lifecycle_state(self, memory_id: str) -> JsonDict:
        return self.lifecycle.get_lifecycle_state(memory_id)

    def list_heat_runs(self, *, limit: int = 20) -> list[JsonDict]:
        return self.lifecycle.list_heat_runs(limit=limit)

    def list_heat_snapshots(
        self,
        *,
        run_id: str | None = None,
        memory_id: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        return self.lifecycle.list_heat_snapshots(run_id=run_id, memory_id=memory_id, limit=limit)

    def plan_consolidation(
        self,
        *,
        run_id: str | None = None,
        now: str | None = None,
        limit: int = 200,
        create_candidates: bool = False,
        actor: str = "lifecycle_worker",
    ) -> JsonDict:
        return self.lifecycle.plan_consolidation(
            run_id=run_id,
            now=now,
            limit=limit,
            create_candidates=create_candidates,
            actor=actor,
        )

    def list_consolidation_plans(
        self,
        *,
        run_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        return self.lifecycle.list_consolidation_plans(run_id=run_id, status=status, limit=limit)

    def list_tombstones(self, *, limit: int = 100) -> list[JsonDict]:
        return self.lifecycle.list_tombstones(limit=limit)

    def process_tombstones(self, *, now: str | None = None) -> JsonDict:
        return self.lifecycle.process_tombstones(now=now)

    def lifecycle_health(self) -> JsonDict:
        return self.lifecycle.lifecycle_health()

    def forget_memory(
        self,
        memory_id: str,
        *,
        mode: str = "delete",
        reason: str = "user_forget_request",
        actor: str = "user",
    ) -> JsonDict:
        return self.retention.request_forget(
            target_type="memory",
            target_id=memory_id,
            mode=mode,
            reason=reason,
            actor=actor,
        )

    def forget_evidence(
        self,
        evidence_id: str,
        *,
        mode: str = "delete",
        reason: str = "user_forget_request",
        actor: str = "user",
    ) -> JsonDict:
        return self.retention.request_forget(
            target_type="evidence",
            target_id=evidence_id,
            mode=mode,
            reason=reason,
            actor=actor,
        )

    def list_forget_requests(self, *, limit: int = 50) -> list[JsonDict]:
        return self.retention.list_forget_requests(limit=limit)

    def set_surface_override(
        self,
        memory_id: str,
        *,
        surface_permission: str,
        override_type: str = "user_control",
        reason: str = "user_surface_override",
        actor: str = "user",
        expires_at: str | None = None,
        cooldown_until: str | None = None,
        metadata: JsonDict | None = None,
    ) -> JsonDict:
        allowed = {"auto", "allow", "silent_only", "ask", "always_ask", "none", "hidden", "deny"}
        if surface_permission not in allowed:
            raise ValueError(f"invalid surface_permission: {surface_permission}")
        before = self.get_memory(memory_id)
        override_id = _new_id("surface_override")
        now = _utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_surface_override (
                    id, memory_id, override_type, surface_permission, reason,
                    actor, status, expires_at, metadata_json, created_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL)
                """,
                (
                    override_id,
                    memory_id,
                    override_type,
                    surface_permission,
                    reason,
                    actor,
                    expires_at,
                    _json(metadata or {}),
                    now,
                ),
            )
            if cooldown_until is None:
                self._conn.execute(
                    """
                    UPDATE memory_record
                    SET surface_permission = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (surface_permission, now, memory_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE memory_record
                    SET surface_permission = ?, surface_cooldown_until = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (surface_permission, cooldown_until, now, memory_id),
                )
            self._conn.commit()
        after = self.get_memory(memory_id)
        self._append_revision(
            memory_id,
            action="surface_override",
            before=before,
            after=after,
            reason=reason,
            actor=actor,
        )
        return {
            "id": override_id,
            "memory_id": memory_id,
            "surface_permission": surface_permission,
            "override_type": override_type,
            "status": "active",
            "expires_at": expires_at,
            "memory": after,
        }

    def hide_memory(
        self,
        memory_id: str,
        *,
        reason: str = "user_hide_memory",
        actor: str = "user",
    ) -> JsonDict:
        return self.set_surface_override(
            memory_id,
            surface_permission="none",
            override_type="hide",
            reason=reason,
            actor=actor,
        )

    def clear_surface_override(
        self,
        memory_id: str,
        *,
        reason: str = "user_clear_surface_override",
        actor: str = "user",
    ) -> JsonDict:
        before = self.get_memory(memory_id)
        now = _utc_now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE memory_surface_override
                SET status = 'revoked', revoked_at = ?
                WHERE memory_id = ? AND status = 'active'
                """,
                (now, memory_id),
            )
            self._conn.execute(
                """
                UPDATE memory_record
                SET surface_permission = 'auto',
                    surface_cooldown_until = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, memory_id),
            )
            self._conn.commit()
        after = self.get_memory(memory_id)
        self._append_revision(
            memory_id,
            action="surface_override_cleared",
            before=before,
            after=after,
            reason=reason,
            actor=actor,
        )
        return {"memory_id": memory_id, "status": "cleared", "memory": after}

    def record_surface_feedback(
        self,
        memory_id: str,
        *,
        feedback_type: str,
        value: float | None = None,
        recall_trace_id: str | None = None,
        actor: str = "user",
        metadata: JsonDict | None = None,
    ) -> JsonDict:
        self.get_memory(memory_id)
        feedback_id = _new_id("surface_feedback")
        now = _utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_surface_feedback (
                    id, memory_id, recall_trace_id, feedback_type, value,
                    actor, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback_id,
                    memory_id,
                    recall_trace_id,
                    feedback_type,
                    value,
                    actor,
                    _json(metadata or {}),
                    now,
                ),
            )
            self._conn.commit()
        return {
            "id": feedback_id,
            "memory_id": memory_id,
            "recall_trace_id": recall_trace_id,
            "feedback_type": feedback_type,
            "value": value,
            "actor": actor,
            "created_at": now,
        }

    def record_surface_consent(
        self,
        memory_id: str,
        *,
        status: str,
        request_type: str = "memory_disclosure",
        prompt: str = "",
        recall_trace_id: str | None = None,
        actor: str = "user",
        expires_at: str | None = None,
        metadata: JsonDict | None = None,
    ) -> JsonDict:
        allowed = {"requested", "granted", "denied", "expired", "revoked"}
        if status not in allowed:
            raise ValueError(f"invalid consent status: {status}")
        self.get_memory(memory_id)
        consent_id = _new_id("surface_consent")
        now = _utc_now()
        granted_at = now if status == "granted" else None
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_surface_consent (
                    id, memory_id, recall_trace_id, request_type, status,
                    prompt, actor, granted_at, expires_at, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    consent_id,
                    memory_id,
                    recall_trace_id,
                    request_type,
                    status,
                    prompt,
                    actor,
                    granted_at,
                    expires_at,
                    _json(metadata or {}),
                    now,
                ),
            )
            self._conn.commit()
        return {
            "id": consent_id,
            "memory_id": memory_id,
            "recall_trace_id": recall_trace_id,
            "request_type": request_type,
            "status": status,
            "granted_at": granted_at,
            "expires_at": expires_at,
            "created_at": now,
        }

    def list_surface_overrides(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[JsonDict]:
        sql = "SELECT * FROM memory_surface_override"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "id": row["id"],
                "memory_id": row["memory_id"],
                "override_type": row["override_type"],
                "surface_permission": row["surface_permission"],
                "reason": row["reason"],
                "actor": row["actor"],
                "status": row["status"],
                "expires_at": row["expires_at"],
                "metadata": _loads(row_value(row, "metadata_json", "{}"), {}),
                "created_at": row["created_at"],
                "revoked_at": row["revoked_at"],
            }
            for row in rows
        ]

    def list_surface_feedback(self, *, limit: int = 50) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memory_surface_feedback ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "memory_id": row["memory_id"],
                "recall_trace_id": row["recall_trace_id"],
                "feedback_type": row["feedback_type"],
                "value": row["value"],
                "actor": row["actor"],
                "metadata": _loads(row["metadata_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_surface_consents(self, *, limit: int = 50) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memory_surface_consent ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "memory_id": row["memory_id"],
                "recall_trace_id": row["recall_trace_id"],
                "request_type": row["request_type"],
                "status": row["status"],
                "prompt": row["prompt"],
                "actor": row["actor"],
                "granted_at": row["granted_at"],
                "expires_at": row["expires_at"],
                "metadata": _loads(row["metadata_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def store_health(self) -> JsonDict:
        return self.observability.store_health()

    def run_canonical_eval(self) -> JsonDict:
        return self.canonical_eval.run()

    def list_canonical_eval_runs(self, *, limit: int = 20) -> list[JsonDict]:
        return self.canonical_eval.list_runs(limit=limit)

    def run_recall_eval(self, cases: list[JsonDict] | None = None) -> JsonDict:
        return self.recall_eval.run(cases=cases)

    def list_recall_eval_runs(self, *, limit: int = 20) -> list[JsonDict]:
        return self.recall_eval.list_runs(limit=limit)

    def run_planner_eval(self, cases: list[JsonDict] | None = None) -> JsonDict:
        return self.planner_eval.run(cases=cases)

    def list_planner_eval_runs(self, *, limit: int = 20) -> list[JsonDict]:
        return self.planner_eval.list_runs(limit=limit)

    def run_surface_eval(self, cases: list[JsonDict] | None = None) -> JsonDict:
        return self.surface_eval.run(cases=cases)

    def list_surface_eval_runs(self, *, limit: int = 20) -> list[JsonDict]:
        return self.surface_eval.list_runs(limit=limit)

    def run_lifecycle_eval(self) -> JsonDict:
        return self.lifecycle_eval.run()

    def list_lifecycle_eval_runs(self, *, limit: int = 20) -> list[JsonDict]:
        return self.lifecycle_eval.list_runs(limit=limit)

    def list_quality_eval_suites(self) -> list[JsonDict]:
        return self.quality_ops.list_eval_suites()

    def register_quality_eval_suite(
        self,
        name: str,
        runner: Callable[[], JsonDict],
        *,
        source_module: str,
        default_severity: str = "error",
    ) -> None:
        self.quality_ops.register_eval_suite(
            name,
            runner,
            source_module=source_module,
            default_severity=default_severity,
        )

    def register_quality_export_section(
        self,
        name: str,
        provider: Callable[[], JsonDict],
    ) -> None:
        self.quality_ops.register_export_section(name, provider)

    def run_quality_eval(
        self,
        *,
        suites: list[str] | None = None,
        run_type: str = "module_eval",
        trigger: str = "manual",
        policy_profile: str = "core",
        dataset_version: str = "default",
        baseline_id: str | None = None,
        scope: JsonDict | None = None,
    ) -> JsonDict:
        return self.quality_ops.run_quality_eval(
            suites=suites,
            run_type=run_type,
            trigger=trigger,
            policy_profile=policy_profile,
            dataset_version=dataset_version,
            baseline_id=baseline_id,
            scope=scope,
        )

    def run_scenario_replay(
        self,
        *,
        package_name: str = "default_memory_scenario_replay",
        suites: list[str] | None = None,
        policy_profile: str = "core",
        dataset_version: str = "default",
    ) -> JsonDict:
        return self.quality_ops.run_scenario_replay(
            package_name=package_name,
            suites=suites,
            policy_profile=policy_profile,
            dataset_version=dataset_version,
        )

    def list_quality_runs(
        self,
        *,
        limit: int = 20,
        run_type: str | None = None,
    ) -> list[JsonDict]:
        return self.quality_ops.list_quality_runs(limit=limit, run_type=run_type)

    def get_quality_run(self, run_id: str) -> JsonDict:
        return self.quality_ops.get_quality_run(run_id)

    def sync_quality_trace_index(self, *, limit: int = 500, replay: bool = False) -> JsonDict:
        return self.quality_ops.sync_trace_index(limit=limit, replay=replay)

    def quality_trace_index_coverage(self) -> JsonDict:
        return self.quality_ops.trace_index_coverage()

    def collect_quality_metrics(self, *, source_ref: str = "quality_ops") -> JsonDict:
        return self.quality_ops.collect_metrics(source_ref=source_ref)

    def quality_dashboard(self) -> JsonDict:
        return self.quality_ops.dashboard()

    def pin_quality_baseline(
        self,
        run_id: str,
        *,
        name: str,
        policy_profile: str = "core",
        dataset_version: str = "default",
        status: str = "candidate",
        accepted_failures: list[JsonDict] | None = None,
        promote: bool = False,
    ) -> JsonDict:
        return self.quality_ops.pin_baseline(
            run_id,
            name=name,
            policy_profile=policy_profile,
            dataset_version=dataset_version,
            status=status,
            accepted_failures=accepted_failures,
            promote=promote,
        )

    def list_quality_baselines(
        self,
        *,
        limit: int = 20,
        status: str | None = None,
        policy_profile: str | None = None,
    ) -> list[JsonDict]:
        return self.quality_ops.list_baselines(
            limit=limit,
            status=status,
            policy_profile=policy_profile,
        )

    def compare_quality_baseline(
        self,
        run_id: str,
        *,
        baseline_id: str | None = None,
    ) -> JsonDict:
        return self.quality_ops.compare_baseline(run_id, baseline_id=baseline_id)

    def run_release_gate(
        self,
        *,
        change_scope: list[str] | None = None,
        baseline_id: str | None = None,
        policy_profile: str = "core",
        dataset_version: str = "default",
    ) -> JsonDict:
        return self.quality_ops.run_release_gate(
            change_scope=change_scope,
            baseline_id=baseline_id,
            policy_profile=policy_profile,
            dataset_version=dataset_version,
        )

    def list_admin_action_specs(self) -> list[JsonDict]:
        return self.quality_ops.list_admin_action_specs()

    def preview_admin_action(
        self,
        action_name: str,
        params: JsonDict | None = None,
        *,
        actor: str = "local_user",
        role: str = "admin",
        reason: str = "manual_admin_action",
    ) -> JsonDict:
        return self.quality_ops.preview_admin_action(
            action_name,
            params or {},
            actor=actor,
            role=role,
            reason=reason,
        )

    def execute_admin_action(
        self,
        action_id: str,
        *,
        confirmation_token: str | None = None,
        actor: str = "local_user",
    ) -> JsonDict:
        return self.quality_ops.execute_admin_action(
            action_id,
            confirmation_token=confirmation_token,
            actor=actor,
        )

    def list_admin_actions(
        self,
        *,
        limit: int = 20,
        execution_status: str | None = None,
    ) -> list[JsonDict]:
        return self.quality_ops.list_admin_actions(
            limit=limit,
            execution_status=execution_status,
        )

    def set_quality_global_dry_run(
        self,
        enabled: bool,
        *,
        ttl_seconds: int | None = None,
    ) -> JsonDict:
        return self.quality_ops.set_global_dry_run(enabled, ttl_seconds=ttl_seconds)

    def cleanup_quality_metrics(self, *, older_than_days: int = 30) -> JsonDict:
        return self.quality_ops.cleanup_metrics(older_than_days=older_than_days)

    def export_quality_report(
        self,
        *,
        run_id: str | None = None,
        output_path: str | Path | None = None,
    ) -> JsonDict:
        return self.quality_ops.export_quality_report(run_id=run_id, output_path=output_path)

    def list_quality_events(
        self,
        *,
        status: str | None = None,
        event_type: str | None = None,
        limit: int = 20,
    ) -> list[JsonDict]:
        return self.quality_ops.list_quality_events(
            status=status,
            event_type=event_type,
            limit=limit,
        )

    def revisions_for_memory(self, memory_id: str) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memory_revision WHERE memory_id = ? ORDER BY created_at DESC",
                (memory_id,),
            ).fetchall()
        return [revision_from_row(row) for row in rows]

    def list_recall_traces(self, limit: int = 20) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM recall_trace ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "query_text": row["query_text"],
                "backend": row["backend"],
                "surface_action": row["surface_action"],
                "decision_reason": row["decision_reason"],
                "degraded": bool(row["degraded"]),
                "latency_ms": row["latency_ms"],
                "plan": json_loads(row_value(row, "plan_json", "{}"), {}),
                "token_budget": row_value(row, "token_budget", 0),
                "token_used": row_value(row, "token_used", 0),
                "final_count": row_value(row, "final_count", 0),
                "item_count": row_value(row, "item_count", 0),
                "cost": json_loads(row_value(row, "cost_json", "{}"), {}),
                "pinned": bool(row_value(row, "pinned", 0)),
                "expires_at": row_value(row, "expires_at"),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_surface_policy_traces(self, limit: int = 20) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM memory_surface_policy_trace
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            trace_ids = [row["id"] for row in rows]
            item_rows: list[sqlite3.Row] = []
            if trace_ids:
                placeholders = ",".join("?" for _ in trace_ids)
                item_rows = self._conn.execute(
                    f"""
                    SELECT * FROM memory_surface_policy_item
                    WHERE trace_id IN ({placeholders})
                    ORDER BY trace_id, rank
                    """,
                    trace_ids,
                ).fetchall()
        grouped: dict[str, list[JsonDict]] = {}
        for item in item_rows:
            grouped.setdefault(item["trace_id"], []).append(
                {
                    "memory_id": item["memory_id"],
                    "rank": item["rank"],
                    "score": item["score"],
                    "priority": item["priority"],
                    "action": item["action"],
                    "legacy_action": item["legacy_action"],
                    "selected": bool(item["selected"]),
                    "reason": item["reason"],
                    "suppression_reason": item["suppression_reason"],
                    "policy": _loads(item["policy_json"], {}),
                }
            )
        return [
            {
                "id": row["id"],
                "recall_trace_id": row["recall_trace_id"],
                "turn_id": row["turn_id"],
                "session_id": row["session_id"],
                "policy_version": row["policy_version"],
                "policy_profile": row["policy_profile"],
                "provider": row["provider"],
                "surface_action": row["surface_action"],
                "legacy_surface_action": row["legacy_surface_action"],
                "decision_reason": row["decision_reason"],
                "forbidden_disclosures": _loads(row["forbidden_disclosures_json"], []),
                "consent_request": _loads(row["consent_request_json"], {}),
                "provider_usage": _loads(row_value(row, "provider_usage_json", "[]"), []),
                "token_budget": row_value(row, "token_budget", 0),
                "token_used": row_value(row, "token_used", 0),
                "items": grouped.get(row["id"], []),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_recall_plan_traces(self, limit: int = 20) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM recall_plan_trace
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "plan_id": row["plan_id"],
                "recall_trace_id": row["recall_trace_id"],
                "turn_id": row["turn_id"],
                "session_id": row["session_id"],
                "planner_version": row["planner_version"],
                "planner_mode": row["planner_mode"],
                "decision": row["decision"],
                "query_intent": row["query_intent"],
                "scene": row["scene"],
                "explicit_recall": bool(row["explicit_recall"]),
                "skipped_reason": row["skipped_reason"],
                "subqueries": json_loads(row["subqueries_json"], []),
                "budget": json_loads(row["budget_json"], {}),
                "decision_reasons": json_loads(row["decision_reasons_json"], []),
                "llm_usage": json_loads(row["llm_usage_json"], {}),
                "planner_trace": json_loads(row["planner_trace_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def approve_review(self, review_id: str, *, actor: str = "manual_review") -> JsonDict:
        return self.gate_pipeline.approve_review(review_id, actor=actor)

    def reject_review(
        self,
        review_id: str,
        *,
        reason: str = "manual_review_reject",
        actor: str = "manual_review",
    ) -> JsonDict:
        return self.gate_pipeline.reject_review(review_id, reason=reason, actor=actor)

    def defer_review(
        self,
        review_id: str,
        *,
        days: int = 30,
        until: str | None = None,
        reason: str = "manual_review_defer",
        actor: str = "manual_review",
    ) -> JsonDict:
        return self.gate_pipeline.defer_review(
            review_id,
            days=days,
            until=until,
            reason=reason,
            actor=actor,
        )

    def expire_deferred_candidates(
        self,
        *,
        now: str | None = None,
        reason: str = "deferred_ttl_expired",
    ) -> JsonDict:
        return self.gate_pipeline.expire_deferred_candidates(now=now, reason=reason)

    def process_expired_deferrals(
        self,
        *,
        now: str | None = None,
        reason: str = "deferred_ttl_expired",
    ) -> JsonDict:
        return self.gate_pipeline.process_expired_deferrals(now=now, reason=reason)

    def get_gate_decision(self, decision_id: str) -> JsonDict:
        return self.gate_pipeline.get_gate_decision(decision_id)

    def list_gate_decisions(self, *, candidate_id: str | None = None, limit: int = 50) -> list[JsonDict]:
        return self.gate_pipeline.list_gate_decisions(candidate_id=candidate_id, limit=limit)

    def get_review_item(self, review_id: str) -> JsonDict:
        return self.gate_pipeline.get_review_item(review_id)

    def list_review_items(self, *, status: str | None = "open", limit: int = 50) -> list[JsonDict]:
        return self.gate_pipeline.list_review_items(status=status, limit=limit)

    def get_quarantine(self, quarantine_id: str) -> JsonDict:
        return self.gate_pipeline.get_quarantine(quarantine_id)

    def list_quarantines(self, *, status: str | None = "active", limit: int = 50) -> list[JsonDict]:
        return self.gate_pipeline.list_quarantines(status=status, limit=limit)

    def health(self) -> JsonDict:
        with self._lock:
            memory_count = self._conn.execute(
                "SELECT COUNT(*) FROM memory_record WHERE status = 'active'"
            ).fetchone()[0]
            candidate_status_rows = self._conn.execute(
                "SELECT status, COUNT(*) AS count FROM memory_candidate GROUP BY status"
            ).fetchall()
            open_review_count = self._conn.execute(
                "SELECT COUNT(*) FROM memory_review_item WHERE status = 'open'"
            ).fetchone()[0]
            active_quarantine_count = self._conn.execute(
                "SELECT COUNT(*) FROM memory_quarantine WHERE status = 'active'"
            ).fetchone()[0]
            gate_decision_count = self._conn.execute(
                "SELECT COUNT(*) FROM memory_gate_decision"
            ).fetchone()[0]
            episode_count = self._conn.execute(
                "SELECT COUNT(*) FROM memory_episode"
            ).fetchone()[0]
            pending_job_count = self._conn.execute(
                "SELECT COUNT(*) FROM extraction_job WHERE status = 'pending'"
            ).fetchone()[0]
        canonical_store = self.store_health()
        return {
            "status": "ok" if canonical_store["status"] in {"ok", "warn"} else "error",
            "mode": "in_process",
            "backend": "hybrid_rrf",
            "database": str(self.path),
            "active_memories": memory_count,
            "candidate_statuses": {
                row["status"]: row["count"] for row in candidate_status_rows
            },
            "pending_candidates": {
                row["status"]: row["count"] for row in candidate_status_rows
            }.get("pending", 0),
            "open_reviews": open_review_count,
            "active_quarantines": active_quarantine_count,
            "gate_decisions": gate_decision_count,
            "episodes": episode_count,
            "pending_extraction_jobs": pending_job_count,
            "canonical_store": canonical_store,
            "lifecycle": canonical_store.get("lifecycle", {}),
        }

    def sync_state_cache(self, state: JsonDict) -> None:
        state.setdefault("memory", {})["items"] = [
            {
                "id": item["id"],
                "type": item["type"],
                "content": item["summary"],
                "tags": item["tags"],
                "status": item["status"],
                "sensitivity": item["sensitivity"],
                "evidence_ids": item["evidence_ids"],
                "created_at": item["created_at"],
                "_projection": "memory_runtime",
            }
            for item in self.list_memories()
        ]
        state["memory"]["runtime"] = self.health()

    def _memory_from_row(self, row: sqlite3.Row) -> JsonDict:
        with self._lock:
            evidence_rows = self._conn.execute(
                """
                SELECT me.evidence_id, e.metadata_json
                FROM memory_evidence me
                JOIN evidence e ON e.id=me.evidence_id
                WHERE me.memory_id = ?
                ORDER BY me.evidence_id
                """,
                (row["id"],),
            ).fetchall()
            surface_feedback = self._surface_feedback_for_memory(row["id"])
            surface_consent = self._surface_consent_for_memory(row["id"])
            lifecycle = self.lifecycle.get_lifecycle_state(row["id"], missing_ok=True)
        evidence_episode_ids = sorted(
            {
                str(metadata["episode_id"])
                for item in evidence_rows
                for metadata in [_loads(item["metadata_json"], {})]
                if metadata.get("episode_id")
            }
        )
        return {
            "id": row["id"],
            "owner_user_id": row["owner_user_id"],
            "agent_id": row["agent_id"],
            "type": row["type"],
            "summary": row["summary"],
            "content": row["summary"],
            "tags": _loads(row["tags_json"], []),
            "status": row["status"],
            "confidence": row["confidence"],
            "importance": row["importance"],
            "sensitivity": row["sensitivity"],
            "evidence_ids": [item["evidence_id"] for item in evidence_rows],
            "evidence_episode_ids": evidence_episode_ids,
            "supersedes_id": row["supersedes_id"],
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "retention_policy": row["retention_policy"],
            "retrieval_permission": row["retrieval_permission"],
            "use_permission": row["use_permission"],
            "disclosure_permission": row["disclosure_permission"],
            "surface_permission": row_value(row, "surface_permission", "auto") or "auto",
            "surface_cooldown_until": row_value(row, "surface_cooldown_until"),
            "relationship_impact": row_value(row, "relationship_impact", 0.0) or 0.0,
            "schema_version": row["schema_version"],
            "index_watermark": row_value(row, "index_watermark"),
            "access_count": row_value(row, "access_count", 0) or 0,
            "last_accessed_at": row_value(row, "last_accessed_at"),
            "surface_count": row_value(row, "surface_count", 0) or 0,
            "last_surfaced_at": row_value(row, "last_surfaced_at"),
            "surface_feedback": surface_feedback,
            "surface_consent": surface_consent,
            "lifecycle": lifecycle,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _surface_feedback_for_memory(self, memory_id: str) -> JsonDict:
        rows = self._conn.execute(
            """
            SELECT * FROM memory_surface_feedback
            WHERE memory_id = ?
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (memory_id,),
        ).fetchall()
        if not rows:
            return {"score": 0.0, "positive_count": 0, "negative_count": 0, "last_feedback_at": None}
        score = 0.0
        positive = 0
        negative = 0
        for row in rows:
            value = row_value(row, "value")
            if value is None:
                value = _feedback_default_value(str(row["feedback_type"]))
            value = float(value)
            score += value
            if value > 0:
                positive += 1
            elif value < 0:
                negative += 1
        normalized = max(-1.0, min(1.0, score / max(len(rows), 1)))
        return {
            "score": round(normalized, 4),
            "positive_count": positive,
            "negative_count": negative,
            "last_feedback_at": rows[0]["created_at"],
        }

    def _surface_consent_for_memory(self, memory_id: str) -> JsonDict:
        rows = self._conn.execute(
            """
            SELECT * FROM memory_surface_consent
            WHERE memory_id = ?
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (memory_id, _utc_now()),
        ).fetchall()
        granted = next((row for row in rows if row["status"] == "granted"), None)
        denied = next((row for row in rows if row["status"] in {"denied", "revoked"}), None)
        return {
            "granted": granted is not None and denied is None,
            "granted_at": row_value(granted, "granted_at") if granted is not None else None,
            "denied": denied is not None and granted is None,
            "denied_at": row_value(denied, "created_at") if denied is not None else None,
        }

    @staticmethod
    def _candidate_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "candidate_id": row["id"],
            "id": row["id"],
            "type": row["type"],
            "summary": row["summary"],
            "tags": _loads(row["tags_json"], []),
            "status": row["status"],
            "confidence": row["confidence"],
            "sensitivity": row["sensitivity"],
            "reason": row["reason"],
            "evidence_ids": _loads(row["evidence_ids_json"], []),
            "supersedes_id": row["supersedes_id"],
            "explicit_user_request": bool(row["explicit_user_request"]),
            "local_only": bool(row["local_only"]),
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "committed_memory_id": row["committed_memory_id"],
            "operation": row_value(row, "operation", "ADD") or "ADD",
            "target_memory_id": row_value(row, "target_memory_id"),
            "importance": row_value(row, "importance", 0.5) or 0.5,
            "stability": row_value(row, "stability", 0.5) or 0.5,
            "specificity": row_value(row, "specificity", 0.5) or 0.5,
            "novelty": row_value(row, "novelty", 1.0) or 1.0,
            "conflict_score": row_value(row, "conflict_score", 0.0) or 0.0,
            "extraction_job_id": row_value(row, "extraction_job_id"),
            "schema_version": row_value(row, "schema_version", "legacy") or "legacy",
            "candidate_hash": row_value(row, "candidate_hash"),
            "gate_decision_id": row_value(row, "gate_decision_id"),
            "quarantine_id": row_value(row, "quarantine_id"),
            "deferred_until": row_value(row, "deferred_until"),
            "trace": _loads(row_value(row, "trace_json", "{}"), {}),
        }
