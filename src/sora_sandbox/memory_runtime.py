from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from .memory_index_fts import hybrid_search, index_text as _index_text
from .memory_index_fts import search_tokens
from .memory_recall_planner import RecallPlanner, RuleRecallPlanner
from .memory_schema import (
    JsonDict,
    RecallPlan,
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
from .memory_surface import build_recollection_items, choose_surface_action, decision_reason
from .memory_trace import record_recall_trace
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

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.gate = self.gate or MemoryGate()
        self.recall_planner = self.recall_planner or RuleRecallPlanner()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()
        self.ingestion = EpisodeIngestionService(self._conn)
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

    def reset(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                DELETE FROM recall_trace_item;
                DELETE FROM recall_trace;
                DELETE FROM memory_revision;
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
            evidence_id = self.add_evidence(
                source_type="legacy_state_import",
                content_summary=summary,
                sensitivity=sensitivity,
                retention_policy=self.gate.retention_policy(sensitivity),
                metadata={"legacy_memory_id": memory_id},
            )
            self._create_memory(
                memory_id=memory_id,
                memory_type=str(item.get("type") or "preference"),
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
                    id, source_type, content_summary, raw_content, raw_pointer, content_hash,
                    visibility, sensitivity, retention_policy, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    source_type,
                    content_summary,
                    stored_raw_content,
                    raw_pointer,
                    content_hash,
                    visibility,
                    sensitivity,
                    retention_policy,
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
        plan = RecallPlan(
            query_text=query_text,
            top_k=top_k,
            tokens=search_tokens(query_text),
            salient_terms=[],
            explicit_recall=False,
            backends=("fts", "vector_local", "temporal"),
            service_budget_ms=500,
            planner_mode="extractor_related_memory",
        )
        if not plan.tokens:
            return []
        with self._lock:
            hits = hybrid_search(
                self._conn,
                owner_user_id=self.owner_user_id,
                agent_id=self.agent_id,
                plan=plan,
            )
            related: list[JsonDict] = []
            for hit in hits[:top_k]:
                memory = self._memory_from_row(hit.row)
                memory["related_score"] = hit.score
                memory["matched_backends"] = list(hit.matched_backends)
                related.append(memory)
            return related

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
            if event_id not in event_ids and episode_id != metadata_episode_id:
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
                        memory = duplicate
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
        self._sync_fts(memory_id, summary, memory_type, tags)
        memory = self.get_memory(memory_id)
        self._append_revision(
            memory_id,
            action="created",
            before=None,
            after=memory,
            reason=reason,
            actor=actor,
        )
        self._conn.commit()
        return memory

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

    def _restore_memory_for_rollback(self, memory_id: str, *, reason: str, actor: str) -> JsonDict:
        before = self.get_memory(memory_id)
        self._conn.execute(
            "UPDATE memory_record SET status = 'active', updated_at = ? WHERE id = ?",
            (_utc_now(), memory_id),
        )
        after = self.get_memory(memory_id)
        self._sync_fts(after["id"], after["summary"], after["type"], after["tags"])
        self._append_revision(
            memory_id,
            action="rollback_restore",
            before=before,
            after=after,
            reason=reason,
            actor=actor,
        )
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
            self._sync_fts(after["id"], after["summary"], after["type"], after["tags"])
            self._append_revision(
                memory_id,
                action="restored",
                before=before,
                after=after,
                reason="manual_restore",
                actor="manual_repl",
            )
            self._conn.commit()
        return after

    def recall(
        self,
        query_text: str,
        *,
        turn_frame: JsonDict | None = None,
        context_use_policy: JsonDict | None = None,
        top_k: int = 3,
    ) -> JsonDict:
        import time

        started = time.perf_counter()
        plan = self.recall_planner.plan(
            query_text,
            turn_frame=turn_frame,
            context_use_policy=context_use_policy,
            top_k=top_k,
        )
        if plan.skipped_reason:
            return self._empty_recollection(
                query_text,
                plan.skipped_reason,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        with self._lock:
            fused_hits = hybrid_search(
                self._conn,
                owner_user_id=self.owner_user_id,
                agent_id=self.agent_id,
                plan=plan,
            )
            selected: list[tuple[float, JsonDict, tuple[str, ...]]] = [
                (hit.score, self._memory_from_row(hit.row), hit.matched_backends)
                for hit in fused_hits[:top_k]
            ]

        surface_action = choose_surface_action(plan, selected)
        items = build_recollection_items(selected)
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        reason = decision_reason(surface_action, bool(items))
        trace_id = self._record_recall_trace(
            query_text,
            surface_action=surface_action,
            reason=reason,
            selected=[(score, memory) for score, memory, _ in selected],
            latency_ms=latency_ms,
        )
        return {
            "surface_action": surface_action,
            "items": items,
            "retrieved_ids": [item["memory_id"] for item in items],
            "memory_summaries": [item["gist"] for item in items],
            "evidence_level": "strong" if items else "none",
            "forbidden_disclosures": ["raw_evidence"],
            "decision_reason": reason,
            "recall_trace_id": trace_id,
            "backend": "hybrid_rrf",
            "recall_plan": {
                "mode": plan.planner_mode,
                "backends": list(plan.backends),
                "salient_terms": list(plan.salient_terms),
                "explicit_recall": plan.explicit_recall,
                "service_budget_ms": plan.service_budget_ms,
            },
            "degraded": False,
            "latency_ms": latency_ms,
        }

    def _empty_recollection(self, query_text: str, reason: str, *, latency_ms: float) -> JsonDict:
        trace_id = self._record_recall_trace(
            query_text,
            surface_action="none",
            reason=reason,
            selected=[],
            latency_ms=round(latency_ms, 2),
        )
        return {
            "surface_action": "none",
            "items": [],
            "retrieved_ids": [],
            "memory_summaries": [],
            "evidence_level": "none",
            "forbidden_disclosures": [],
            "decision_reason": reason,
            "recall_trace_id": trace_id,
            "backend": "hybrid_rrf",
            "recall_plan": {
                "mode": "rules",
                "backends": [],
                "salient_terms": [],
                "explicit_recall": False,
                "service_budget_ms": 500,
            },
            "degraded": False,
            "latency_ms": round(latency_ms, 2),
        }

    def _record_recall_trace(
        self,
        query_text: str,
        *,
        surface_action: str,
        reason: str,
        selected: list[tuple[float, JsonDict]],
        latency_ms: float,
    ) -> str:
        with self._lock:
            return record_recall_trace(
                self._conn,
                query_text,
                backend="hybrid_rrf",
                surface_action=surface_action,
                reason=reason,
                selected=selected,
                latency_ms=latency_ms,
            )

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

    def evidence_for_memory(self, memory_id: str) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT e.*
                FROM evidence e
                JOIN memory_evidence me ON me.evidence_id = e.id
                WHERE me.memory_id = ?
                ORDER BY e.created_at DESC
                """,
                (memory_id,),
            ).fetchall()
        return [evidence_from_row(row) for row in rows]

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
        return {
            "status": "ok",
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
                "SELECT evidence_id FROM memory_evidence WHERE memory_id = ? ORDER BY evidence_id",
                (row["id"],),
            ).fetchall()
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
            "supersedes_id": row["supersedes_id"],
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "retention_policy": row["retention_policy"],
            "retrieval_permission": row["retrieval_permission"],
            "use_permission": row["use_permission"],
            "disclosure_permission": row["disclosure_permission"],
            "schema_version": row["schema_version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
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
