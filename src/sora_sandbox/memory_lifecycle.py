from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .memory_index_fts import search_tokens
from .memory_schema import JsonDict, json_dumps, json_loads, new_id, utc_now


LIFECYCLE_POLICY_VERSION = "memory_lifecycle_policy.v1"
CONSOLIDATION_PLANNER_VERSION = "memory_consolidation_planner.v1"
RUN_MODES = {"dry_run", "heat_only", "consolidate", "retention", "forget", "backfill"}

NEGATIVE_FEEDBACK_TYPES = {
    "too_much",
    "not_relevant",
    "bad_surface",
    "annoying",
    "wrong_context",
    "wrong",
    "incorrect",
    "hide",
    "forget",
}
POSITIVE_FEEDBACK_TYPES = {"helpful", "useful", "use_more", "good_surface", "relevant", "correct"}

STABLE_TYPES = {"persona_delta", "preference", "domain_principle", "project_principle", "profile", "sensitive_boundary"}
EPISODIC_TYPES = {"episode", "shared_episode", "worldops_episode"}
TEMPORAL_TYPES = {"world_ops", "worldops", "short_term_state", "temporary_state"}
TYPE_HALF_LIFE_DAYS = {
    "persona_delta": 365.0,
    "profile": 365.0,
    "domain_principle": 365.0,
    "project_principle": 365.0,
    "sensitive_boundary": 365.0,
    "preference": 180.0,
    "relationship_signal": 120.0,
    "episode": 60.0,
    "shared_episode": 60.0,
    "worldops_episode": 60.0,
    "world_ops": 30.0,
    "worldops": 30.0,
    "short_term_state": 14.0,
    "temporary_state": 14.0,
    "temporary_fact": 14.0,
    "task_or_session": 7.0,
}

POSITIVE_TERMS = ("喜欢", "偏好", "想要", "希望", "prefer", "prefers", "like", "likes", "love")
NEGATIVE_TERMS = (
    "不喜欢",
    "不再喜欢",
    "不想要",
    "不希望",
    "讨厌",
    "戒了",
    "不是",
    "不要",
    "no longer",
    "do not",
    "don't",
    "not prefer",
    "dislike",
)


@dataclass(frozen=True)
class HeatSnapshot:
    memory_id: str
    heat_score: float
    tier_before: str | None
    tier_after: str
    usefulness_score: float
    evidence_strength: float
    recent_negative_feedback: float
    stale_penalty: float
    last_validation_at: str | None
    signals: JsonDict
    reasons: list[str]


class MemoryLifecycleService:
    """Heat, consolidation, forgetting, and lifecycle state for canonical memory."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        lock: Any,
        owner_user_id: str,
        agent_id: str,
        get_memory: Callable[[str], JsonDict],
        list_memories: Callable[..., list[JsonDict]],
        evidence_for_memory: Callable[[str], list[JsonDict]],
        propose_candidate: Callable[..., JsonDict],
        run_candidate_gate: Callable[..., JsonDict],
        append_revision: Callable[..., None],
        retention_process: Callable[..., JsonDict] | None = None,
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._owner_user_id = owner_user_id
        self._agent_id = agent_id
        self._get_memory = get_memory
        self._list_memories = list_memories
        self._evidence_for_memory = evidence_for_memory
        self._propose_candidate = propose_candidate
        self._run_candidate_gate = run_candidate_gate
        self._append_revision = append_revision
        self._retention_process = retention_process

    def attach_retention_process(self, retention_process: Callable[..., JsonDict]) -> None:
        self._retention_process = retention_process

    def run(
        self,
        *,
        mode: str = "dry_run",
        now: str | None = None,
        limit: int = 200,
        actor: str = "lifecycle_worker",
        create_consolidation_candidates: bool | None = None,
    ) -> JsonDict:
        if mode not in RUN_MODES:
            raise ValueError(f"invalid lifecycle mode: {mode}")
        now_dt = _parse_time(now) if now else datetime.now(timezone.utc)
        apply_state = mode in {"heat_only", "consolidate", "retention", "forget", "backfill"}
        should_consolidate = mode in {"consolidate", "backfill"}
        should_retention = mode in {"retention", "backfill"}
        create_candidates = should_consolidate if create_consolidation_candidates is None else create_consolidation_candidates
        run_id = new_id("heat_run")
        started_at = now_dt.isoformat()
        config = {
            "mode": mode,
            "limit": limit,
            "policy_version": LIFECYCLE_POLICY_VERSION,
            "apply_state": apply_state,
            "create_consolidation_candidates": create_candidates,
        }
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_heat_run (
                    id, mode, status, policy_version, total_count, scored_count,
                    migrated_count, proposed_count, archived_count, forgotten_count,
                    config_json, summary_json, created_at
                ) VALUES (?, ?, 'running', ?, 0, 0, 0, 0, 0, 0, ?, '{}', ?)
                """,
                (run_id, mode, LIFECYCLE_POLICY_VERSION, json_dumps(config), started_at),
            )
            self._conn.commit()

        try:
            memories = self._active_memories(limit=limit)
            snapshots = [self.score_memory(memory["id"], now=now_dt.isoformat()) for memory in memories]
            migrated_count = 0
            archived_count = 0
            with self._lock:
                for snapshot in snapshots:
                    self._record_snapshot(run_id, snapshot, created_at=started_at)
                    if apply_state:
                        changed = self._upsert_lifecycle_state(snapshot, now=started_at, actor=actor)
                        if changed:
                            migrated_count += 1
                        if snapshot.tier_after == "archived" and snapshot.tier_before != "archived":
                            archived_count += 1
                self._conn.commit()

            consolidation = {"planned_count": 0, "candidate_count": 0, "items": []}
            if should_consolidate:
                consolidation = self.plan_consolidation(
                    run_id=run_id,
                    now=now_dt.isoformat(),
                    limit=limit,
                    create_candidates=create_candidates,
                    actor=actor,
                )

            retention = {"status": "skipped"}
            if should_retention and self._retention_process is not None:
                retention = self._retention_process(now=now_dt.isoformat(), limit=limit)

            tombstones = self.process_tombstones(now=now_dt.isoformat()) if mode in {"forget", "backfill"} else {"status": "skipped"}
            summary = {
                "mode": mode,
                "scored_count": len(snapshots),
                "migrated_count": migrated_count,
                "archived_count": archived_count,
                "tier_counts": self.lifecycle_tier_counts(),
                "consolidation": {
                    "planned_count": consolidation["planned_count"],
                    "candidate_count": consolidation["candidate_count"],
                },
                "retention": retention,
                "tombstones": tombstones,
                "heat_distribution": self.heat_distribution(snapshots),
            }
            with self._lock:
                self._conn.execute(
                    """
                    UPDATE memory_heat_run
                    SET status = 'completed', total_count = ?, scored_count = ?,
                        migrated_count = ?, proposed_count = ?, archived_count = ?,
                        forgotten_count = ?, summary_json = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (
                        len(memories),
                        len(snapshots),
                        migrated_count,
                        consolidation["candidate_count"],
                        archived_count,
                        int(tombstones.get("deleted_count") or 0),
                        json_dumps(summary),
                        utc_now(),
                        run_id,
                    ),
                )
                self._conn.commit()
            return {
                "run_id": run_id,
                "status": "completed",
                "mode": mode,
                "summary": summary,
                "snapshots": [self._snapshot_dict(snapshot) for snapshot in snapshots],
                "consolidation": consolidation,
            }
        except Exception as exc:
            with self._lock:
                self._conn.execute(
                    """
                    UPDATE memory_heat_run
                    SET status = 'failed', error = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (str(exc), utc_now(), run_id),
                )
                self._conn.commit()
            raise

    def score_memory(self, memory_id: str, *, now: str | None = None) -> HeatSnapshot:
        now_dt = _parse_time(now) if now else datetime.now(timezone.utc)
        memory = self._get_memory(memory_id)
        evidence = self._evidence_for_memory(memory_id)
        state = self.get_lifecycle_state(memory_id, missing_ok=True)
        tier_before = state.get("tier") if state else None
        signals = self._collect_signals(memory, evidence, now_dt)
        positive = (
            0.20 * signals["recency"]
            + 0.13 * signals["frequency"]
            + 0.15 * signals["importance"]
            + 0.11 * signals["confidence"]
            + 0.12 * signals["usefulness_score"]
            + 0.10 * signals["stability"]
            + 0.07 * signals["relationship"]
            + 0.06 * signals["evidence_strength"]
            + 0.06 * signals["explicit_user_pin"]
        )
        penalties = (
            0.10 * signals["exposure_penalty"]
            + 0.08 * signals["stale_penalty"]
            + 0.06 * signals["negative_surface_penalty"]
        )
        heat_score = _clamp(positive - penalties)
        tier_after = self._tier_for_score(heat_score, tier_before, signals)
        reasons = self._migration_reasons(tier_before, tier_after, heat_score, signals)
        return HeatSnapshot(
            memory_id=memory_id,
            heat_score=round(heat_score, 6),
            tier_before=tier_before,
            tier_after=tier_after,
            usefulness_score=round(signals["usefulness_score"], 6),
            evidence_strength=round(signals["evidence_strength"], 6),
            recent_negative_feedback=round(signals["recent_negative_feedback"], 6),
            stale_penalty=round(signals["stale_penalty"], 6),
            last_validation_at=signals.get("last_validation_at"),
            signals=signals,
            reasons=reasons,
        )

    def score_memory_public(self, memory_id: str, *, now: str | None = None) -> JsonDict:
        return self._snapshot_dict(self.score_memory(memory_id, now=now))

    def plan_consolidation(
        self,
        *,
        run_id: str | None = None,
        now: str | None = None,
        limit: int = 200,
        create_candidates: bool = False,
        actor: str = "lifecycle_worker",
    ) -> JsonDict:
        memories = [
            memory
            for memory in self._active_memories(limit=limit)
            if (self.get_lifecycle_state(memory["id"], missing_ok=True) or {}).get("tier") != "archived"
        ]
        candidates: list[JsonDict] = []
        seen_pairs: set[tuple[str, str]] = set()
        for left, right in self._candidate_pairs(memories):
            pair = tuple(sorted([left["id"], right["id"]]))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            plan = self._plan_pair(left, right)
            if not plan:
                continue
            plan_id = self._record_consolidation_plan(run_id, plan)
            if create_candidates and plan["action"] in {"MERGE", "UPDATE"}:
                candidate = self._create_consolidation_candidate(plan, actor=actor)
                self._conn.execute(
                    """
                    UPDATE memory_consolidation_plan
                    SET candidate_id = ?, status = ?
                    WHERE id = ?
                    """,
                    (
                        candidate.get("candidate_id") or candidate.get("id"),
                        "candidate_created" if candidate.get("candidate_id") else "candidate_rejected",
                        plan_id,
                    ),
                )
                if candidate.get("candidate_id"):
                    gate = self._run_candidate_gate(candidate["candidate_id"], actor=actor)
                    candidate = {**candidate, "gate": gate}
                candidates.append(candidate)
        self._conn.commit()
        plans = self.list_consolidation_plans(run_id=run_id, limit=limit)
        return {
            "status": "planned",
            "run_id": run_id,
            "planned_count": len(plans),
            "candidate_count": len([item for item in candidates if item.get("candidate_id")]),
            "items": plans,
            "candidates": candidates,
        }

    def record_tombstone(
        self,
        *,
        target_type: str,
        target_id: str,
        content: str,
        reason: str,
        actor: str,
        metadata: JsonDict | None = None,
        now: str | None = None,
        ttl_days: int = 180,
    ) -> JsonDict:
        created_at = now or utc_now()
        normalized = _normalize_text(content)
        tombstone_id = new_id("tomb")
        expires_at = (_parse_time(created_at) + timedelta(days=ttl_days)).isoformat()
        row = {
            "id": tombstone_id,
            "target_type": target_type,
            "target_id": target_id,
            "content_hash": _hash(normalized),
            "entity_hash": _hash(_entity_signature(content)),
            "reason": reason,
            "actor": actor,
            "retention_stage": "full_hash",
            "expires_at": expires_at,
            "metadata": metadata or {},
            "created_at": created_at,
        }
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_tombstone (
                    id, target_type, target_id, content_hash, entity_hash, reason,
                    actor, retention_stage, expires_at, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["target_type"],
                    row["target_id"],
                    row["content_hash"],
                    row["entity_hash"],
                    row["reason"],
                    row["actor"],
                    row["retention_stage"],
                    row["expires_at"],
                    json_dumps(row["metadata"]),
                    row["created_at"],
                ),
            )
            self._conn.commit()
        return row

    def find_tombstone_match(self, content: str, *, now: str | None = None) -> JsonDict | None:
        normalized = _normalize_text(content)
        content_hash = _hash(normalized)
        entity_hash = _hash(_entity_signature(content))
        cutoff = now or utc_now()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT *
                FROM memory_tombstone
                WHERE (expires_at IS NULL OR expires_at > ?)
                  AND (
                    content_hash = ?
                    OR (retention_stage IN ('entity_guard', 'entity_hash_only', 'full_hash') AND entity_hash = ?)
                  )
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (cutoff, content_hash, entity_hash),
            ).fetchone()
        return self._tombstone_from_row(row) if row else None

    def process_tombstones(self, *, now: str | None = None) -> JsonDict:
        now_dt = _parse_time(now) if now else datetime.now(timezone.utc)
        entity_cutoff = (now_dt - timedelta(days=90)).isoformat()
        delete_cutoff = now_dt.isoformat()
        with self._lock:
            compacted = self._conn.execute(
                """
                UPDATE memory_tombstone
                SET retention_stage = 'entity_guard'
                WHERE retention_stage IN ('full_hash', 'entity_hash_only')
                  AND created_at <= ?
                """,
                (entity_cutoff,),
            ).rowcount
            rows = self._conn.execute(
                """
                SELECT id FROM memory_tombstone
                WHERE expires_at IS NOT NULL AND expires_at <= ?
                """,
                (delete_cutoff,),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self._conn.execute(f"DELETE FROM memory_tombstone WHERE id IN ({placeholders})", ids)
            self._conn.commit()
        return {"status": "ok", "compacted_count": compacted, "deleted_count": len(ids), "deleted_ids": ids}

    def lifecycle_health(self) -> JsonDict:
        with self._lock:
            tier_rows = self._conn.execute(
                "SELECT tier, COUNT(*) AS count FROM memory_lifecycle_state GROUP BY tier"
            ).fetchall()
            pending_plans = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM memory_consolidation_plan WHERE status IN ('planned', 'candidate_created')"
                ).fetchone()[0]
            )
            tombstones = int(self._conn.execute("SELECT COUNT(*) FROM memory_tombstone").fetchone()[0])
            last_run = self._conn.execute(
                "SELECT * FROM memory_heat_run ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return {
            "policy_version": LIFECYCLE_POLICY_VERSION,
            "tier_counts": {row["tier"]: row["count"] for row in tier_rows},
            "pending_consolidation_plans": pending_plans,
            "tombstones": tombstones,
            "last_run": self._heat_run_from_row(last_run) if last_run else None,
        }

    def lifecycle_tier_counts(self) -> JsonDict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT tier, COUNT(*) AS count FROM memory_lifecycle_state GROUP BY tier"
            ).fetchall()
        return {row["tier"]: row["count"] for row in rows}

    def list_lifecycle_states(self, *, tier: str | None = None, limit: int = 100) -> list[JsonDict]:
        sql = """
            SELECT s.*, m.summary, m.type
            FROM memory_lifecycle_state s
            JOIN memory_record m ON m.id = s.memory_id
            WHERE 1 = 1
        """
        params: list[Any] = []
        if tier:
            sql += " AND s.tier = ?"
            params.append(tier)
        sql += " ORDER BY s.heat_score DESC, s.last_scored_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._state_from_row(row) for row in rows]

    def get_lifecycle_state(self, memory_id: str, *, missing_ok: bool = False) -> JsonDict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_lifecycle_state WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
        if not row:
            if missing_ok:
                return {}
            raise ValueError(f"lifecycle state not found: {memory_id}")
        return self._state_from_row(row)

    def list_heat_runs(self, *, limit: int = 20) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memory_heat_run ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._heat_run_from_row(row) for row in rows]

    def list_heat_snapshots(self, *, run_id: str | None = None, memory_id: str | None = None, limit: int = 100) -> list[JsonDict]:
        sql = "SELECT * FROM memory_heat_snapshot WHERE 1 = 1"
        params: list[Any] = []
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        if memory_id:
            sql += " AND memory_id = ?"
            params.append(memory_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._snapshot_row(row) for row in rows]

    def list_consolidation_plans(self, *, run_id: str | None = None, status: str | None = None, limit: int = 100) -> list[JsonDict]:
        sql = "SELECT * FROM memory_consolidation_plan WHERE 1 = 1"
        params: list[Any] = []
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._consolidation_from_row(row) for row in rows]

    def list_tombstones(self, *, limit: int = 100) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memory_tombstone ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._tombstone_from_row(row) for row in rows]

    def heat_distribution(self, snapshots: list[HeatSnapshot] | None = None) -> JsonDict:
        if snapshots is None:
            with self._lock:
                rows = self._conn.execute("SELECT heat_score, tier FROM memory_lifecycle_state").fetchall()
            values = [float(row["heat_score"]) for row in rows]
            tiers = [str(row["tier"]) for row in rows]
        else:
            values = [snapshot.heat_score for snapshot in snapshots]
            tiers = [snapshot.tier_after for snapshot in snapshots]
        buckets = {"0.00-0.20": 0, "0.20-0.45": 0, "0.45-0.72": 0, "0.72-1.00": 0}
        for value in values:
            if value < 0.20:
                buckets["0.00-0.20"] += 1
            elif value < 0.45:
                buckets["0.20-0.45"] += 1
            elif value < 0.72:
                buckets["0.45-0.72"] += 1
            else:
                buckets["0.72-1.00"] += 1
        return {
            "count": len(values),
            "min": round(min(values), 6) if values else None,
            "max": round(max(values), 6) if values else None,
            "avg": round(sum(values) / len(values), 6) if values else None,
            "buckets": buckets,
            "tier_counts": {tier: tiers.count(tier) for tier in sorted(set(tiers))},
        }

    def _collect_signals(
        self,
        memory: JsonDict,
        evidence: list[JsonDict],
        now_dt: datetime,
    ) -> JsonDict:
        access_count = int(memory.get("access_count") or 0)
        last_accessed = _parse_time(memory.get("last_accessed_at") or memory.get("updated_at") or memory.get("created_at"))
        age_days = max((now_dt - last_accessed).total_seconds() / 86400, 0.0)
        half_life = _type_half_life_days(str(memory.get("type") or ""))
        recency = math.exp(-age_days / max(half_life, 1.0))
        frequency = 1 - math.exp(-access_count / 8)
        usefulness, recent_negative, feedback_trace = self._usefulness(memory["id"], now_dt)
        last_validation_at = self._last_validation_at(memory, evidence, feedback_trace)
        stale_days = max((now_dt - _parse_time(last_validation_at)).total_seconds() / 86400, 0.0) if last_validation_at else 365.0
        stale_half_life = _stale_half_life_days(str(memory.get("type") or ""))
        stale_penalty = 1 - math.exp(-stale_days / max(stale_half_life, 1.0))
        evidence_strength = self._evidence_strength(evidence)
        exposure_penalty = min(float(memory.get("surface_count") or 0) / 12.0, 1.0)
        if usefulness > 0.65:
            exposure_penalty *= 0.5
        signals = {
            "recency": _clamp(recency),
            "frequency": _clamp(frequency),
            "importance": _clamp(float(memory.get("importance") or 0.0)),
            "confidence": _clamp(float(memory.get("confidence") or 0.0)),
            "usefulness_score": usefulness,
            "stability": _type_stability(str(memory.get("type") or "")),
            "relationship": _clamp(float(memory.get("relationship_impact") or 0.0)),
            "evidence_strength": evidence_strength,
            "explicit_user_pin": self._explicit_pin(memory["id"], memory),
            "exposure_penalty": _clamp(exposure_penalty),
            "stale_penalty": _clamp(stale_penalty),
            "negative_surface_penalty": _clamp(abs(min(recent_negative, 0.0))),
            "recent_negative_feedback": recent_negative,
            "last_validation_at": last_validation_at,
            "age_days_since_access": round(age_days, 4),
            "days_since_last_validation": round(stale_days, 4),
            "feedback_trace": feedback_trace,
            "evidence_count": len(evidence),
            "policy_version": LIFECYCLE_POLICY_VERSION,
        }
        return signals

    def _usefulness(self, memory_id: str, now_dt: datetime) -> tuple[float, float, JsonDict]:
        rows = self._conn.execute(
            """
            SELECT feedback_type, value, created_at
            FROM memory_surface_feedback
            WHERE memory_id = ?
            ORDER BY created_at DESC
            LIMIT 50
            """,
            (memory_id,),
        ).fetchall()
        weighted = 0.0
        weight_sum = 0.0
        negative_weighted = 0.0
        negative_weight_sum = 0.0
        last_positive_at = None
        last_negative_at = None
        for row in rows:
            created = _parse_time(row["created_at"])
            age = max((now_dt - created).total_seconds() / 86400, 0.0)
            if age > 30:
                continue
            value = row["value"]
            if value is None:
                value = _feedback_default_value(str(row["feedback_type"]))
            value = _clamp(float(value), -1.0, 1.0)
            weight = math.exp(-age / 7)
            weighted += value * weight
            weight_sum += weight
            if value > 0 and last_positive_at is None:
                last_positive_at = row["created_at"]
            if value < 0:
                negative_weighted += value * weight
                negative_weight_sum += weight
                if last_negative_at is None:
                    last_negative_at = row["created_at"]
        access_rows = self._conn.execute(
            """
            SELECT surface_action, accessed_at
            FROM memory_access_signal
            WHERE memory_id = ?
            ORDER BY accessed_at DESC
            LIMIT 30
            """,
            (memory_id,),
        ).fetchall()
        implicit_events = 0
        for row in access_rows:
            action = str(row["surface_action"] or "")
            if action in {"gist", "implicit", "answer_memory_question", "implicit_continuity"}:
                age = max((now_dt - _parse_time(row["accessed_at"])).total_seconds() / 86400, 0.0)
                if age <= 14:
                    implicit_events += 1
        implicit_success = 0.0 if negative_weight_sum else min(0.30, implicit_events * 0.05)
        feedback_avg = weighted / weight_sum if weight_sum else 0.0
        raw = _clamp(feedback_avg + implicit_success, -1.0, 1.0)
        usefulness = _clamp(0.5 + 0.5 * raw)
        recent_negative = negative_weighted / negative_weight_sum if negative_weight_sum else 0.0
        return usefulness, round(recent_negative, 6), {
            "feedback_avg": round(feedback_avg, 6),
            "implicit_success": round(implicit_success, 6),
            "last_positive_feedback_at": last_positive_at,
            "last_negative_feedback_at": last_negative_at,
            "recent_negative_feedback": round(recent_negative, 6),
        }

    def _last_validation_at(
        self,
        memory: JsonDict,
        evidence: list[JsonDict],
        feedback_trace: JsonDict,
    ) -> str | None:
        values = [
            memory.get("created_at"),
            feedback_trace.get("last_positive_feedback_at"),
        ]
        values.extend(item.get("created_at") for item in evidence if not item.get("deleted_at"))
        rows = self._conn.execute(
            """
            SELECT created_at
            FROM memory_revision
            WHERE memory_id = ?
              AND action IN ('created', 'restored', 'evidence_linked', 'idempotent_evidence_merged', 'evidence_merged')
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (memory["id"],),
        ).fetchall()
        values.extend(row["created_at"] for row in rows)
        parsed = [_parse_time(value) for value in values if value]
        return max(parsed).isoformat() if parsed else None

    def _evidence_strength(self, evidence: list[JsonDict]) -> float:
        active = [item for item in evidence if not item.get("deleted_at")]
        if not active:
            return 0.0
        source_types = {str(item.get("source_type") or "") for item in active}
        directness = max(_evidence_directness(item) for item in active)
        independent = min(math.log1p(len(active)) / math.log1p(5), 1.0)
        diversity = min(len(source_types) / 3.0, 1.0)
        return _clamp(0.45 * directness + 0.35 * independent + 0.20 * diversity)

    def _explicit_pin(self, memory_id: str, memory: JsonDict) -> float:
        if memory.get("surface_permission") == "allow":
            return 1.0
        row = self._conn.execute(
            """
            SELECT 1
            FROM memory_surface_override
            WHERE memory_id = ? AND status = 'active'
              AND override_type IN ('pin', 'always_use')
            LIMIT 1
            """,
            (memory_id,),
        ).fetchone()
        return 1.0 if row else 0.0

    def _tier_for_score(self, score: float, tier_before: str | None, signals: JsonDict) -> str:
        negative = float(signals.get("recent_negative_feedback") or 0.0)
        if negative <= -0.75:
            return "cold"
        if negative <= -0.50 and tier_before == "hot":
            return "warm"
        if signals.get("explicit_user_pin") and score >= 0.45:
            return "hot"
        if score >= 0.72:
            return "hot"
        if score >= 0.45:
            return "warm"
        if score >= 0.18:
            return "cold"
        return "archived" if tier_before == "cold" else "cold"

    def _migration_reasons(
        self,
        tier_before: str | None,
        tier_after: str,
        heat_score: float,
        signals: JsonDict,
    ) -> list[str]:
        reasons = [f"heat_score={heat_score:.3f}"]
        if tier_before != tier_after:
            reasons.append(f"tier:{tier_before or 'unscored'}->{tier_after}")
        if signals.get("recent_negative_feedback", 0.0) <= -0.5:
            reasons.append("recent_negative_feedback_accelerated_demotion")
        if signals.get("stale_penalty", 0.0) >= 0.7:
            reasons.append("stale_without_recent_validation")
        if signals.get("evidence_strength", 0.0) >= 0.75:
            reasons.append("strong_evidence_support")
        return reasons

    def _upsert_lifecycle_state(self, snapshot: HeatSnapshot, *, now: str, actor: str) -> bool:
        before = self.get_lifecycle_state(snapshot.memory_id, missing_ok=True)
        archived_at = now if snapshot.tier_after == "archived" and before.get("tier") != "archived" else before.get("archived_at")
        self._conn.execute(
            """
            INSERT INTO memory_lifecycle_state (
                memory_id, tier, heat_score, usefulness_score, evidence_strength,
                recent_negative_feedback, stale_penalty, last_validation_at,
                last_scored_at, archived_at, policy_version, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                tier = excluded.tier,
                heat_score = excluded.heat_score,
                usefulness_score = excluded.usefulness_score,
                evidence_strength = excluded.evidence_strength,
                recent_negative_feedback = excluded.recent_negative_feedback,
                stale_penalty = excluded.stale_penalty,
                last_validation_at = excluded.last_validation_at,
                last_scored_at = excluded.last_scored_at,
                archived_at = excluded.archived_at,
                policy_version = excluded.policy_version,
                metadata_json = excluded.metadata_json
            """,
            (
                snapshot.memory_id,
                snapshot.tier_after,
                snapshot.heat_score,
                snapshot.usefulness_score,
                snapshot.evidence_strength,
                snapshot.recent_negative_feedback,
                snapshot.stale_penalty,
                snapshot.last_validation_at,
                now,
                archived_at,
                LIFECYCLE_POLICY_VERSION,
                json_dumps({"signals": snapshot.signals, "reasons": snapshot.reasons}),
            ),
        )
        changed = bool(before) and before.get("tier") != snapshot.tier_after
        if changed or not before:
            memory = self._get_memory(snapshot.memory_id)
            self._append_revision(
                snapshot.memory_id,
                action="lifecycle_tier_changed" if changed else "lifecycle_scored",
                before={"lifecycle": before} if before else None,
                after={"lifecycle": self._snapshot_dict(snapshot), "memory": memory},
                reason=";".join(snapshot.reasons),
                actor=actor,
            )
        return changed

    def _record_snapshot(self, run_id: str, snapshot: HeatSnapshot, *, created_at: str) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO memory_heat_snapshot (
                run_id, memory_id, heat_score, tier_before, tier_after,
                usefulness_score, evidence_strength, recent_negative_feedback,
                stale_penalty, signals_json, reasons_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                snapshot.memory_id,
                snapshot.heat_score,
                snapshot.tier_before,
                snapshot.tier_after,
                snapshot.usefulness_score,
                snapshot.evidence_strength,
                snapshot.recent_negative_feedback,
                snapshot.stale_penalty,
                json_dumps(snapshot.signals),
                json_dumps(snapshot.reasons),
                created_at,
            ),
        )

    def _candidate_pairs(self, memories: list[JsonDict]) -> list[tuple[JsonDict, JsonDict]]:
        buckets: dict[str, list[JsonDict]] = {}
        for memory in memories:
            terms = set(search_tokens(" ".join([memory["summary"], memory["type"], " ".join(memory.get("tags", []))])))
            keys = {f"type:{memory['type']}"}
            keys.update(f"tag:{tag}" for tag in memory.get("tags", [])[:5])
            keys.update(f"term:{term}" for term in sorted(terms)[:8])
            for key in keys:
                buckets.setdefault(key, []).append(memory)
        pairs: list[tuple[JsonDict, JsonDict]] = []
        seen: set[tuple[str, str]] = set()
        for group in buckets.values():
            if len(group) < 2 or len(group) > 40:
                continue
            for index, left in enumerate(group):
                for right in group[index + 1 :]:
                    pair = tuple(sorted([left["id"], right["id"]]))
                    if pair in seen:
                        continue
                    seen.add(pair)
                    pairs.append((left, right))
        return pairs

    def _plan_pair(self, left: JsonDict, right: JsonDict) -> JsonDict | None:
        left_norm = _normalize_text(left["summary"])
        right_norm = _normalize_text(right["summary"])
        left_score = self.get_lifecycle_state(left["id"], missing_ok=True).get("heat_score", left.get("importance", 0.5))
        right_score = self.get_lifecycle_state(right["id"], missing_ok=True).get("heat_score", right.get("importance", 0.5))
        target, source = (left, right) if float(left_score) >= float(right_score) else (right, left)
        if left_norm == right_norm:
            return self._plan_payload(
                action="MERGE",
                cluster_key=f"duplicate:{_hash(left_norm)[:12]}",
                score=0.98,
                target=target,
                source=source,
                reasons=["exact_duplicate_summary"],
            )
        contradiction = _contradiction_score(left["summary"], right["summary"])
        if contradiction >= 0.70:
            winner, loser, basis = self._contradiction_winner(left, right)
            return self._plan_payload(
                action="UPDATE",
                cluster_key=f"contradiction:{_hash(_entity_signature(left['summary'] + right['summary']))[:12]}",
                score=contradiction,
                target=loser,
                source=winner,
                reasons=["contradiction_detected", *basis],
                payload={"winner_memory_id": winner["id"], "loser_memory_id": loser["id"], "winner_basis": basis},
            )
        similarity = _token_jaccard(left["summary"], right["summary"])
        if left["type"] == right["type"] and similarity >= 0.62:
            return self._plan_payload(
                action="MERGE",
                cluster_key=f"near_duplicate:{left['type']}:{_hash(_entity_signature(left['summary'] + right['summary']))[:12]}",
                score=similarity,
                target=target,
                source=source,
                reasons=["high_semantic_overlap", "same_memory_type"],
            )
        return None

    def _contradiction_winner(self, left: JsonDict, right: JsonDict) -> tuple[JsonDict, JsonDict, list[str]]:
        left_state = self.get_lifecycle_state(left["id"], missing_ok=True)
        right_state = self.get_lifecycle_state(right["id"], missing_ok=True)
        left_protected = self._protected_from_supersede(left, left_state)
        right_protected = self._protected_from_supersede(right, right_state)
        if left_protected and not right_protected:
            return left, right, ["protected_from_supersede"]
        if right_protected and not left_protected:
            return right, left, ["protected_from_supersede"]

        left_confirmation = self._user_confirmation_score(left, left_state)
        right_confirmation = self._user_confirmation_score(right, right_state)
        if right_confirmation > left_confirmation:
            return right, left, ["user_confirmation"]
        if left_confirmation > right_confirmation:
            return left, right, ["user_confirmation"]

        left_validation = self._validation_time(left, left_state)
        right_validation = self._validation_time(right, right_state)
        if right_validation > left_validation:
            return right, left, ["newer_evidence"]
        if left_validation > right_validation:
            return left, right, ["newer_evidence"]

        left_confidence = float(left.get("confidence") or 0.0)
        right_confidence = float(right.get("confidence") or 0.0)
        if right_confidence - left_confidence >= 0.10:
            return right, left, ["higher_confidence"]
        if left_confidence - right_confidence >= 0.10:
            return left, right, ["higher_confidence"]

        left_strength = self._memory_evidence_strength(left, left_state)
        right_strength = self._memory_evidence_strength(right, right_state)
        if right_strength - left_strength >= 0.10:
            return right, left, ["higher_evidence_strength"]
        if left_strength - right_strength >= 0.10:
            return left, right, ["higher_evidence_strength"]
        return left, right, ["stable_tie_breaker"]

    def _protected_from_supersede(self, memory: JsonDict, state: JsonDict) -> bool:
        metadata = state.get("metadata") or {}
        signals = metadata.get("signals") or {}
        lifecycle = memory.get("lifecycle") or {}
        tags = {str(tag) for tag in memory.get("tags", [])}
        return (
            _truthy(memory.get("protected_from_supersede"))
            or _truthy(lifecycle.get("protected_from_supersede"))
            or _truthy(metadata.get("protected_from_supersede"))
            or _truthy(signals.get("protected_from_supersede"))
            or "protected_from_supersede" in tags
            or "manual_verified" in tags
        )

    def _user_confirmation_score(self, memory: JsonDict, state: JsonDict) -> float:
        metadata = state.get("metadata") or {}
        signals = metadata.get("signals") or {}
        feedback_trace = signals.get("feedback_trace") or {}
        lifecycle = memory.get("lifecycle") or {}
        tags = {str(tag) for tag in memory.get("tags", [])}
        if (
            _truthy(memory.get("user_confirmation"))
            or _truthy(lifecycle.get("user_confirmation"))
            or _truthy(metadata.get("user_confirmation"))
            or _truthy(signals.get("user_confirmation"))
            or "user_confirmed" in tags
            or "confirmed_by_user" in tags
        ):
            return 1.0
        if feedback_trace.get("last_positive_feedback_at"):
            return 0.8
        return 0.0

    def _validation_time(self, memory: JsonDict, state: JsonDict) -> datetime:
        metadata = state.get("metadata") or {}
        signals = metadata.get("signals") or {}
        return _parse_time(
            state.get("last_validation_at")
            or signals.get("last_validation_at")
            or memory.get("updated_at")
            or memory.get("created_at")
        )

    def _memory_evidence_strength(self, memory: JsonDict, state: JsonDict) -> float:
        metadata = state.get("metadata") or {}
        signals = metadata.get("signals") or {}
        lifecycle = memory.get("lifecycle") or {}
        for value in (
            state.get("evidence_strength"),
            signals.get("evidence_strength"),
            lifecycle.get("evidence_strength"),
            memory.get("evidence_strength"),
        ):
            if value is not None:
                return _clamp(float(value))
        return 0.0

    def _plan_payload(
        self,
        *,
        action: str,
        cluster_key: str,
        score: float,
        target: JsonDict,
        source: JsonDict,
        reasons: list[str],
        payload: JsonDict | None = None,
    ) -> JsonDict:
        source_evidence = self._evidence_for_memory(source["id"])
        return {
            "action": action,
            "cluster_key": cluster_key,
            "score": round(score, 6),
            "target_memory_id": target["id"],
            "memory_ids": [target["id"], source["id"]],
            "evidence_ids": [item["id"] for item in source_evidence if item.get("id")],
            "summary": source["summary"] if action == "UPDATE" else target["summary"],
            "memory_type": target["type"],
            "confidence": min(float(source.get("confidence") or 0.8), 0.95),
            "importance": max(float(target.get("importance") or 0.5), float(source.get("importance") or 0.5)),
            "reasons": reasons,
            "payload": payload or {},
        }

    def _record_consolidation_plan(self, run_id: str | None, plan: JsonDict) -> str:
        plan_id = new_id("consol")
        self._conn.execute(
            """
            INSERT INTO memory_consolidation_plan (
                id, run_id, status, planner_version, cluster_key, action,
                target_memory_id, score, reasons_json, evidence_ids_json,
                memory_ids_json, payload_json, created_at
            ) VALUES (?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                run_id,
                CONSOLIDATION_PLANNER_VERSION,
                plan["cluster_key"],
                plan["action"],
                plan.get("target_memory_id"),
                plan["score"],
                json_dumps(plan.get("reasons", [])),
                json_dumps(plan.get("evidence_ids", [])),
                json_dumps(plan.get("memory_ids", [])),
                json_dumps(plan),
                utc_now(),
            ),
        )
        return plan_id

    def _create_consolidation_candidate(self, plan: JsonDict, *, actor: str) -> JsonDict:
        evidence_ids = list(plan.get("evidence_ids") or [])
        if not evidence_ids:
            return {"status": "discarded", "reason": "consolidation_missing_evidence"}
        operation = str(plan["action"])
        conflict_score = float(plan["score"]) if operation == "UPDATE" else 0.0
        return self._propose_candidate(
            summary=str(plan["summary"]),
            memory_type=str(plan["memory_type"]),
            evidence_ids=evidence_ids,
            tags=["lifecycle_consolidation"],
            confidence=float(plan.get("confidence") or 0.8),
            reason=f"lifecycle_consolidation:{operation.lower()}:{','.join(plan.get('reasons', []))}",
            explicit_user_request=False,
            operation=operation,
            target_memory_id=plan.get("target_memory_id"),
            supersedes_id=plan.get("target_memory_id") if operation == "UPDATE" else None,
            importance=float(plan.get("importance") or 0.5),
            stability=0.8 if operation == "MERGE" else 0.6,
            specificity=0.7,
            novelty=0.1 if operation == "MERGE" else 0.5,
            conflict_score=conflict_score,
            schema_version="lifecycle_consolidation.v1",
            trace={"consolidation_plan": plan, "actor": actor},
            allow_auto_commit=False,
        )

    def _active_memories(self, *, limit: int) -> list[JsonDict]:
        return self._list_memories(status="active")[: max(limit, 0)]

    @staticmethod
    def _snapshot_dict(snapshot: HeatSnapshot) -> JsonDict:
        return {
            "memory_id": snapshot.memory_id,
            "heat_score": snapshot.heat_score,
            "tier_before": snapshot.tier_before,
            "tier_after": snapshot.tier_after,
            "usefulness_score": snapshot.usefulness_score,
            "evidence_strength": snapshot.evidence_strength,
            "recent_negative_feedback": snapshot.recent_negative_feedback,
            "stale_penalty": snapshot.stale_penalty,
            "last_validation_at": snapshot.last_validation_at,
            "signals": snapshot.signals,
            "reasons": snapshot.reasons,
        }

    @staticmethod
    def _state_from_row(row: sqlite3.Row) -> JsonDict:
        result = {
            "memory_id": row["memory_id"],
            "tier": row["tier"],
            "heat_score": row["heat_score"],
            "usefulness_score": row["usefulness_score"],
            "evidence_strength": row["evidence_strength"],
            "recent_negative_feedback": row["recent_negative_feedback"],
            "stale_penalty": row["stale_penalty"],
            "last_validation_at": row["last_validation_at"],
            "last_scored_at": row["last_scored_at"],
            "archived_at": row["archived_at"],
            "policy_version": row["policy_version"],
            "metadata": json_loads(row["metadata_json"], {}),
        }
        if "summary" in row.keys():
            result["summary"] = row["summary"]
            result["type"] = row["type"]
        return result

    @staticmethod
    def _heat_run_from_row(row: sqlite3.Row | None) -> JsonDict:
        if row is None:
            return {}
        return {
            "id": row["id"],
            "mode": row["mode"],
            "status": row["status"],
            "policy_version": row["policy_version"],
            "total_count": row["total_count"],
            "scored_count": row["scored_count"],
            "migrated_count": row["migrated_count"],
            "proposed_count": row["proposed_count"],
            "archived_count": row["archived_count"],
            "forgotten_count": row["forgotten_count"],
            "config": json_loads(row["config_json"], {}),
            "summary": json_loads(row["summary_json"], {}),
            "error": row["error"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        }

    @staticmethod
    def _snapshot_row(row: sqlite3.Row) -> JsonDict:
        return {
            "run_id": row["run_id"],
            "memory_id": row["memory_id"],
            "heat_score": row["heat_score"],
            "tier_before": row["tier_before"],
            "tier_after": row["tier_after"],
            "usefulness_score": row["usefulness_score"],
            "evidence_strength": row["evidence_strength"],
            "recent_negative_feedback": row["recent_negative_feedback"],
            "stale_penalty": row["stale_penalty"],
            "signals": json_loads(row["signals_json"], {}),
            "reasons": json_loads(row["reasons_json"], []),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _consolidation_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "status": row["status"],
            "planner_version": row["planner_version"],
            "cluster_key": row["cluster_key"],
            "action": row["action"],
            "target_memory_id": row["target_memory_id"],
            "candidate_id": row["candidate_id"],
            "score": row["score"],
            "reasons": json_loads(row["reasons_json"], []),
            "evidence_ids": json_loads(row["evidence_ids_json"], []),
            "memory_ids": json_loads(row["memory_ids_json"], []),
            "payload": json_loads(row["payload_json"], {}),
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
        }

    @staticmethod
    def _tombstone_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "id": row["id"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "content_hash": row["content_hash"],
            "entity_hash": row["entity_hash"],
            "reason": row["reason"],
            "actor": row["actor"],
            "retention_stage": row["retention_stage"],
            "expires_at": row["expires_at"],
            "metadata": json_loads(row["metadata_json"], {}),
            "created_at": row["created_at"],
        }


def _feedback_default_value(feedback_type: str) -> float:
    normalized = feedback_type.lower().strip()
    if normalized in POSITIVE_FEEDBACK_TYPES:
        return 1.0
    if normalized in NEGATIVE_FEEDBACK_TYPES:
        return -1.0
    return 0.0


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.lower().strip() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _parse_time(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or utc_now())
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _type_half_life_days(memory_type: str) -> float:
    if memory_type in TYPE_HALF_LIFE_DAYS:
        return TYPE_HALF_LIFE_DAYS[memory_type]
    if memory_type in STABLE_TYPES:
        return 365.0
    if memory_type in EPISODIC_TYPES:
        return 60.0
    if memory_type in TEMPORAL_TYPES:
        return 14.0
    return 60.0


def _stale_half_life_days(memory_type: str) -> float:
    if memory_type in STABLE_TYPES:
        return 180.0
    if memory_type in EPISODIC_TYPES:
        return 90.0
    if memory_type in TEMPORAL_TYPES:
        return 21.0
    return max(_type_half_life_days(memory_type) * 1.5, 30.0)


def _type_stability(memory_type: str) -> float:
    if memory_type in STABLE_TYPES:
        return 0.85
    if memory_type in EPISODIC_TYPES:
        return 0.70
    if memory_type in TEMPORAL_TYPES:
        return 0.35
    if memory_type == "relationship_signal":
        return 0.55
    return 0.60


def _evidence_directness(evidence: JsonDict) -> float:
    source = str(evidence.get("source_type") or "")
    metadata = evidence.get("metadata") or {}
    text = " ".join(
        str(part or "")
        for part in [evidence.get("raw_content"), evidence.get("content_summary"), metadata.get("extraction_reason")]
    ).lower()
    if "explicit" in text or "请记住" in text or source in {"manual", "seed"}:
        return 1.0
    if source in {"conversation", "world_event"}:
        return 0.75
    if "inferred" in text or "implicit" in text:
        return 0.45
    return 0.60


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).strip().lower())


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entity_signature(text: str) -> str:
    tokens = search_tokens(text)
    if not tokens:
        return _normalize_text(text)
    return " ".join(sorted(tokens[:24]))


def _token_jaccard(left: str, right: str) -> float:
    left_tokens = set(search_tokens(left))
    right_tokens = set(search_tokens(right))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _contradiction_score(left: str, right: str) -> float:
    left_norm = _normalize_text(left)
    right_norm = _normalize_text(right)
    left_negative = any(term in left_norm for term in NEGATIVE_TERMS)
    right_negative = any(term in right_norm for term in NEGATIVE_TERMS)
    if left_negative == right_negative:
        return 0.0
    left_positive = any(term in left_norm for term in POSITIVE_TERMS)
    right_positive = any(term in right_norm for term in POSITIVE_TERMS)
    if not (left_positive or right_positive):
        return 0.0
    overlap = _token_jaccard(left, right)
    if overlap >= 0.22:
        return min(0.95, 0.70 + overlap)
    return 0.0
