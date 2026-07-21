from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..memory_schema import json_dumps, json_loads, new_id, utc_now
from .contracts import (
    DEFAULT_POLICY_PROFILE,
    DOMAIN_EVENT_TYPES,
    RELATIONSHIP_AGREEMENT_CONTRACT,
    RELATIONSHIP_AMENDMENT_CONTRACT,
    RELATIONSHIP_DOMAIN_EVENT_CONTRACT,
    RELATIONSHIP_ESTIMATE_SNAPSHOT_CONTRACT,
    RELATIONSHIP_FRAME_CONTRACT,
    RELATIONSHIP_POLICY_VERSION,
    RELATIONSHIP_REPAIR_CONTRACT,
    RELATIONSHIP_SCHEMA_VERSION,
    RELATIONSHIP_STATE_SNAPSHOT_CONTRACT,
    RELATIONSHIP_STATUS_EVENT_CONTRACT,
    REPAIR_STATES,
    REPAIR_TRANSITIONS,
    JsonDict,
    RelationshipEvidenceCandidate,
    RelationshipInputEvent,
    hash_payload,
)
from .dynamics import RelationshipDynamicsEngine, snapshot_checksum
from .schema import RELATIONSHIP_SCHEMA_SQL


class RelationshipStore:
    """Canonical pair-isolated relationship store with append-only contribution status."""

    def __init__(
        self,
        path: str | Path,
        *,
        sora_id: str = "default-agent",
        profile: Mapping[str, Any] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sora_id = sora_id
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(RELATIONSHIP_SCHEMA_SQL)
        self._conn.commit()
        self._seed_profile(dict(profile or DEFAULT_POLICY_PROFILE))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def reset(self) -> None:
        tables = [
            "relationship_dead_letter_resolution",
            "relationship_rebuild_run",
            "relationship_metric",
            "relationship_audit",
            "relationship_control_receipt",
            "relationship_tombstone",
            "relationship_outbox",
            "relationship_frame",
            "relationship_repair_episode",
            "relationship_agreement_amendment_candidate",
            "relationship_agreement",
            "relationship_estimate_snapshot",
            "relationship_state_snapshot",
            "relationship_contribution_status_event",
            "relationship_contribution",
            "relationship_candidate_evidence_ref",
            "relationship_candidate",
            "relationship_inbox",
            "relationship_pair",
            "relationship_policy_profile",
        ]
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for table in tables:
                    self._conn.execute(f"DELETE FROM {table}")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        self._seed_profile(dict(DEFAULT_POLICY_PROFILE))

    def profile(self, version: str = RELATIONSHIP_POLICY_VERSION) -> JsonDict:
        row = self._read_one("SELECT profile_json FROM relationship_policy_profile WHERE version=?", (version,))
        if row is None:
            raise RuntimeError(f"Relationship policy profile not found: {version}")
        return dict(json_loads(row["profile_json"], {}))

    def ensure_pair(self, user_id: str, *, sora_id: str | None = None) -> JsonDict:
        normalized_user = str(user_id or "default-user").strip()
        normalized_sora = str(sora_id or self.sora_id).strip()
        if not normalized_user or not normalized_sora:
            raise ValueError("sora_id and user_id are required for Relationship pair isolation")
        existing = self.pair(user_id=normalized_user, sora_id=normalized_sora)
        if existing:
            return existing
        pair_id = "rel_" + hashlib.sha256(f"{normalized_sora}\0{normalized_user}".encode("utf-8")).hexdigest()[:24]
        timestamp = utc_now()
        engine = RelationshipDynamicsEngine(self.profile())
        agreement_id = new_id("rag")
        state_id = new_id("rels")
        estimate_id = new_id("rele")
        frame_id = new_id("relf")
        agreement = {
            "schema_version": RELATIONSHIP_AGREEMENT_CONTRACT,
            "agreement_id": agreement_id,
            "pair_id": pair_id,
            "revision": 1,
            "mode": "companion",
            "status": "active",
            "terms": {
                "romantic_expression": "deny",
                "exclusivity": "not_agreed",
                "jealousy_style": "honest_noncoercive",
                "possessive_expression": "deny",
                "public_attribution": "private",
            },
            "consent": {
                "source": "restrictive_product_default",
                "accepted_by_user": False,
                "accepted_by_sora": True,
                "allows_only_non_intimate_defaults": True,
            },
            "source_refs": ["relationship_profile:default"],
        }
        state = engine.aggregate_state(
            [],
            agreement=agreement,
            repair_state="stable",
            previous_snapshot=None,
            relationship_started_at=timestamp,
            generated_at=timestamp,
        )
        state.update({"snapshot_id": state_id, "pair_id": pair_id, "revision": 0, "generation": 0, "status": "active", "agreement_revision": 1, "source_watermark": 0})
        estimate = engine.aggregate_estimate([], generated_at=timestamp)
        estimate.update({"snapshot_id": estimate_id, "pair_id": pair_id, "revision": 0, "generation": 0, "status": "active", "source_watermark": 0})
        frame = engine.build_frame(
            pair_id=pair_id,
            state_snapshot=state,
            estimate_snapshot=estimate,
            agreement=agreement,
            repair=None,
            surface_permission="implicit",
            generated_at=timestamp,
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO relationship_pair(
                        pair_id,sora_id,user_id,status,profile_version,tombstone_salt,
                        active_state_snapshot_id,active_estimate_snapshot_id,active_frame_id,
                        learning_paused,surface_permission,generation,created_at,updated_at
                    ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, 0, 'implicit', 0, ?, ?)
                    """,
                    (pair_id, normalized_sora, normalized_user, RELATIONSHIP_POLICY_VERSION, secrets.token_hex(16), state_id, estimate_id, frame_id, timestamp, timestamp),
                )
                if self._conn.execute("SELECT 1 FROM relationship_state_snapshot WHERE snapshot_id=?", (state_id,)).fetchone() is None:
                    self._insert_agreement(self._conn, agreement, timestamp=timestamp)
                    self._insert_state_snapshot(self._conn, state)
                    self._insert_estimate_snapshot(self._conn, estimate)
                    self._insert_frame(self._conn, frame_id, pair_id, state, estimate, frame)
                    self._insert_audit(
                        self._conn,
                        pair_id=pair_id,
                        action="relationship_pair_created",
                        entity_type="pair",
                        entity_id=pair_id,
                        actor="system",
                        details={"sora_id": normalized_sora, "user_id_hash": hashlib.sha256(normalized_user.encode("utf-8")).hexdigest()},
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self.pair(pair_id=pair_id) or {}

    def pair(
        self,
        *,
        pair_id: str | None = None,
        user_id: str | None = None,
        sora_id: str | None = None,
    ) -> JsonDict | None:
        if pair_id:
            row = self._read_one("SELECT * FROM relationship_pair WHERE pair_id=?", (pair_id,))
        elif user_id:
            row = self._read_one("SELECT * FROM relationship_pair WHERE sora_id=? AND user_id=?", (sora_id or self.sora_id, user_id))
        else:
            raise ValueError("pair_id or user_id is required")
        return self._pair_row(row) if row else None

    def list_pairs(self, *, limit: int = 100) -> list[JsonDict]:
        return [self._pair_row(row) for row in self._read_all("SELECT * FROM relationship_pair ORDER BY updated_at DESC LIMIT ?", (limit,))]

    def ingest(self, event: RelationshipInputEvent) -> JsonDict:
        event.validate()
        pair = self.pair(pair_id=event.pair_id)
        if pair is None:
            raise ValueError(f"Relationship pair not found: {event.pair_id}")
        stored_payload = self._redacted_event_payload(event.payload)
        payload_hash = hash_payload(stored_payload)
        source_refs = self._source_refs(event, stored_payload)
        if any(self.is_tombstoned(event.pair_id, source_ref) for source_ref in source_refs):
            self.record_audit(
                pair_id=event.pair_id,
                action="relationship_event_rejected_tombstone",
                entity_type="input_event",
                entity_id=event.event_id,
                actor="relationship_adapter",
                details={"source_system": event.source_system},
                correlation_id=event.correlation_id,
            )
            return {"status": "rejected_policy", "reason": "source_tombstoned", "event_id": event.event_id}
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM relationship_inbox WHERE source_system=? AND event_id=? AND source_revision=? AND action=?",
                (event.source_system, event.event_id, event.source_revision, event.action),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) == payload_hash:
                    return {"status": "skipped_duplicate", "inbox_id": existing["inbox_id"], "event_id": event.event_id}
                self._conn.execute(
                    "UPDATE relationship_inbox SET status='quarantined', quarantine_reason='idempotency_payload_conflict', last_error=? WHERE inbox_id=?",
                    (f"conflicting_payload_hash:{payload_hash}", existing["inbox_id"]),
                )
                self._insert_audit(
                    self._conn,
                    pair_id=event.pair_id,
                    action="relationship_event_quarantined",
                    entity_type="inbox",
                    entity_id=str(existing["inbox_id"]),
                    actor="relationship_adapter",
                    details={"reason": "idempotency_payload_conflict"},
                    correlation_id=event.correlation_id,
                )
                self._conn.commit()
                return {"status": "rejected_policy", "reason": "idempotency_payload_conflict", "inbox_id": existing["inbox_id"], "event_id": event.event_id}
            inbox_id = new_id("relin")
            self._conn.execute(
                """
                INSERT INTO relationship_inbox(
                    inbox_id,pair_id,source_system,event_id,source_revision,action,schema_version,
                    payload_hash,payload_json,status,attempts,max_attempts,correlation_id,turn_id,
                    origin_chain_json,evidence_refs_json,sensitivity,occurred_at,received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 5, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inbox_id,
                    event.pair_id,
                    event.source_system,
                    event.event_id,
                    event.source_revision,
                    event.action,
                    event.schema_version,
                    payload_hash,
                    json_dumps(stored_payload),
                    event.correlation_id,
                    event.turn_id,
                    json_dumps(list(event.origin_chain)),
                    json_dumps(list(event.evidence_refs)),
                    event.sensitivity,
                    event.occurred_at or utc_now(),
                    utc_now(),
                ),
            )
            self._insert_audit(
                self._conn,
                pair_id=event.pair_id,
                action="relationship_event_ingested",
                entity_type="inbox",
                entity_id=inbox_id,
                actor="relationship_adapter",
                details={"source_system": event.source_system, "schema_version": event.schema_version, "action": event.action},
                correlation_id=event.correlation_id,
            )
            self._conn.commit()
        return {"status": "applied", "inbox_id": inbox_id, "event_id": event.event_id}

    def pending_inbox(self, *, limit: int = 20, force: bool = False) -> list[JsonDict]:
        now = utc_now()
        sql = """
            SELECT * FROM relationship_inbox
            WHERE status IN ('pending','retry')
              AND (? OR next_attempt_at IS NULL OR next_attempt_at<=?)
            ORDER BY received_at, inbox_id LIMIT ?
        """
        return [self._inbox_row(row) for row in self._read_all(sql, (int(force), now, limit))]

    def claim_inbox(
        self,
        *,
        worker_id: str,
        limit: int = 20,
        lease_seconds: int = 90,
        force: bool = False,
    ) -> list[JsonDict]:
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires_at = (now + timedelta(seconds=max(10, lease_seconds))).isoformat()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    UPDATE relationship_inbox
                    SET status='retry',lease_owner=NULL,lease_expires_at=NULL,
                        last_error=COALESCE(last_error,'expired_worker_lease')
                    WHERE status='processing' AND lease_expires_at IS NOT NULL AND lease_expires_at<=?
                    """,
                    (now_text,),
                )
                rows = self._conn.execute(
                    """
                    SELECT inbox_id FROM relationship_inbox
                    WHERE status IN ('pending','retry')
                      AND (? OR next_attempt_at IS NULL OR next_attempt_at<=?)
                    ORDER BY received_at,inbox_id LIMIT ?
                    """,
                    (int(force), now_text, max(1, limit)),
                ).fetchall()
                ids = [str(row["inbox_id"]) for row in rows]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    self._conn.execute(
                        f"UPDATE relationship_inbox SET status='processing',lease_owner=?,lease_expires_at=? WHERE inbox_id IN ({placeholders})",
                        (worker_id, expires_at, *ids),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return [
            self._inbox_row(row)
            for row in self._read_all(
                f"SELECT * FROM relationship_inbox WHERE inbox_id IN ({placeholders}) ORDER BY received_at,inbox_id",
                tuple(ids),
            )
        ]

    def mark_inbox(
        self,
        inbox_id: str,
        *,
        status: str,
        error: str | None = None,
        retry_seconds: int | None = None,
        quarantine_reason: str | None = None,
    ) -> None:
        next_attempt = (
            (datetime.now(timezone.utc) + timedelta(seconds=retry_seconds)).isoformat()
            if retry_seconds is not None
            else None
        )
        with self._lock:
            self._conn.execute(
                """
                UPDATE relationship_inbox
                SET status=?, attempts=attempts+1, last_error=?, next_attempt_at=?, quarantine_reason=?,
                    lease_owner=NULL,lease_expires_at=NULL,
                    processed_at=CASE WHEN ? IN ('processed','rejected','quarantined','dead_letter') THEN ? ELSE processed_at END
                WHERE inbox_id=?
                """,
                (status, error, next_attempt, quarantine_reason, status, utc_now(), inbox_id),
            )
            self._conn.commit()

    def save_candidate(
        self,
        candidate: RelationshipEvidenceCandidate,
        *,
        inbox_id: str | None,
        status: str,
        gate_reason: str,
        provider_trace: Mapping[str, Any],
        deferred_until: str | None = None,
    ) -> JsonDict:
        candidate.validate()
        candidate_hash = candidate.candidate_hash()
        timestamp = utc_now()
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM relationship_candidate WHERE pair_id=? AND candidate_hash=?",
                (candidate.pair_id, candidate_hash),
            ).fetchone()
            if existing is not None:
                for ref in candidate.evidence_refs:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO relationship_candidate_evidence_ref(candidate_id,ref_hash,source_ref,created_at) VALUES (?, ?, ?, ?)",
                        (existing["candidate_id"], hashlib.sha256(ref.encode("utf-8")).hexdigest(), ref, timestamp),
                    )
                self._conn.commit()
                return {**self._candidate_row(existing), "duplicate": True}
            self._conn.execute(
                """
                INSERT INTO relationship_candidate(
                    candidate_id,pair_id,inbox_id,candidate_hash,schema_version,direction_scope,
                    dimension,direction,evidence_type,source_kind,source_ref,subject_key,context_key,
                    episode_ref,payload_json,status,gate_reason,provider_trace_json,deferred_until,
                    created_at,updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.candidate_id,
                    candidate.pair_id,
                    inbox_id,
                    candidate_hash,
                    candidate.schema_version,
                    candidate.direction_scope,
                    candidate.dimension,
                    candidate.direction,
                    candidate.evidence_type,
                    candidate.source_kind,
                    candidate.source_ref,
                    candidate.subject_key,
                    candidate.context_key,
                    candidate.episode_ref,
                    json_dumps(candidate.to_dict()),
                    status,
                    gate_reason,
                    json_dumps(dict(provider_trace)),
                    deferred_until,
                    timestamp,
                    timestamp,
                ),
            )
            for ref in candidate.evidence_refs:
                self._conn.execute(
                    "INSERT INTO relationship_candidate_evidence_ref(candidate_id,ref_hash,source_ref,created_at) VALUES (?, ?, ?, ?)",
                    (candidate.candidate_id, hashlib.sha256(ref.encode("utf-8")).hexdigest(), ref, timestamp),
                )
            self._insert_audit(
                self._conn,
                pair_id=candidate.pair_id,
                action="relationship_candidate_saved",
                entity_type="candidate",
                entity_id=candidate.candidate_id,
                actor="relationship_gate",
                details={"status": status, "reason": gate_reason, "dimension": candidate.dimension, "direction_scope": candidate.direction_scope},
            )
            self._conn.commit()
        return self.candidate(candidate.candidate_id) or {}

    def candidate(self, candidate_id: str) -> JsonDict | None:
        row = self._read_one("SELECT * FROM relationship_candidate WHERE candidate_id=?", (candidate_id,))
        return self._candidate_row(row) if row else None

    def list_candidates(self, *, pair_id: str | None = None, status: str | None = None, limit: int = 100) -> list[JsonDict]:
        clauses: list[str] = []
        params: list[Any] = []
        if pair_id:
            clauses.append("pair_id=?")
            params.append(pair_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        return [self._candidate_row(row) for row in self._read_all(f"SELECT * FROM relationship_candidate{where} ORDER BY created_at DESC LIMIT ?", tuple(params))]

    def set_candidate_status(self, candidate_id: str, status: str, *, reason: str, actor: str) -> JsonDict:
        timestamp = utc_now()
        with self._lock:
            row = self._conn.execute("SELECT pair_id FROM relationship_candidate WHERE candidate_id=?", (candidate_id,)).fetchone()
            if row is None:
                raise ValueError(f"Relationship candidate not found: {candidate_id}")
            self._conn.execute(
                "UPDATE relationship_candidate SET status=?,gate_reason=?,updated_at=?,decided_at=? WHERE candidate_id=?",
                (status, reason, timestamp, timestamp, candidate_id),
            )
            self._insert_audit(
                self._conn,
                pair_id=row["pair_id"],
                action="relationship_candidate_status_changed",
                entity_type="candidate",
                entity_id=candidate_id,
                actor=actor,
                details={"status": status, "reason": reason},
            )
            self._conn.commit()
        return self.candidate(candidate_id) or {}

    def active_contributions(self, pair_id: str) -> list[JsonDict]:
        rows = self._read_all(
            """
            WITH latest AS (
                SELECT contribution_id,status,
                       ROW_NUMBER() OVER (PARTITION BY contribution_id ORDER BY occurred_at DESC,status_event_id DESC) AS rn
                FROM relationship_contribution_status_event
            )
            SELECT c.* FROM relationship_contribution c
            JOIN latest s ON s.contribution_id=c.contribution_id AND s.rn=1
            WHERE c.pair_id=? AND s.status='active'
            ORDER BY c.occurred_at,c.contribution_id
            """,
            (pair_id,),
        )
        return [self._contribution_row(row) for row in rows]

    def list_contributions(self, *, pair_id: str | None = None, active: bool | None = None, limit: int = 100) -> list[JsonDict]:
        clauses = ["1=1"]
        params: list[Any] = []
        if pair_id:
            clauses.append("c.pair_id=?")
            params.append(pair_id)
        if active is not None:
            clauses.append("s.status=?")
            params.append("active" if active else "deactivated")
        params.append(limit)
        rows = self._read_all(
            f"""
            WITH latest AS (
                SELECT contribution_id,status,reason,occurred_at,
                       ROW_NUMBER() OVER (PARTITION BY contribution_id ORDER BY occurred_at DESC,status_event_id DESC) AS rn
                FROM relationship_contribution_status_event
            )
            SELECT c.*,s.status AS current_status,s.reason AS status_reason FROM relationship_contribution c
            JOIN latest s ON s.contribution_id=c.contribution_id AND s.rn=1
            WHERE {' AND '.join(clauses)} ORDER BY c.occurred_at DESC LIMIT ?
            """,
            tuple(params),
        )
        return [self._contribution_row(row) | {"status": row["current_status"], "status_reason": row["status_reason"]} for row in rows]

    def commit_candidate(
        self,
        candidate_id: str,
        *,
        computation: Mapping[str, Any],
        engine: RelationshipDynamicsEngine,
        actor: str,
        occurred_at: str,
        correlation_id: str | None = None,
        rebuild: bool = True,
    ) -> JsonDict:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute("SELECT * FROM relationship_candidate WHERE candidate_id=?", (candidate_id,)).fetchone()
                if row is None:
                    raise ValueError(f"Relationship candidate not found: {candidate_id}")
                if row["status"] == "committed":
                    existing = self._conn.execute("SELECT contribution_id FROM relationship_contribution WHERE candidate_id=?", (candidate_id,)).fetchone()
                    self._conn.rollback()
                    return {"status": "skipped_duplicate", "candidate_id": candidate_id, "contribution_id": existing["contribution_id"] if existing else None}
                candidate = RelationshipEvidenceCandidate.from_dict(json_loads(row["payload_json"], {}))
                pair = self._conn.execute("SELECT * FROM relationship_pair WHERE pair_id=?", (candidate.pair_id,)).fetchone()
                if pair is None:
                    raise RuntimeError("Relationship pair disappeared during commit")
                contribution_id = new_id("relcon")
                provenance = {
                    "candidate_id": candidate_id,
                    "reason_codes": list(candidate.reason_codes),
                    "evidence_summary": str(candidate.metadata.get("evidence_summary") or "")[:500],
                    "entities": list(candidate.metadata.get("entities") or [])[:16],
                    "evidence_embedding": list(candidate.metadata.get("evidence_embedding") or [])[:4096],
                    "novelty_trace": dict(computation.get("novelty_trace") or {}),
                    "conflict_trace": dict(computation.get("conflict_trace") or {}),
                    "remaining_budgets": dict(computation.get("remaining_budgets") or {}),
                }
                self._conn.execute(
                    """
                    INSERT INTO relationship_contribution(
                        contribution_id,pair_id,candidate_id,direction_scope,dimension,direction,
                        evidence_type,source_kind,source_ref,subject_key,context_key,episode_ref,
                        evidence_refs_json,confidence,evidence_strength,source_reliability,
                        direction_certainty,consistency,novelty_factor,conflict_score,support,
                        raw_delta,bounded_delta,sensitivity,algorithm_version,policy_version,
                        provenance_json,occurred_at,created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        contribution_id,
                        candidate.pair_id,
                        candidate_id,
                        candidate.direction_scope,
                        candidate.dimension,
                        candidate.direction,
                        candidate.evidence_type,
                        candidate.source_kind,
                        candidate.source_ref,
                        candidate.subject_key,
                        candidate.context_key,
                        candidate.episode_ref,
                        json_dumps(list(candidate.evidence_refs)),
                        candidate.confidence,
                        candidate.evidence_strength,
                        candidate.source_reliability,
                        candidate.direction_certainty,
                        candidate.consistency,
                        float(computation.get("novelty_factor", 1.0)),
                        float(computation.get("conflict_score", 0.0)),
                        float(computation.get("support", 0.0)),
                        float(computation.get("raw_delta", 0.0)),
                        float(computation.get("bounded_delta", 0.0)),
                        candidate.sensitivity,
                        str(computation.get("algorithm_version") or engine.profile.get("algorithm_version")),
                        str(engine.profile.get("version")),
                        json_dumps(provenance),
                        occurred_at,
                        utc_now(),
                    ),
                )
                self._insert_status_event(
                    self._conn,
                    contribution_id=contribution_id,
                    status="active",
                    reason="candidate_committed",
                    actor=actor,
                    source_ref=candidate.source_ref,
                    correlation_id=correlation_id,
                )
                for superseded in candidate.supersedes:
                    if self._conn.execute("SELECT 1 FROM relationship_contribution WHERE contribution_id=? AND pair_id=?", (superseded, candidate.pair_id)).fetchone():
                        self._insert_status_event(
                            self._conn,
                            contribution_id=superseded,
                            status="deactivated",
                            reason="superseded_by_explicit_correction",
                            actor=actor,
                            source_ref=candidate.source_ref,
                            correlation_id=correlation_id,
                        )
                self._conn.execute(
                    "UPDATE relationship_candidate SET status='committed',gate_reason='committed',updated_at=?,decided_at=? WHERE candidate_id=?",
                    (utc_now(), utc_now(), candidate_id),
                )
                rebuild_result = (
                    self._rebuild_pair_tx(
                        self._conn,
                        pair_id=candidate.pair_id,
                        engine=engine,
                        reason="candidate_commit",
                        actor=actor,
                        correlation_id=correlation_id,
                    )
                    if rebuild
                    else {"pair_id": candidate.pair_id, "rebuild_deferred": True}
                )
                self._insert_audit(
                    self._conn,
                    pair_id=candidate.pair_id,
                    action="relationship_contribution_committed",
                    entity_type="contribution",
                    entity_id=contribution_id,
                    actor=actor,
                    details={"candidate_id": candidate_id, "dimension": candidate.dimension, "direction_scope": candidate.direction_scope, "bounded_delta": computation.get("bounded_delta")},
                    correlation_id=correlation_id,
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {"status": "committed", "candidate_id": candidate_id, "contribution_id": contribution_id, **rebuild_result}

    def active_state_snapshot(self, pair_id: str) -> JsonDict:
        row = self._read_one(
            "SELECT s.* FROM relationship_state_snapshot s JOIN relationship_pair p ON p.active_state_snapshot_id=s.snapshot_id WHERE p.pair_id=?",
            (pair_id,),
        )
        if row is None:
            raise RuntimeError(f"active Relationship state snapshot missing: {pair_id}")
        return self._state_snapshot_row(row)

    def active_estimate_snapshot(self, pair_id: str) -> JsonDict:
        row = self._read_one(
            "SELECT s.* FROM relationship_estimate_snapshot s JOIN relationship_pair p ON p.active_estimate_snapshot_id=s.snapshot_id WHERE p.pair_id=?",
            (pair_id,),
        )
        if row is None:
            raise RuntimeError(f"active Relationship estimate snapshot missing: {pair_id}")
        return self._estimate_snapshot_row(row)

    def active_frame(self, pair_id: str) -> JsonDict:
        row = self._read_one(
            "SELECT f.* FROM relationship_frame f JOIN relationship_pair p ON p.active_frame_id=f.frame_id WHERE p.pair_id=?",
            (pair_id,),
        )
        if row is None:
            raise RuntimeError(f"active Relationship frame missing: {pair_id}")
        return self._frame_row(row)

    def active_agreement(self, pair_id: str) -> JsonDict:
        row = self._read_one(
            "SELECT * FROM relationship_agreement WHERE pair_id=? AND status='active' ORDER BY revision DESC LIMIT 1",
            (pair_id,),
        )
        if row is None:
            return {"schema_version": RELATIONSHIP_AGREEMENT_CONTRACT, "pair_id": pair_id, "revision": 0, "mode": "organic", "status": "none", "terms": {}, "consent": {}, "source_refs": []}
        return self._agreement_row(row)

    def open_repair(self, pair_id: str) -> JsonDict | None:
        row = self._read_one(
            """
            SELECT * FROM relationship_repair_episode
            WHERE pair_id=? AND status NOT IN ('stable','repaired','accepted_difference')
            ORDER BY updated_at DESC LIMIT 1
            """,
            (pair_id,),
        )
        return self._repair_row(row) if row else None

    def rebuild(self, pair_id: str, *, engine: RelationshipDynamicsEngine, reason: str, actor: str) -> JsonDict:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = self._rebuild_pair_tx(self._conn, pair_id=pair_id, engine=engine, reason=reason, actor=actor)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return result

    def verify_rebuild_parity(self, pair_id: str, *, engine: RelationshipDynamicsEngine, reason: str, actor: str) -> JsonDict:
        active_state = self.active_state_snapshot(pair_id)
        active_estimate = self.active_estimate_snapshot(pair_id)
        contributions = self.active_contributions(pair_id)
        agreement = self.active_agreement(pair_id)
        repair = self.open_repair(pair_id)
        generated_at = str(active_state.get("generated_at") or utc_now())
        shadow_state = engine.aggregate_state(
            contributions,
            agreement=agreement,
            repair_state=str((repair or {}).get("status") or "stable"),
            previous_snapshot=active_state,
            relationship_started_at=str(active_state.get("relationship_started_at") or generated_at),
            generated_at=generated_at,
        )
        shadow_estimate = engine.aggregate_estimate(contributions, generated_at=generated_at)
        state_parity = snapshot_checksum(active_state) == snapshot_checksum(shadow_state)
        estimate_parity = snapshot_checksum(active_estimate) == snapshot_checksum(shadow_estimate)
        run_id = new_id("relrb")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO relationship_rebuild_run(
                    run_id,pair_id,reason,actor,status,old_state_checksum,shadow_state_checksum,
                    old_estimate_checksum,shadow_estimate_checksum,parity,details_json,created_at,completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    pair_id,
                    reason,
                    actor,
                    "passed" if state_parity and estimate_parity else "failed",
                    snapshot_checksum(active_state),
                    snapshot_checksum(shadow_state),
                    snapshot_checksum(active_estimate),
                    snapshot_checksum(shadow_estimate),
                    int(state_parity and estimate_parity),
                    json_dumps({"state_parity": state_parity, "estimate_parity": estimate_parity}),
                    generated_at,
                    utc_now(),
                ),
            )
            self._conn.commit()
        return {"run_id": run_id, "pair_id": pair_id, "state_parity": state_parity, "estimate_parity": estimate_parity, "parity": state_parity and estimate_parity}

    def rollback_contribution(
        self,
        contribution_id: str,
        *,
        engine: RelationshipDynamicsEngine,
        reason: str,
        actor: str,
        correlation_id: str | None = None,
    ) -> JsonDict:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute("SELECT pair_id FROM relationship_contribution WHERE contribution_id=?", (contribution_id,)).fetchone()
                if row is None:
                    raise ValueError(f"Relationship contribution not found: {contribution_id}")
                self._insert_status_event(
                    self._conn,
                    contribution_id=contribution_id,
                    status="deactivated",
                    reason=reason,
                    actor=actor,
                    source_ref=f"rollback:{contribution_id}",
                    correlation_id=correlation_id,
                )
                result = self._rebuild_pair_tx(self._conn, pair_id=row["pair_id"], engine=engine, reason="contribution_rollback", actor=actor, correlation_id=correlation_id)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {"contribution_id": contribution_id, "status": "deactivated", **result}

    def rollback_source(
        self,
        pair_id: str,
        source_ref: str,
        *,
        engine: RelationshipDynamicsEngine,
        reason: str,
        actor: str,
    ) -> JsonDict:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT contribution_id FROM relationship_contribution WHERE pair_id=? AND source_ref=?",
                    (pair_id, source_ref),
                ).fetchall()
                for row in rows:
                    self._insert_status_event(
                        self._conn,
                        contribution_id=row["contribution_id"],
                        status="deactivated",
                        reason=reason,
                        actor=actor,
                        source_ref=f"rollback:{source_ref}",
                    )
                result = self._rebuild_pair_tx(
                    self._conn,
                    pair_id=pair_id,
                    engine=engine,
                    reason="source_rollback",
                    actor=actor,
                    bypass_debounce=True,
                )
                self._insert_audit(
                    self._conn,
                    pair_id=pair_id,
                    action="relationship_source_rolled_back",
                    entity_type="source",
                    entity_id=hashlib.sha256(source_ref.encode("utf-8")).hexdigest(),
                    actor=actor,
                    details={"deactivated_contribution_count": len(rows), "source_ref_redacted": True},
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {"pair_id": pair_id, "status": "rolled_back", "deactivated_contribution_count": len(rows), **result}

    def forget_source(
        self,
        pair_id: str,
        source_ref: str,
        *,
        engine: RelationshipDynamicsEngine,
        reason: str,
        actor: str,
    ) -> JsonDict:
        ref_hash = self._tombstone_hash(pair_id, source_ref)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO relationship_tombstone(pair_id,ref_hash,ref_kind,reason_code,created_at) VALUES (?, ?, 'source_ref', ?, ?)",
                    (pair_id, ref_hash, reason, utc_now()),
                )
                self._conn.execute(
                    "UPDATE relationship_candidate SET status='forgotten',gate_reason=?,updated_at=?,decided_at=? WHERE pair_id=? AND source_ref=?",
                    (reason, utc_now(), utc_now(), pair_id, source_ref),
                )
                rows = self._conn.execute("SELECT contribution_id FROM relationship_contribution WHERE pair_id=? AND source_ref=?", (pair_id, source_ref)).fetchall()
                for row in rows:
                    self._insert_status_event(
                        self._conn,
                        contribution_id=row["contribution_id"],
                        status="deactivated",
                        reason="user_forget",
                        actor=actor,
                        source_ref="forget_receipt",
                    )
                result = self._rebuild_pair_tx(self._conn, pair_id=pair_id, engine=engine, reason="forget_source", actor=actor, bypass_debounce=True)
                self._enqueue_domain_event_tx(
                    self._conn,
                    pair_id=pair_id,
                    event_type="forget_applied",
                    source_ref="forget_receipt",
                    payload={"deactivated_contribution_count": len(rows)},
                    origin_chain=["user_control", "relationship"],
                )
                self._insert_audit(
                    self._conn,
                    pair_id=pair_id,
                    action="relationship_source_forgotten",
                    entity_type="source",
                    entity_id=ref_hash,
                    actor=actor,
                    details={"deactivated_contribution_count": len(rows), "source_ref_redacted": True},
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {"pair_id": pair_id, "status": "forgotten", "deactivated_contribution_count": len(rows), "tombstone_hash": ref_hash, **result}

    def is_tombstoned(self, pair_id: str, source_ref: str) -> bool:
        if not source_ref:
            return False
        ref_hash = self._tombstone_hash(pair_id, source_ref)
        return self._read_one("SELECT 1 FROM relationship_tombstone WHERE pair_id=? AND ref_hash=?", (pair_id, ref_hash)) is not None

    def set_control(
        self,
        pair_id: str,
        *,
        control_type: str,
        value: Any,
        source_ref: str,
        actor: str = "user",
    ) -> JsonDict:
        if control_type not in {"learning_paused", "surface_permission"}:
            raise ValueError(f"unsupported Relationship control: {control_type}")
        if control_type == "surface_permission" and str(value) not in {"silent", "implicit", "explicit"}:
            raise ValueError("Relationship surface_permission must be silent, implicit, or explicit")
        column = "learning_paused" if control_type == "learning_paused" else "surface_permission"
        stored_value: Any = int(bool(value)) if column == "learning_paused" else str(value)
        receipt_id = new_id("relctl")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if self._conn.execute("SELECT 1 FROM relationship_pair WHERE pair_id=?", (pair_id,)).fetchone() is None:
                    raise ValueError(f"Relationship pair not found: {pair_id}")
                self._conn.execute(f"UPDATE relationship_pair SET {column}=?,updated_at=? WHERE pair_id=?", (stored_value, utc_now(), pair_id))
                self._conn.execute(
                    "INSERT INTO relationship_control_receipt VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (receipt_id, pair_id, control_type, json_dumps(value), source_ref, actor, utc_now()),
                )
                self._enqueue_domain_event_tx(
                    self._conn,
                    pair_id=pair_id,
                    event_type="relationship_control_changed",
                    source_ref=receipt_id,
                    payload={"control_type": control_type, "value": value},
                    origin_chain=["user_control", "relationship"],
                )
                self._insert_audit(
                    self._conn,
                    pair_id=pair_id,
                    action="relationship_control_changed",
                    entity_type="control_receipt",
                    entity_id=receipt_id,
                    actor=actor,
                    details={"control_type": control_type, "value": value},
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {"schema_version": "RelationshipControlReceipt.v1", "receipt_id": receipt_id, "pair_id": pair_id, "control_type": control_type, "value": value, "effective": "next_read"}

    def propose_agreement_amendment(
        self,
        pair_id: str,
        *,
        operation: str,
        term_path: str,
        proposed_value: Any,
        reason_codes: Sequence[str],
        evidence_refs: Sequence[str],
        source_repair_episode_id: str | None = None,
        ttl_seconds: int = 900,
    ) -> JsonDict:
        if operation not in {"add_or_replace_term", "revoke_agreement"}:
            raise ValueError("unsupported Agreement amendment operation")
        if operation == "add_or_replace_term" and not term_path.startswith("terms."):
            raise ValueError("Agreement amendment term_path must start with terms.")
        if operation == "revoke_agreement" and (term_path != "agreement.status" or proposed_value != "revoked"):
            raise ValueError("Agreement revocation must target agreement.status=revoked")
        candidate_id = new_id("raga")
        token = secrets.token_urlsafe(18)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
        with self._lock:
            duplicate = self._conn.execute(
                """
                SELECT candidate_id FROM relationship_agreement_amendment_candidate
                WHERE pair_id=? AND term_path=? AND proposed_value_json=? AND status='pending_confirmation'
                """,
                (pair_id, term_path, json_dumps(proposed_value)),
            ).fetchone()
            if duplicate:
                return {"schema_version": RELATIONSHIP_AMENDMENT_CONTRACT, "candidate_id": duplicate["candidate_id"], "pair_id": pair_id, "status": "pending_confirmation", "duplicate": True, "confirmation_token": None}
            self._conn.execute(
                """
                INSERT INTO relationship_agreement_amendment_candidate(
                    candidate_id,pair_id,source_repair_episode_id,operation,term_path,
                    proposed_value_json,reason_codes_json,evidence_refs_json,status,
                    confirmation_token_hash,expires_at,created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending_confirmation', ?, ?, ?)
                """,
                (candidate_id, pair_id, source_repair_episode_id, operation, term_path, json_dumps(proposed_value), json_dumps(list(reason_codes)), json_dumps(list(evidence_refs)), token_hash, expires_at, utc_now()),
            )
            self._insert_audit(
                self._conn,
                pair_id=pair_id,
                action="agreement_amendment_proposed",
                entity_type="agreement_amendment",
                entity_id=candidate_id,
                actor="relationship_repair" if source_repair_episode_id else "relationship_control",
                details={"term_path": term_path, "requires_user_confirmation": True},
            )
            self._enqueue_domain_event_tx(
                self._conn,
                pair_id=pair_id,
                event_type="agreement_confirmation_required",
                source_ref=f"agreement-amendment:{candidate_id}",
                payload={
                    "candidate_id": candidate_id,
                    "operation": operation,
                    "term_path": term_path,
                    "proposed_value": proposed_value,
                    "confirmation_token": token,
                    "expires_at": expires_at,
                },
                origin_chain=["relationship_extractor", "relationship_agreement"],
            )
            self._conn.commit()
        return {
            "schema_version": RELATIONSHIP_AMENDMENT_CONTRACT,
            "candidate_id": candidate_id,
            "pair_id": pair_id,
            "operation": operation,
            "term_path": term_path,
            "proposed_value": proposed_value,
            "reason_codes": list(reason_codes),
            "evidence_refs": list(evidence_refs),
            "requires_user_confirmation": True,
            "status": "pending_confirmation",
            "confirmation_token": token,
            "expires_at": expires_at,
        }

    def list_agreement_amendments(self, pair_id: str, *, status: str | None = None, limit: int = 100) -> list[JsonDict]:
        if status:
            rows = self._read_all(
                "SELECT * FROM relationship_agreement_amendment_candidate WHERE pair_id=? AND status=? ORDER BY created_at DESC LIMIT ?",
                (pair_id, status, limit),
            )
        else:
            rows = self._read_all(
                "SELECT * FROM relationship_agreement_amendment_candidate WHERE pair_id=? ORDER BY created_at DESC LIMIT ?",
                (pair_id, limit),
            )
        return [
            {
                "candidate_id": row["candidate_id"],
                "pair_id": row["pair_id"],
                "operation": row["operation"],
                "term_path": row["term_path"],
                "proposed_value": json_loads(row["proposed_value_json"], None),
                "reason_codes": json_loads(row["reason_codes_json"], []),
                "evidence_refs": json_loads(row["evidence_refs_json"], []),
                "status": row["status"],
                "expires_at": row["expires_at"],
                "created_at": row["created_at"],
                "decided_at": row["decided_at"],
            }
            for row in rows
        ]

    def expire_agreement_amendments(self, *, now: str | None = None) -> int:
        timestamp = now or utc_now()
        with self._lock:
            expired = self._conn.execute(
                "UPDATE relationship_agreement_amendment_candidate SET status='expired',decided_at=? WHERE status='pending_confirmation' AND expires_at IS NOT NULL AND expires_at<=?",
                (timestamp, timestamp),
            ).rowcount
            self._conn.commit()
        return int(expired)

    def confirm_agreement_amendment(
        self,
        pair_id: str,
        candidate_id: str,
        token: str,
        *,
        engine: RelationshipDynamicsEngine,
        actor: str = "user",
    ) -> JsonDict:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute("SELECT * FROM relationship_agreement_amendment_candidate WHERE candidate_id=? AND pair_id=?", (candidate_id, pair_id)).fetchone()
                if row is None:
                    raise ValueError("Agreement amendment candidate not found")
                if row["status"] != "pending_confirmation":
                    raise ValueError(f"Agreement amendment is not pending: {row['status']}")
                if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
                    self._conn.execute("UPDATE relationship_agreement_amendment_candidate SET status='expired',decided_at=? WHERE candidate_id=?", (utc_now(), candidate_id))
                    raise ValueError("Agreement amendment confirmation expired")
                if not secrets.compare_digest(str(row["confirmation_token_hash"]), hashlib.sha256(token.encode("utf-8")).hexdigest()):
                    raise ValueError("Agreement amendment confirmation token is invalid")
                current = self._active_agreement_tx(self._conn, pair_id)
                operation = str(row["operation"])
                if operation == "revoke_agreement":
                    self._conn.execute("UPDATE relationship_agreement SET status='revoked',updated_at=? WHERE pair_id=? AND status='active'", (utc_now(), pair_id))
                    agreement = {**current, "status": "revoked"}
                    result_status = "revoked"
                    changed_term_path = "agreement.status"
                else:
                    terms = dict(current.get("terms") or {})
                    path = str(row["term_path"]).removeprefix("terms.").split(".")
                    proposed_value = json_loads(row["proposed_value_json"], None)
                    mode = str(current.get("mode", "companion"))
                    if path == ["relationship_mode"]:
                        mode = str(proposed_value)
                        if mode not in {"organic", "companion", "friend", "romantic_partner", "family_style", "mentor_peer"}:
                            raise ValueError(f"unsupported Relationship agreement mode: {mode}")
                    else:
                        self._set_nested(terms, path, proposed_value)
                    revision = int(current.get("revision", 0)) + 1
                    agreement_id = new_id("rag")
                    self._conn.execute("UPDATE relationship_agreement SET status='amended',updated_at=? WHERE pair_id=? AND status='active'", (utc_now(), pair_id))
                    agreement = {
                        "schema_version": RELATIONSHIP_AGREEMENT_CONTRACT,
                        "agreement_id": agreement_id,
                        "pair_id": pair_id,
                        "revision": revision,
                        "mode": mode,
                        "status": "active",
                        "terms": terms,
                        "consent": {"accepted_by_user": True, "accepted_by_sora": True, "accepted_at": utc_now(), "source_candidate_id": candidate_id},
                        "supersedes": current.get("agreement_id"),
                        "source_refs": list(json_loads(row["evidence_refs_json"], [])),
                    }
                    self._insert_agreement(self._conn, agreement, timestamp=utc_now())
                    result_status = "active"
                    changed_term_path = row["term_path"]
                self._conn.execute("UPDATE relationship_agreement_amendment_candidate SET status='confirmed',decided_at=? WHERE candidate_id=?", (utc_now(), candidate_id))
                rebuild = self._rebuild_pair_tx(self._conn, pair_id=pair_id, engine=engine, reason="agreement_confirmed", actor=actor, bypass_debounce=True)
                self._enqueue_domain_event_tx(
                    self._conn,
                    pair_id=pair_id,
                    event_type="agreement_changed",
                    source_ref=f"agreement-amendment:{candidate_id}:confirmed",
                    payload={"revision": agreement.get("revision"), "status": result_status, "changed_term_path": changed_term_path},
                    origin_chain=["user_control", "relationship"],
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {"status": result_status, "agreement": agreement, **rebuild}

    def revoke_agreement(self, pair_id: str, *, engine: RelationshipDynamicsEngine, actor: str = "user", reason: str = "user_revoked") -> JsonDict:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._active_agreement_tx(self._conn, pair_id)
                self._conn.execute("UPDATE relationship_agreement SET status='revoked',updated_at=? WHERE pair_id=? AND status='active'", (utc_now(), pair_id))
                rebuild = self._rebuild_pair_tx(self._conn, pair_id=pair_id, engine=engine, reason="agreement_revoked", actor=actor, bypass_debounce=True)
                self._enqueue_domain_event_tx(
                    self._conn,
                    pair_id=pair_id,
                    event_type="agreement_changed",
                    source_ref=f"agreement:{current.get('agreement_id')}:revoke",
                    payload={"from_revision": current.get("revision"), "status": "revoked", "reason": reason},
                    origin_chain=["user_control", "relationship"],
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {"status": "revoked", "agreement_id": current.get("agreement_id"), **rebuild}

    def create_repair(
        self,
        pair_id: str,
        *,
        trigger_ref: str,
        evidence_refs: Sequence[str],
        affected_dimensions: Sequence[str],
        responsibility: Mapping[str, Any] | None = None,
        requested_repair: Mapping[str, Any] | None = None,
        repeated_violation_key: str | None = None,
    ) -> JsonDict:
        repair_id = new_id("repair")
        timestamp = utc_now()
        repeated_count = 0
        if repeated_violation_key:
            prior = self._read_one(
                "SELECT MAX(repeated_violation_count) AS count FROM relationship_repair_episode WHERE pair_id=? AND repeated_violation_key=?",
                (pair_id, repeated_violation_key),
            )
            repeated_count = int(prior["count"] or 0) + 1 if prior else 1
        history = [{"from": None, "to": "rupture_detected", "actor": "relationship_system", "occurred_at": timestamp}]
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO relationship_repair_episode(
                    repair_episode_id,pair_id,status,trigger_ref,evidence_refs_json,
                    affected_dimensions_json,responsibility_json,requested_repair_json,
                    repair_actions_json,user_outcome,repeated_violation_key,repeated_violation_count,
                    policy_severity,state_history_json,opened_at,updated_at
                ) VALUES (?, ?, 'rupture_detected', ?, ?, ?, ?, ?, '[]', NULL, ?, ?, ?, ?, ?, ?)
                """,
                (repair_id, pair_id, trigger_ref, json_dumps(list(evidence_refs)), json_dumps(list(affected_dimensions)), json_dumps(dict(responsibility or {})), json_dumps(dict(requested_repair or {})), repeated_violation_key, repeated_count, "elevated" if repeated_count >= 2 else "normal", json_dumps(history), timestamp, timestamp),
            )
            self._enqueue_domain_event_tx(
                self._conn,
                pair_id=pair_id,
                event_type="repair_state_changed",
                source_ref=f"repair:{repair_id}:rupture_detected",
                payload={"from": "stable", "to": "rupture_detected", "repeated_violation_count": repeated_count},
                origin_chain=["interaction", "relationship"],
            )
            self._conn.commit()
        return self.repair(repair_id) or {}

    def repair(self, repair_id: str) -> JsonDict | None:
        row = self._read_one("SELECT * FROM relationship_repair_episode WHERE repair_episode_id=?", (repair_id,))
        return self._repair_row(row) if row else None

    def transition_repair(
        self,
        repair_id: str,
        to_state: str,
        *,
        engine: RelationshipDynamicsEngine,
        actor: str,
        repair_action: Mapping[str, Any] | None = None,
        user_outcome: str | None = None,
    ) -> JsonDict:
        if to_state not in REPAIR_STATES:
            raise ValueError(f"unsupported Repair state: {to_state}")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute("SELECT * FROM relationship_repair_episode WHERE repair_episode_id=?", (repair_id,)).fetchone()
                if row is None:
                    raise ValueError(f"Repair episode not found: {repair_id}")
                current = str(row["status"])
                if to_state not in REPAIR_TRANSITIONS.get(current, set()):
                    raise ValueError(f"illegal Repair transition: {current} -> {to_state}")
                if to_state == "repaired":
                    actions = list(json_loads(row["repair_actions_json"], []))
                    if repair_action:
                        actions.append(dict(repair_action))
                    if not actions and user_outcome != "accepted":
                        raise ValueError("Repair cannot become repaired without corrective action or explicit user acceptance")
                else:
                    actions = list(json_loads(row["repair_actions_json"], []))
                    if repair_action:
                        actions.append(dict(repair_action))
                history = list(json_loads(row["state_history_json"], []))
                history.append({"from": current, "to": to_state, "actor": actor, "occurred_at": utc_now()})
                closed_at = utc_now() if to_state in {"repaired", "accepted_difference"} else None
                self._conn.execute(
                    """
                    UPDATE relationship_repair_episode
                    SET status=?,repair_actions_json=?,user_outcome=COALESCE(?,user_outcome),
                        state_history_json=?,updated_at=?,closed_at=?
                    WHERE repair_episode_id=?
                    """,
                    (to_state, json_dumps(actions), user_outcome, json_dumps(history), utc_now(), closed_at, repair_id),
                )
                rebuild = self._rebuild_pair_tx(self._conn, pair_id=row["pair_id"], engine=engine, reason="repair_transition", actor=actor, bypass_debounce=True)
                self._enqueue_domain_event_tx(
                    self._conn,
                    pair_id=row["pair_id"],
                    event_type="repair_state_changed",
                    source_ref=f"repair:{repair_id}:transition:{to_state}",
                    payload={"from": current, "to": to_state, "affect_stimulus_hint": "repair_relief" if to_state == "repaired" else "relationship_rupture"},
                    origin_chain=["interaction", "relationship"],
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {"repair": self.repair(repair_id), **rebuild}

    def list_outbox(self, *, status: str | None = None, limit: int = 100) -> list[JsonDict]:
        if status:
            rows = self._read_all("SELECT * FROM relationship_outbox WHERE status=? ORDER BY created_at LIMIT ?", (status, limit))
        else:
            rows = self._read_all("SELECT * FROM relationship_outbox ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._outbox_row(row) for row in rows]

    def deliverable_outbox(self, *, limit: int = 100) -> list[JsonDict]:
        now = utc_now()
        rows = self._read_all(
            """
            SELECT * FROM relationship_outbox
            WHERE status IN ('pending','retry')
              AND (next_attempt_at IS NULL OR next_attempt_at<=?)
            ORDER BY created_at,event_id LIMIT ?
            """,
            (now, limit),
        )
        return [self._outbox_row(row) for row in rows]

    def inbox_status_count(self, status: str) -> int:
        row = self._read_one("SELECT COUNT(*) AS count FROM relationship_inbox WHERE status=?", (status,))
        return int(row["count"] if row else 0)

    def acknowledge_outbox(self, event_id: str, *, status: str, error: str | None = None) -> JsonDict:
        if status not in {"delivered", "retry", "dead_letter"}:
            raise ValueError("invalid Relationship outbox acknowledgement")
        with self._lock:
            row = self._conn.execute("SELECT * FROM relationship_outbox WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise ValueError(f"Relationship outbox event not found: {event_id}")
            attempts = int(row["attempts"]) + 1
            next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=min(3600, 2 ** attempts))).isoformat() if status == "retry" else None
            self._conn.execute(
                "UPDATE relationship_outbox SET status=?,attempts=?,next_attempt_at=?,last_error=?,processed_at=? WHERE event_id=?",
                (status, attempts, next_attempt, error, utc_now() if status in {"delivered", "dead_letter"} else None, event_id),
            )
            self._conn.commit()
        return self._outbox_row(self._read_one("SELECT * FROM relationship_outbox WHERE event_id=?", (event_id,)))

    def resolve_dead_letter(self, event_id: str, *, resolution: str, actor: str, reason: str, export_ref: str | None = None) -> JsonDict:
        if resolution not in {"retry", "discarded", "exported"}:
            raise ValueError("unsupported Relationship dead-letter resolution")
        if resolution == "exported" and not export_ref:
            raise ValueError("exported dead-letter requires export_ref")
        with self._lock:
            row = self._conn.execute("SELECT status FROM relationship_outbox WHERE event_id=?", (event_id,)).fetchone()
            if row is None or row["status"] != "dead_letter":
                raise ValueError("Relationship outbox event is not dead-lettered")
            self._conn.execute(
                "INSERT OR REPLACE INTO relationship_dead_letter_resolution VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, resolution, export_ref, actor, reason, utc_now()),
            )
            if resolution == "retry":
                self._conn.execute("UPDATE relationship_outbox SET status='pending',next_attempt_at=NULL,last_error=NULL WHERE event_id=?", (event_id,))
            self._conn.commit()
        return {"event_id": event_id, "resolution": resolution, "actor": actor}

    def record_audit(
        self,
        *,
        pair_id: str | None,
        action: str,
        entity_type: str,
        entity_id: str,
        actor: str,
        details: Mapping[str, Any],
        correlation_id: str | None = None,
    ) -> None:
        with self._lock:
            self._insert_audit(self._conn, pair_id=pair_id, action=action, entity_type=entity_type, entity_id=entity_id, actor=actor, details=details, correlation_id=correlation_id)
            self._conn.commit()

    def list_audit(self, *, pair_id: str | None = None, action: str | None = None, limit: int = 100) -> list[JsonDict]:
        clauses: list[str] = []
        params: list[Any] = []
        if pair_id:
            clauses.append("pair_id=?")
            params.append(pair_id)
        if action:
            clauses.append("action=?")
            params.append(action)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        rows = self._read_all(f"SELECT * FROM relationship_audit{where} ORDER BY created_at DESC LIMIT ?", tuple(params))
        return [{"audit_id": row["audit_id"], "pair_id": row["pair_id"], "action": row["action"], "entity_type": row["entity_type"], "entity_id": row["entity_id"], "actor": row["actor"], "correlation_id": row["correlation_id"], "details": json_loads(row["details_json"], {}), "created_at": row["created_at"]} for row in rows]

    def record_metric(self, name: str, value: float, *, pair_id: str | None = None, labels: Mapping[str, Any] | None = None) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO relationship_metric VALUES (?, ?, ?, ?, ?, ?)", (new_id("relm"), pair_id, name, float(value), json_dumps(dict(labels or {})), utc_now()))
            self._conn.commit()

    def run_retention(self, *, now: datetime | None = None, batch_size: int = 500) -> JsonDict:
        current = now or datetime.now(timezone.utc)
        trace_cutoff = (current - timedelta(days=30)).isoformat()
        outbox_cutoff = (current - timedelta(days=14)).isoformat()
        with self._lock:
            metrics = self._conn.execute("DELETE FROM relationship_metric WHERE created_at<?", (trace_cutoff,)).rowcount
            outbox = self._conn.execute("DELETE FROM relationship_outbox WHERE status='delivered' AND processed_at<?", (outbox_cutoff,)).rowcount
            expired = self._conn.execute(
                "UPDATE relationship_candidate SET status='rejected',gate_reason='deferred_ttl_expired',updated_at=?,decided_at=? WHERE status='deferred' AND deferred_until IS NOT NULL AND deferred_until<?",
                (utc_now(), utc_now(), current.isoformat()),
            ).rowcount
            agreement_expired = self._conn.execute(
                "UPDATE relationship_agreement_amendment_candidate SET status='expired',decided_at=? WHERE status='pending_confirmation' AND expires_at IS NOT NULL AND expires_at<=?",
                (current.isoformat(), current.isoformat()),
            ).rowcount
            self._conn.commit()
        return {
            "metrics_deleted": metrics,
            "outbox_deleted": outbox,
            "deferred_expired": expired,
            "agreement_proposals_expired": agreement_expired,
            "batch_size": batch_size,
        }

    def table_counts(self) -> JsonDict:
        tables = [
            "relationship_pair",
            "relationship_inbox",
            "relationship_candidate",
            "relationship_contribution",
            "relationship_contribution_status_event",
            "relationship_state_snapshot",
            "relationship_estimate_snapshot",
            "relationship_agreement",
            "relationship_repair_episode",
            "relationship_frame",
            "relationship_outbox",
            "relationship_tombstone",
            "relationship_audit",
        ]
        return {table: int(self._read_one(f"SELECT COUNT(*) AS count FROM {table}")["count"]) for table in tables}

    def pragma_health(self) -> JsonDict:
        with self._lock:
            integrity = self._conn.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = bool(self._conn.execute("PRAGMA foreign_keys").fetchone()[0])
            journal_mode = str(self._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        return {"integrity": integrity, "foreign_keys": foreign_keys, "journal_mode": journal_mode, "path": str(self.path)}

    def export_pair(self, pair_id: str, *, include_audit: bool = True, include_restore_secrets: bool = False) -> JsonDict:
        contributions = self.list_contributions(pair_id=pair_id, limit=100000)
        contribution_ids = {item["contribution_id"] for item in contributions}
        status_events = [
            {
                "status_event_id": row["status_event_id"],
                "contribution_id": row["contribution_id"],
                "status": row["status"],
                "reason": row["reason"],
                "actor": row["actor"],
                "source_ref": row["source_ref"],
                "occurred_at": row["occurred_at"],
                "correlation_id": row["correlation_id"],
                "schema_version": row["schema_version"],
            }
            for row in self._read_all(
                """
                SELECT s.* FROM relationship_contribution_status_event s
                JOIN relationship_contribution c ON c.contribution_id=s.contribution_id
                WHERE c.pair_id=? ORDER BY s.occurred_at,s.status_event_id
                """,
                (pair_id,),
            )
            if row["contribution_id"] in contribution_ids
        ]
        agreements = [
            self._agreement_row(row)
            for row in self._read_all(
                "SELECT * FROM relationship_agreement WHERE pair_id=? ORDER BY revision",
                (pair_id,),
            )
        ]
        repairs = [
            self._repair_row(row)
            for row in self._read_all(
                "SELECT * FROM relationship_repair_episode WHERE pair_id=? ORDER BY opened_at",
                (pair_id,),
            )
        ]
        tombstones = [
            {"ref_kind": row["ref_kind"], "reason_code": row["reason_code"], "created_at": row["created_at"]}
            for row in self._read_all(
                "SELECT ref_kind,reason_code,created_at FROM relationship_tombstone WHERE pair_id=? ORDER BY created_at",
                (pair_id,),
            )
        ]
        bundle = {
            "schema_version": "RelationshipExportBundle.v1",
            "exported_at": utc_now(),
            "pair": self._redacted_pair(self.pair(pair_id=pair_id) or {}),
            "policy": self.profile(),
            "state_snapshot": self.active_state_snapshot(pair_id),
            "estimate_snapshot": self.active_estimate_snapshot(pair_id),
            "frame": self.active_frame(pair_id),
            "agreement": self.active_agreement(pair_id),
            "agreement_history": agreements,
            "open_repair": self.open_repair(pair_id),
            "repair_history": repairs,
            "contributions": contributions,
            "contribution_status_events": status_events,
            "controls": self._controls(pair_id),
            "tombstone_metadata": tombstones,
            "recent_audit": self.list_audit(pair_id=pair_id, limit=500) if include_audit else [],
            "checksums": {
                "state": snapshot_checksum(self.active_state_snapshot(pair_id)),
                "estimate": snapshot_checksum(self.active_estimate_snapshot(pair_id)),
            },
        }
        if include_restore_secrets:
            raw_pair = self.pair(pair_id=pair_id) or {}
            bundle["restore_secrets"] = {
                "tombstone_salt": raw_pair.get("tombstone_salt"),
                "tombstones": [
                    {
                        "ref_hash": row["ref_hash"],
                        "ref_kind": row["ref_kind"],
                        "reason_code": row["reason_code"],
                        "created_at": row["created_at"],
                    }
                    for row in self._read_all(
                        "SELECT ref_hash,ref_kind,reason_code,created_at FROM relationship_tombstone WHERE pair_id=? ORDER BY created_at",
                        (pair_id,),
                    )
                ],
            }
        bundle["checksums"]["bundle"] = hash_payload({key: value for key, value in bundle.items() if key != "checksums"})
        return bundle

    def restore_export_bundle(
        self,
        pair_id: str,
        bundle: Mapping[str, Any],
        *,
        engine: RelationshipDynamicsEngine,
        actor: str,
        replace: bool = False,
    ) -> JsonDict:
        if str(bundle.get("schema_version")) != "RelationshipExportBundle.v1":
            raise ValueError("unsupported Relationship restore bundle")
        contributions = [dict(item) for item in bundle.get("contributions") or []]
        status_events = [dict(item) for item in bundle.get("contribution_status_events") or []]
        source_agreement = dict(bundle.get("agreement") or {})
        source_repair = dict(bundle.get("open_repair") or {})
        source_state = dict(bundle.get("state_snapshot") or {})
        restore_secrets = dict(bundle.get("restore_secrets") or {})
        active_existing = self.active_contributions(pair_id)
        if active_existing and not replace:
            raise ValueError("target Relationship pair is not empty; explicit replace is required")
        restored_at = utc_now()
        contribution_map: dict[str, str] = {}
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if replace:
                    for item in active_existing:
                        self._insert_status_event(
                            self._conn,
                            contribution_id=str(item["contribution_id"]),
                            status="deactivated",
                            reason="replaced_by_verified_restore",
                            actor=actor,
                            source_ref="relationship_restore",
                        )
                if source_agreement:
                    current = self._active_agreement_tx(self._conn, pair_id)
                    revision = int(
                        self._conn.execute(
                            "SELECT COALESCE(MAX(revision),0)+1 FROM relationship_agreement WHERE pair_id=?",
                            (pair_id,),
                        ).fetchone()[0]
                    )
                    self._conn.execute(
                        "UPDATE relationship_agreement SET status='amended',updated_at=? WHERE pair_id=? AND status='active'",
                        (restored_at, pair_id),
                    )
                    restored_agreement = {
                        "schema_version": RELATIONSHIP_AGREEMENT_CONTRACT,
                        "agreement_id": new_id("rag"),
                        "pair_id": pair_id,
                        "revision": revision,
                        "mode": str(source_agreement.get("mode") or "companion"),
                        "status": "active",
                        "terms": dict(source_agreement.get("terms") or {}),
                        "consent": dict(source_agreement.get("consent") or {}),
                        "source_refs": ["encrypted_relationship_restore", *list(source_agreement.get("source_refs") or [])],
                        "supersedes": current.get("agreement_id"),
                    }
                    self._insert_agreement(self._conn, restored_agreement, timestamp=restored_at)
                for index, item in enumerate(contributions):
                    old_contribution_id = str(item.get("contribution_id") or f"restore-source-{index}")
                    candidate_id = new_id("relc")
                    contribution_id = new_id("relcon")
                    contribution_map[old_contribution_id] = contribution_id
                    candidate = RelationshipEvidenceCandidate.from_dict(
                        {
                            "candidate_id": candidate_id,
                            "pair_id": pair_id,
                            "direction_scope": item.get("direction_scope"),
                            "dimension": item.get("dimension"),
                            "direction": item.get("direction"),
                            "evidence_type": item.get("evidence_type"),
                            "source_kind": item.get("source_kind") or "encrypted_restore",
                            "source_ref": item.get("source_ref") or f"restore:{old_contribution_id}",
                            "evidence_refs": list(item.get("evidence_refs") or [f"restore:{old_contribution_id}"]),
                            "confidence": item.get("confidence", 0.0),
                            "evidence_strength": item.get("evidence_strength", 0.0),
                            "source_reliability": item.get("source_reliability", 0.0),
                            "direction_certainty": item.get("direction_certainty", 0.0),
                            "consistency": item.get("consistency", 0.0),
                            "novelty": item.get("novelty_factor", 1.0),
                            "conflict_score": item.get("conflict_score", 0.0),
                            "sensitivity": item.get("sensitivity", "low"),
                            "subject_key": item.get("subject_key", "general"),
                            "context_key": item.get("context_key", "general"),
                            "episode_ref": item.get("episode_ref"),
                            "reason_codes": ["verified_encrypted_restore"],
                            "metadata": dict(item.get("provenance") or {}),
                        }
                    )
                    candidate_hash = hash_payload(
                        {
                            "restore_bundle": dict(bundle.get("checksums") or {}).get("bundle"),
                            "source_contribution_id": old_contribution_id,
                            "pair_id": pair_id,
                        }
                    )
                    self._conn.execute(
                        """
                        INSERT INTO relationship_candidate(
                            candidate_id,pair_id,inbox_id,candidate_hash,schema_version,direction_scope,
                            dimension,direction,evidence_type,source_kind,source_ref,subject_key,context_key,
                            episode_ref,payload_json,status,gate_reason,provider_trace_json,attempts,
                            created_at,updated_at,decided_at
                        ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'committed',
                                  'verified_encrypted_restore', ?, 0, ?, ?, ?)
                        """,
                        (
                            candidate_id,
                            pair_id,
                            candidate_hash,
                            candidate.schema_version,
                            candidate.direction_scope,
                            candidate.dimension,
                            candidate.direction,
                            candidate.evidence_type,
                            candidate.source_kind,
                            candidate.source_ref,
                            candidate.subject_key,
                            candidate.context_key,
                            candidate.episode_ref,
                            json_dumps(candidate.to_dict()),
                            json_dumps({"provider": "encrypted_restore", "source_candidate_id": item.get("candidate_id")}),
                            restored_at,
                            restored_at,
                            restored_at,
                        ),
                    )
                    self._conn.execute(
                        """
                        INSERT INTO relationship_contribution(
                            contribution_id,pair_id,candidate_id,direction_scope,dimension,direction,
                            evidence_type,source_kind,source_ref,subject_key,context_key,episode_ref,
                            evidence_refs_json,confidence,evidence_strength,source_reliability,
                            direction_certainty,consistency,novelty_factor,conflict_score,support,
                            raw_delta,bounded_delta,sensitivity,algorithm_version,policy_version,
                            provenance_json,occurred_at,created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            contribution_id,
                            pair_id,
                            candidate_id,
                            candidate.direction_scope,
                            candidate.dimension,
                            candidate.direction,
                            candidate.evidence_type,
                            candidate.source_kind,
                            candidate.source_ref,
                            candidate.subject_key,
                            candidate.context_key,
                            candidate.episode_ref,
                            json_dumps(list(candidate.evidence_refs)),
                            candidate.confidence,
                            candidate.evidence_strength,
                            candidate.source_reliability,
                            candidate.direction_certainty,
                            candidate.consistency,
                            float(item.get("novelty_factor", 1.0)),
                            float(item.get("conflict_score", 0.0)),
                            float(item.get("support", 0.0)),
                            float(item.get("raw_delta", 0.0)),
                            float(item.get("bounded_delta", 0.0)),
                            candidate.sensitivity,
                            str(item.get("algorithm_version") or engine.profile.get("algorithm_version")),
                            str(item.get("policy_version") or engine.profile.get("version")),
                            json_dumps({**dict(item.get("provenance") or {}), "restore_source_contribution_id": old_contribution_id}),
                            str(item.get("occurred_at") or restored_at),
                            restored_at,
                        ),
                    )
                grouped_events: dict[str, list[JsonDict]] = {}
                for event in status_events:
                    grouped_events.setdefault(str(event.get("contribution_id") or ""), []).append(event)
                for old_id, new_id_value in contribution_map.items():
                    source_events = sorted(
                        grouped_events.get(old_id) or [],
                        key=lambda item: (str(item.get("occurred_at") or ""), str(item.get("status_event_id") or "")),
                    )
                    if not source_events:
                        source_item = next(item for item in contributions if str(item.get("contribution_id") or "") == old_id)
                        source_events = [{"status": source_item.get("status", "active"), "reason": "restored_current_status", "actor": actor, "occurred_at": restored_at}]
                    for event in source_events:
                        self._conn.execute(
                            "INSERT INTO relationship_contribution_status_event VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                new_id("relst"),
                                new_id_value,
                                str(event.get("status") or "deactivated"),
                                str(event.get("reason") or "encrypted_restore"),
                                str(event.get("actor") or actor),
                                event.get("source_ref"),
                                str(event.get("occurred_at") or restored_at),
                                event.get("correlation_id"),
                                RELATIONSHIP_STATUS_EVENT_CONTRACT,
                            ),
                        )
                if source_repair:
                    self._conn.execute(
                        """
                        INSERT INTO relationship_repair_episode(
                            repair_episode_id,pair_id,status,trigger_ref,evidence_refs_json,
                            affected_dimensions_json,responsibility_json,requested_repair_json,
                            repair_actions_json,user_outcome,repeated_violation_key,
                            repeated_violation_count,policy_severity,state_history_json,
                            opened_at,updated_at,closed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_id("repair"),
                            pair_id,
                            str(source_repair.get("status") or "unresolved"),
                            str(source_repair.get("trigger_ref") or "encrypted_restore"),
                            json_dumps(list(source_repair.get("evidence_refs") or ["encrypted_restore"])),
                            json_dumps(list(source_repair.get("affected_dimensions") or [])),
                            json_dumps(dict(source_repair.get("responsibility") or {})),
                            json_dumps(dict(source_repair.get("requested_repair") or {})),
                            json_dumps(list(source_repair.get("repair_actions") or [])),
                            source_repair.get("user_outcome"),
                            source_repair.get("repeated_violation_key"),
                            int(source_repair.get("repeated_violation_count") or 0),
                            str(source_repair.get("policy_severity") or "normal"),
                            json_dumps(list(source_repair.get("state_history") or [])),
                            str(source_repair.get("opened_at") or restored_at),
                            restored_at,
                            source_repair.get("closed_at"),
                        ),
                    )
                secret_salt = restore_secrets.get("tombstone_salt")
                if secret_salt:
                    self._conn.execute("UPDATE relationship_pair SET tombstone_salt=? WHERE pair_id=?", (str(secret_salt), pair_id))
                    for item in restore_secrets.get("tombstones") or []:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO relationship_tombstone VALUES (?, ?, ?, ?, ?)",
                            (
                                pair_id,
                                str(item.get("ref_hash") or ""),
                                str(item.get("ref_kind") or "source_ref"),
                                str(item.get("reason_code") or "restored_forget"),
                                str(item.get("created_at") or restored_at),
                            ),
                        )
                result = self._rebuild_pair_tx(
                    self._conn,
                    pair_id=pair_id,
                    engine=engine,
                    reason="verified_encrypted_restore",
                    actor=actor,
                    bypass_debounce=True,
                    relationship_started_at_override=str(source_state.get("relationship_started_at") or "") or None,
                    previous_snapshot_override=source_state or None,
                )
                self._insert_audit(
                    self._conn,
                    pair_id=pair_id,
                    action="relationship_encrypted_restore_activated",
                    entity_type="restore",
                    entity_id=str(dict(bundle.get("checksums") or {}).get("bundle") or new_id("restore")),
                    actor=actor,
                    details={"imported_contributions": len(contribution_map), "replace": replace, "source_pair_ref": dict(bundle.get("pair") or {}).get("pair_id")},
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {"status": "restored", "pair_id": pair_id, "imported_contributions": len(contribution_map), **result}

    def _rebuild_pair_tx(
        self,
        conn: sqlite3.Connection,
        *,
        pair_id: str,
        engine: RelationshipDynamicsEngine,
        reason: str,
        actor: str,
        correlation_id: str | None = None,
        bypass_debounce: bool = False,
        relationship_started_at_override: str | None = None,
        previous_snapshot_override: Mapping[str, Any] | None = None,
    ) -> JsonDict:
        pair_row = conn.execute("SELECT * FROM relationship_pair WHERE pair_id=?", (pair_id,)).fetchone()
        if pair_row is None:
            raise ValueError(f"Relationship pair not found: {pair_id}")
        contributions = self._active_contributions_tx(conn, pair_id)
        agreement = self._active_agreement_tx(conn, pair_id)
        repair_row = conn.execute(
            "SELECT * FROM relationship_repair_episode WHERE pair_id=? ORDER BY updated_at DESC LIMIT 1",
            (pair_id,),
        ).fetchone()
        repair = self._repair_row(repair_row) if repair_row else None
        previous_state_row = conn.execute("SELECT * FROM relationship_state_snapshot WHERE snapshot_id=?", (pair_row["active_state_snapshot_id"],)).fetchone()
        previous_state = dict(previous_snapshot_override) if previous_snapshot_override is not None else (self._state_snapshot_row(previous_state_row) if previous_state_row else None)
        generated_at = utc_now()
        state = engine.aggregate_state(
            contributions,
            agreement=agreement,
            repair_state=str((repair or {}).get("status") or "stable"),
            previous_snapshot=previous_state,
            relationship_started_at=str(relationship_started_at_override or (previous_state or {}).get("relationship_started_at") or pair_row["created_at"]),
            generated_at=generated_at,
        )
        estimate = engine.aggregate_estimate(contributions, generated_at=generated_at)
        state_revision = int(conn.execute("SELECT COALESCE(MAX(revision),-1)+1 FROM relationship_state_snapshot WHERE pair_id=?", (pair_id,)).fetchone()[0])
        estimate_revision = int(conn.execute("SELECT COALESCE(MAX(revision),-1)+1 FROM relationship_estimate_snapshot WHERE pair_id=?", (pair_id,)).fetchone()[0])
        generation = int(pair_row["generation"]) + 1
        state.update({
            "snapshot_id": new_id("rels"),
            "pair_id": pair_id,
            "revision": state_revision,
            "generation": generation,
            "status": "active",
            "agreement_revision": int(agreement.get("revision", 0)),
            "source_watermark": len(contributions),
        })
        estimate.update({
            "snapshot_id": new_id("rele"),
            "pair_id": pair_id,
            "revision": estimate_revision,
            "generation": generation,
            "status": "active",
            "source_watermark": len(contributions),
        })
        conn.execute("UPDATE relationship_state_snapshot SET status='superseded' WHERE pair_id=? AND status='active'", (pair_id,))
        conn.execute("UPDATE relationship_estimate_snapshot SET status='superseded' WHERE pair_id=? AND status='active'", (pair_id,))
        conn.execute("UPDATE relationship_frame SET status='superseded',invalidated_at=? WHERE pair_id=? AND status='active'", (generated_at, pair_id))
        self._insert_state_snapshot(conn, state)
        self._insert_estimate_snapshot(conn, estimate)
        frame = engine.build_frame(
            pair_id=pair_id,
            state_snapshot=state,
            estimate_snapshot=estimate,
            agreement=agreement,
            repair=repair,
            surface_permission=str(pair_row["surface_permission"]),
            generated_at=generated_at,
        )
        frame_id = new_id("relf")
        self._insert_frame(conn, frame_id, pair_id, state, estimate, frame)
        conn.execute(
            """
            UPDATE relationship_pair
            SET active_state_snapshot_id=?,active_estimate_snapshot_id=?,active_frame_id=?,generation=?,updated_at=?
            WHERE pair_id=?
            """,
            (state["snapshot_id"], estimate["snapshot_id"], frame_id, generation, generated_at, pair_id),
        )
        self._enqueue_domain_event_tx(
            conn,
            pair_id=pair_id,
            event_type="state_snapshot_published",
            source_ref=f"snapshot:{state['snapshot_id']}",
            payload={"state_snapshot_version": state_revision, "estimate_snapshot_version": estimate_revision, "bond_stage": state["bond_stage"], "reason": reason, "bypass_debounce": bypass_debounce},
            origin_chain=["relationship"],
            correlation_id=correlation_id,
        )
        self._insert_audit(
            conn,
            pair_id=pair_id,
            action="relationship_pair_rebuilt",
            entity_type="frame",
            entity_id=frame_id,
            actor=actor,
            details={"reason": reason, "generation": generation, "state_revision": state_revision, "estimate_revision": estimate_revision},
            correlation_id=correlation_id,
        )
        return {"pair_id": pair_id, "generation": generation, "state_snapshot": state, "estimate_snapshot": estimate, "frame": frame}

    def _active_contributions_tx(self, conn: sqlite3.Connection, pair_id: str) -> list[JsonDict]:
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT contribution_id,status,
                       ROW_NUMBER() OVER (PARTITION BY contribution_id ORDER BY occurred_at DESC,status_event_id DESC) AS rn
                FROM relationship_contribution_status_event
            )
            SELECT c.* FROM relationship_contribution c
            JOIN latest s ON s.contribution_id=c.contribution_id AND s.rn=1
            WHERE c.pair_id=? AND s.status='active'
            ORDER BY c.occurred_at,c.contribution_id
            """,
            (pair_id,),
        ).fetchall()
        return [self._contribution_row(row) for row in rows]

    def _active_agreement_tx(self, conn: sqlite3.Connection, pair_id: str) -> JsonDict:
        row = conn.execute("SELECT * FROM relationship_agreement WHERE pair_id=? AND status='active' ORDER BY revision DESC LIMIT 1", (pair_id,)).fetchone()
        return self._agreement_row(row) if row else {"pair_id": pair_id, "revision": 0, "mode": "organic", "status": "none", "terms": {}, "consent": {}, "source_refs": []}

    @staticmethod
    def _insert_state_snapshot(conn: sqlite3.Connection, state: Mapping[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO relationship_state_snapshot(
                snapshot_id,pair_id,revision,generation,status,bond_stage,values_json,
                agreement_revision,repair_state,source_watermark,source_contribution_count,
                episode_count,relationship_started_at,stage_evidence_json,algorithm_version,
                policy_version,checksum,generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state["snapshot_id"], state["pair_id"], state["revision"], state["generation"], state["status"], state["bond_stage"],
                json_dumps(dict(state.get("sora_to_user") or {})), state.get("agreement_revision", 0), state.get("repair_state", "stable"),
                state.get("source_watermark", 0), state.get("source_contribution_count", 0), state.get("episode_count", 0),
                state.get("relationship_started_at"), json_dumps(dict(state.get("stage_evidence") or {})), state.get("algorithm_version"),
                state.get("policy_version"), state.get("checksum"), state.get("generated_at"),
            ),
        )

    @staticmethod
    def _insert_estimate_snapshot(conn: sqlite3.Connection, estimate: Mapping[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO relationship_estimate_snapshot(
                snapshot_id,pair_id,revision,generation,status,values_json,source_watermark,
                source_contribution_count,algorithm_version,checksum,generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                estimate["snapshot_id"], estimate["pair_id"], estimate["revision"], estimate["generation"], estimate["status"],
                json_dumps(dict(estimate.get("user_to_sora_estimate") or {})), estimate.get("source_watermark", 0),
                estimate.get("source_contribution_count", 0), estimate.get("algorithm_version"), estimate.get("checksum"), estimate.get("generated_at"),
            ),
        )

    @staticmethod
    def _insert_frame(
        conn: sqlite3.Connection,
        frame_id: str,
        pair_id: str,
        state: Mapping[str, Any],
        estimate: Mapping[str, Any],
        frame: Mapping[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO relationship_frame(
                frame_id,pair_id,state_snapshot_id,estimate_snapshot_id,state_snapshot_version,
                estimate_snapshot_version,status,frame_json,checksum,generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (frame_id, pair_id, state["snapshot_id"], estimate["snapshot_id"], state["revision"], estimate["revision"], json_dumps(dict(frame)), frame.get("checksum"), frame.get("generated_at")),
        )

    @staticmethod
    def _insert_agreement(conn: sqlite3.Connection, agreement: Mapping[str, Any], *, timestamp: str) -> None:
        conn.execute(
            """
            INSERT INTO relationship_agreement(
                agreement_id,pair_id,revision,mode,status,terms_json,consent_json,
                source_refs_json,supersedes,created_at,updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agreement["agreement_id"], agreement["pair_id"], agreement["revision"], agreement["mode"], agreement["status"],
                json_dumps(dict(agreement.get("terms") or {})), json_dumps(dict(agreement.get("consent") or {})),
                json_dumps(list(agreement.get("source_refs") or [])), agreement.get("supersedes"), timestamp, timestamp,
            ),
        )

    @staticmethod
    def _insert_status_event(
        conn: sqlite3.Connection,
        *,
        contribution_id: str,
        status: str,
        reason: str,
        actor: str,
        source_ref: str | None,
        correlation_id: str | None = None,
    ) -> str:
        status_id = new_id("relcs")
        conn.execute(
            "INSERT INTO relationship_contribution_status_event VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (status_id, contribution_id, status, reason, actor, source_ref, utc_now(), correlation_id, RELATIONSHIP_STATUS_EVENT_CONTRACT),
        )
        return status_id

    def _enqueue_domain_event_tx(
        self,
        conn: sqlite3.Connection,
        *,
        pair_id: str,
        event_type: str,
        source_ref: str,
        payload: Mapping[str, Any],
        origin_chain: Sequence[str],
        correlation_id: str | None = None,
    ) -> str:
        if event_type not in DOMAIN_EVENT_TYPES:
            raise ValueError(f"unsupported Relationship domain event type: {event_type}")
        event_id = new_id("reld")
        pair = conn.execute("SELECT generation FROM relationship_pair WHERE pair_id=?", (pair_id,)).fetchone()
        state = conn.execute("SELECT revision FROM relationship_state_snapshot WHERE pair_id=? AND status='active' ORDER BY revision DESC LIMIT 1", (pair_id,)).fetchone()
        estimate = conn.execute("SELECT revision FROM relationship_estimate_snapshot WHERE pair_id=? AND status='active' ORDER BY revision DESC LIMIT 1", (pair_id,)).fetchone()
        domain_event = {
            "schema_version": RELATIONSHIP_DOMAIN_EVENT_CONTRACT,
            "event_id": event_id,
            "event_type": event_type,
            "pair_id": pair_id,
            "state_snapshot_version": int(state["revision"]) if state else 0,
            "estimate_snapshot_version": int(estimate["revision"]) if estimate else 0,
            "relationship_generation": int(pair["generation"]) if pair else 0,
            "source_ref": source_ref,
            "payload": dict(payload),
            "correlation_id": correlation_id,
            "origin_chain": list(origin_chain),
            "occurred_at": utc_now(),
        }
        event_key = f"{pair_id}:{event_type}:{source_ref}"
        payload_hash = hash_payload(domain_event)
        conn.execute(
            """
            INSERT OR IGNORE INTO relationship_outbox(
                event_id,pair_id,event_type,event_key,payload_hash,payload_json,status,
                attempts,max_attempts,created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, 5, ?)
            """,
            (event_id, pair_id, event_type, event_key, payload_hash, json_dumps(domain_event), utc_now()),
        )
        return event_id

    @staticmethod
    def _insert_audit(
        conn: sqlite3.Connection,
        *,
        pair_id: str | None,
        action: str,
        entity_type: str,
        entity_id: str,
        actor: str,
        details: Mapping[str, Any],
        correlation_id: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO relationship_audit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id("rela"), pair_id, action, entity_type, entity_id, actor, correlation_id, json_dumps(dict(details)), utc_now()),
        )

    def _seed_profile(self, profile: JsonDict) -> None:
        checksum = hash_payload(profile)
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO relationship_policy_profile VALUES (?, ?, ?, ?, 'active', ?)",
                (str(profile.get("version") or RELATIONSHIP_POLICY_VERSION), str(profile.get("schema_version") or "RelationshipPolicyProfile.v1"), json_dumps(profile), checksum, utc_now()),
            )
            self._conn.commit()

    def _tombstone_hash(self, pair_id: str, source_ref: str) -> str:
        pair = self.pair(pair_id=pair_id)
        if pair is None:
            raise ValueError(f"Relationship pair not found: {pair_id}")
        salt = str(pair["tombstone_salt"])
        return hashlib.sha256(f"{salt}\0{source_ref}".encode("utf-8")).hexdigest()

    @staticmethod
    def _redacted_event_payload(value: Mapping[str, Any]) -> JsonDict:
        payload = dict(value)
        for key in ("untrusted_text", "raw_text", "content", "message_text"):
            payload.pop(key, None)
        if isinstance(payload.get("memory"), Mapping):
            memory = dict(payload["memory"])
            memory.pop("raw_evidence", None)
            memory.pop("raw_text", None)
            payload["memory"] = memory
        return payload

    @staticmethod
    def _source_refs(event: RelationshipInputEvent, payload: Mapping[str, Any]) -> list[str]:
        refs = list(event.evidence_refs)
        source = dict(payload.get("source") or {})
        memory = dict(payload.get("memory") or {})
        refs.extend(str(value) for value in (source.get("source_ref"), memory.get("id"), payload.get("source_ref")) if value)
        return list(dict.fromkeys(refs))

    @staticmethod
    def _set_nested(target: JsonDict, path: Sequence[str], value: Any) -> None:
        if not path:
            raise ValueError("empty Agreement term path")
        cursor = target
        for item in path[:-1]:
            existing = cursor.get(item)
            if not isinstance(existing, dict):
                existing = {}
                cursor[item] = existing
            cursor = existing
        cursor[path[-1]] = value

    def _controls(self, pair_id: str) -> list[JsonDict]:
        rows = self._read_all("SELECT * FROM relationship_control_receipt WHERE pair_id=? ORDER BY created_at DESC", (pair_id,))
        return [{"receipt_id": row["receipt_id"], "control_type": row["control_type"], "value": json_loads(row["control_value_json"], None), "source_ref": row["source_ref"], "actor": row["actor"], "created_at": row["created_at"]} for row in rows]

    @staticmethod
    def _redacted_pair(pair: Mapping[str, Any]) -> JsonDict:
        return {key: value for key, value in pair.items() if key not in {"user_id", "tombstone_salt"}} | {"user_id_hash": hashlib.sha256(str(pair.get("user_id") or "").encode("utf-8")).hexdigest()}

    def _read_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _read_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    @staticmethod
    def _pair_row(row: sqlite3.Row) -> JsonDict:
        return {
            "pair_id": row["pair_id"], "sora_id": row["sora_id"], "user_id": row["user_id"], "status": row["status"],
            "profile_version": row["profile_version"], "tombstone_salt": row["tombstone_salt"],
            "active_state_snapshot_id": row["active_state_snapshot_id"], "active_estimate_snapshot_id": row["active_estimate_snapshot_id"],
            "active_frame_id": row["active_frame_id"], "learning_paused": bool(row["learning_paused"]),
            "surface_permission": row["surface_permission"], "generation": int(row["generation"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    @staticmethod
    def _inbox_row(row: sqlite3.Row) -> JsonDict:
        return {
            "inbox_id": row["inbox_id"], "pair_id": row["pair_id"], "source_system": row["source_system"], "event_id": row["event_id"],
            "source_revision": row["source_revision"], "action": row["action"], "schema_version": row["schema_version"], "payload_hash": row["payload_hash"],
            "payload": json_loads(row["payload_json"], {}), "status": row["status"], "attempts": int(row["attempts"]), "max_attempts": int(row["max_attempts"]),
            "last_error": row["last_error"], "quarantine_reason": row["quarantine_reason"], "correlation_id": row["correlation_id"], "turn_id": row["turn_id"],
            "origin_chain": json_loads(row["origin_chain_json"], []), "evidence_refs": json_loads(row["evidence_refs_json"], []), "sensitivity": row["sensitivity"],
            "occurred_at": row["occurred_at"], "received_at": row["received_at"], "processed_at": row["processed_at"],
        }

    @staticmethod
    def _candidate_row(row: sqlite3.Row) -> JsonDict:
        payload = dict(json_loads(row["payload_json"], {}))
        return {
            "candidate_id": row["candidate_id"], "pair_id": row["pair_id"], "inbox_id": row["inbox_id"], "candidate_hash": row["candidate_hash"],
            "status": row["status"], "gate_reason": row["gate_reason"], "provider_trace": json_loads(row["provider_trace_json"], {}),
            "deferred_until": row["deferred_until"], "created_at": row["created_at"], "updated_at": row["updated_at"], "decided_at": row["decided_at"],
            "candidate": payload,
        }

    @staticmethod
    def _contribution_row(row: sqlite3.Row) -> JsonDict:
        return {
            "contribution_id": row["contribution_id"], "pair_id": row["pair_id"], "candidate_id": row["candidate_id"],
            "direction_scope": row["direction_scope"], "dimension": row["dimension"], "direction": int(row["direction"]),
            "evidence_type": row["evidence_type"], "source_kind": row["source_kind"], "source_ref": row["source_ref"],
            "subject_key": row["subject_key"], "context_key": row["context_key"], "episode_ref": row["episode_ref"],
            "evidence_refs": json_loads(row["evidence_refs_json"], []), "confidence": float(row["confidence"]),
            "evidence_strength": float(row["evidence_strength"]), "source_reliability": float(row["source_reliability"]),
            "direction_certainty": float(row["direction_certainty"]), "consistency": float(row["consistency"]),
            "novelty_factor": float(row["novelty_factor"]), "conflict_score": float(row["conflict_score"]), "support": float(row["support"]),
            "raw_delta": float(row["raw_delta"]), "bounded_delta": float(row["bounded_delta"]), "sensitivity": row["sensitivity"],
            "algorithm_version": row["algorithm_version"], "policy_version": row["policy_version"], "provenance": json_loads(row["provenance_json"], {}),
            "occurred_at": row["occurred_at"], "created_at": row["created_at"],
        }

    @staticmethod
    def _state_snapshot_row(row: sqlite3.Row) -> JsonDict:
        return {
            "schema_version": RELATIONSHIP_STATE_SNAPSHOT_CONTRACT, "snapshot_id": row["snapshot_id"], "pair_id": row["pair_id"],
            "revision": int(row["revision"]), "generation": int(row["generation"]), "status": row["status"], "bond_stage": row["bond_stage"],
            "sora_to_user": json_loads(row["values_json"], {}), "agreement_revision": int(row["agreement_revision"]), "repair_state": row["repair_state"],
            "source_watermark": int(row["source_watermark"]), "source_contribution_count": int(row["source_contribution_count"]), "episode_count": int(row["episode_count"]),
            "relationship_started_at": row["relationship_started_at"], "stage_evidence": json_loads(row["stage_evidence_json"], {}),
            "algorithm_version": row["algorithm_version"], "policy_version": row["policy_version"], "checksum": row["checksum"], "generated_at": row["generated_at"],
        }

    @staticmethod
    def _estimate_snapshot_row(row: sqlite3.Row) -> JsonDict:
        return {
            "schema_version": RELATIONSHIP_ESTIMATE_SNAPSHOT_CONTRACT, "snapshot_id": row["snapshot_id"], "pair_id": row["pair_id"],
            "revision": int(row["revision"]), "generation": int(row["generation"]), "status": row["status"],
            "user_to_sora_estimate": json_loads(row["values_json"], {}), "source_watermark": int(row["source_watermark"]),
            "source_contribution_count": int(row["source_contribution_count"]), "algorithm_version": row["algorithm_version"],
            "checksum": row["checksum"], "generated_at": row["generated_at"],
        }

    @staticmethod
    def _frame_row(row: sqlite3.Row) -> JsonDict:
        frame = dict(json_loads(row["frame_json"], {}))
        frame.update({"frame_id": row["frame_id"], "status": row["status"], "checksum": row["checksum"]})
        return frame

    @staticmethod
    def _agreement_row(row: sqlite3.Row) -> JsonDict:
        return {
            "schema_version": RELATIONSHIP_AGREEMENT_CONTRACT, "agreement_id": row["agreement_id"], "pair_id": row["pair_id"],
            "revision": int(row["revision"]), "mode": row["mode"], "status": row["status"], "terms": json_loads(row["terms_json"], {}),
            "consent": json_loads(row["consent_json"], {}), "source_refs": json_loads(row["source_refs_json"], []), "supersedes": row["supersedes"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    @staticmethod
    def _repair_row(row: sqlite3.Row) -> JsonDict:
        return {
            "schema_version": RELATIONSHIP_REPAIR_CONTRACT, "repair_episode_id": row["repair_episode_id"], "pair_id": row["pair_id"], "status": row["status"],
            "trigger_ref": row["trigger_ref"], "evidence_refs": json_loads(row["evidence_refs_json"], []), "affected_dimensions": json_loads(row["affected_dimensions_json"], []),
            "responsibility": json_loads(row["responsibility_json"], {}), "requested_repair": json_loads(row["requested_repair_json"], {}),
            "repair_actions": json_loads(row["repair_actions_json"], []), "user_outcome": row["user_outcome"], "repeated_violation_key": row["repeated_violation_key"],
            "repeated_violation_count": int(row["repeated_violation_count"]), "policy_severity": row["policy_severity"],
            "state_history": json_loads(row["state_history_json"], []), "opened_at": row["opened_at"], "updated_at": row["updated_at"], "closed_at": row["closed_at"],
        }

    @staticmethod
    def _outbox_row(row: sqlite3.Row | None) -> JsonDict:
        if row is None:
            return {}
        return {
            "event_id": row["event_id"], "pair_id": row["pair_id"], "event_type": row["event_type"], "event_key": row["event_key"],
            "payload_hash": row["payload_hash"], "payload": json_loads(row["payload_json"], {}), "status": row["status"],
            "attempts": int(row["attempts"]), "max_attempts": int(row["max_attempts"]), "next_attempt_at": row["next_attempt_at"],
            "last_error": row["last_error"], "created_at": row["created_at"], "processed_at": row["processed_at"],
        }
