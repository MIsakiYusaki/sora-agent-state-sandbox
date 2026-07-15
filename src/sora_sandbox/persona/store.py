from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..memory_schema import json_dumps, json_loads, new_id, utc_now
from .contracts import (
    DEFAULT_DEFINITION,
    DEFAULT_TRAITS,
    PERSONA_FRAME_CONTRACT,
    PERSONA_POLICY_VERSION,
    PERSONA_SNAPSHOT_CONTRACT,
    JsonDict,
    PersonaGrowthCandidate,
    TraitDefinition,
    normalize_persona_value,
    trait_band,
)
from .coordinator import PersonaWriteCoordinator
from .schema import DDL, initialize_schema


def _hash_json(value: Any) -> str:
    return hashlib.sha256(json_dumps(value).encode("utf-8")).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _delete_ids(conn: sqlite3.Connection, table: str, ids: list[str]) -> None:
    if not ids:
        return
    allowed = {"persona_outbox", "persona_metric"}
    if table not in allowed:
        raise ValueError(f"unsupported Persona cleanup table: {table}")
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"DELETE FROM {table} WHERE id IN ({placeholders})", ids)


class PersonaStore:
    """Canonical Persona persistence with append-only contributions and rebuildable snapshots."""

    def __init__(
        self,
        path: Path | str,
        *,
        agent_id: str = "sora",
        definition: Mapping[str, Any] | None = None,
        traits: Iterable[TraitDefinition] = DEFAULT_TRAITS,
        queue_high_watermark: int = 500,
        queue_max: int = 1000,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.agent_id = agent_id
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            timeout=5.0,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        initialize_schema(self._conn, now=utc_now(), checksum=_hash_text(DDL))
        self._coordinator = PersonaWriteCoordinator(
            high_watermark=queue_high_watermark,
            max_queue=queue_max,
        )
        self._definition_seed = dict(definition or DEFAULT_DEFINITION)
        self._trait_seed = tuple(traits)
        for item in self._trait_seed:
            item.validate()
        self._write(self._seed_if_needed, priority=0, name="seed_persona")
        self.recover_interrupted_candidates()

    @property
    def queue_depth(self) -> int:
        return self._coordinator.queue_depth

    @property
    def under_backpressure(self) -> bool:
        return self._coordinator.under_backpressure

    def close(self) -> None:
        self._coordinator.close()
        with self._lock:
            self._conn.close()

    def reset(self) -> None:
        tables = (
            "persona_metric",
            "persona_audit",
            "persona_rebuild_run",
            "persona_dead_letter_resolution",
            "persona_outbox",
            "persona_tombstone",
            "persona_reflection",
            "persona_control",
            "persona_frame",
            "persona_ephemeral_state",
            "persona_interest_state",
            "persona_user_adaptation",
            "persona_snapshot",
            "persona_contribution",
            "persona_candidate",
            "persona_inbox",
            "persona_trait_definition",
            "persona_definition",
            "persona_instance",
        )

        def operation(conn: sqlite3.Connection) -> None:
            for table in tables:
                conn.execute(f"DELETE FROM {table}")
            self._seed_if_needed(conn)

        self._write(operation, priority=0, name="reset_persona")

    def definition(self, agent_id: str | None = None) -> JsonDict:
        selected = agent_id or self.agent_id
        row = self._read_one(
            "SELECT * FROM persona_definition WHERE agent_id=? AND status='active'",
            (selected,),
        )
        if row is None:
            raise RuntimeError(f"active Persona definition not found for {selected}")
        return {
            "agent_id": selected,
            "version": row["version"],
            "schema_version": row["schema_version"],
            **json_loads(row["definition_json"], {}),
        }

    def instance(self, agent_id: str | None = None) -> JsonDict:
        selected = agent_id or self.agent_id
        row = self._read_one("SELECT * FROM persona_instance WHERE agent_id=?", (selected,))
        if row is None:
            raise RuntimeError(f"Persona instance not found for {selected}")
        return {
            "agent_id": row["agent_id"],
            "status": row["status"],
            "active_definition_version": row["active_definition_version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def release_definition(
        self,
        definition: Mapping[str, Any],
        traits: Iterable[TraitDefinition],
        *,
        expected_version: int,
        release_ref: str,
        actor: str,
    ) -> JsonDict:
        proposed_traits = tuple(traits)
        for item in proposed_traits:
            item.validate()
        keys = {item.key for item in proposed_traits}
        if len(keys) != len(proposed_traits):
            raise ValueError("Persona trait release contains duplicate keys")
        if not release_ref:
            raise ValueError("Persona definition release_ref is required")

        def operation(conn: sqlite3.Connection) -> JsonDict:
            current = conn.execute(
                "SELECT * FROM persona_definition WHERE agent_id=? AND status='active'",
                (self.agent_id,),
            ).fetchone()
            if current is None or int(current["version"]) != expected_version:
                actual = int(current["version"]) if current else None
                raise RuntimeError(f"Persona definition version conflict: expected={expected_version}, actual={actual}")
            active_keys = {
                row["target_key"]
                for row in conn.execute(
                    "SELECT DISTINCT target_key FROM persona_contribution WHERE agent_id=? AND active=1",
                    (self.agent_id,),
                ).fetchall()
            }
            missing = active_keys - keys
            if missing:
                raise ValueError(f"Persona definition cannot remove traits with active contributions: {sorted(missing)}")
            version = expected_version + 1
            now = utc_now()
            conn.execute(
                "UPDATE persona_definition SET status='superseded' WHERE agent_id=? AND status='active'",
                (self.agent_id,),
            )
            conn.execute(
                "INSERT INTO persona_definition VALUES (?, ?, ?, 'active', ?, ?, ?)",
                (
                    self.agent_id,
                    version,
                    str(definition.get("schema_version") or "PersonaDefinition.v1"),
                    json_dumps(dict(definition)),
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE persona_instance SET active_definition_version=?, updated_at=? WHERE agent_id=?",
                (version, now, self.agent_id),
            )
            for item in proposed_traits:
                conn.execute(
                    "INSERT INTO persona_trait_definition VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.agent_id,
                        version,
                        item.key,
                        item.initial_value,
                        item.min_bound,
                        item.max_bound,
                        item.inertia,
                        item.per_event_limit,
                        item.daily_limit,
                        item.weekly_limit,
                        item.monthly_limit,
                        item.description,
                    ),
                )
            snapshot = self._rebuild_snapshot(conn, reason=f"definition_release:{release_ref}")
            self._insert_audit(
                conn,
                actor=actor,
                action="definition_released",
                entity_type="definition",
                entity_id=f"{self.agent_id}:{version}",
                details={"previous_version": expected_version, "release_ref": release_ref, "snapshot_id": snapshot["snapshot_id"]},
            )
            return {"definition_version": version, "release_ref": release_ref, "snapshot": snapshot}

        return self._write(operation, priority=0, name="release_persona_definition")

    def rollback_definition(
        self,
        target_version: int,
        *,
        reason: str,
        actor: str,
    ) -> JsonDict:
        def operation(conn: sqlite3.Connection) -> JsonDict:
            target = conn.execute(
                "SELECT * FROM persona_definition WHERE agent_id=? AND version=?",
                (self.agent_id, target_version),
            ).fetchone()
            if target is None:
                raise ValueError(f"Persona definition version not found: {target_version}")
            current = conn.execute(
                "SELECT version FROM persona_definition WHERE agent_id=? AND status='active'",
                (self.agent_id,),
            ).fetchone()
            if int(current["version"]) == target_version:
                return {"definition_version": target_version, "snapshot": self._active_snapshot_conn(conn), "idempotent": True}
            target_keys = {
                row["trait_key"]
                for row in conn.execute(
                    "SELECT trait_key FROM persona_trait_definition WHERE agent_id=? AND definition_version=?",
                    (self.agent_id, target_version),
                ).fetchall()
            }
            active_keys = {
                row["target_key"]
                for row in conn.execute(
                    "SELECT DISTINCT target_key FROM persona_contribution WHERE agent_id=? AND active=1",
                    (self.agent_id,),
                ).fetchall()
            }
            if active_keys - target_keys:
                raise ValueError("target Persona definition cannot replay all active contributions")
            conn.execute(
                "UPDATE persona_definition SET status='superseded' WHERE agent_id=? AND status='active'",
                (self.agent_id,),
            )
            conn.execute(
                "UPDATE persona_definition SET status='active', activated_at=? WHERE agent_id=? AND version=?",
                (utc_now(), self.agent_id, target_version),
            )
            conn.execute(
                "UPDATE persona_instance SET active_definition_version=?, updated_at=? WHERE agent_id=?",
                (target_version, utc_now(), self.agent_id),
            )
            snapshot = self._rebuild_snapshot(conn, reason=f"definition_rollback:{reason}")
            self._insert_audit(
                conn,
                actor=actor,
                action="definition_rolled_back",
                entity_type="definition",
                entity_id=f"{self.agent_id}:{target_version}",
                details={"previous_version": int(current["version"]), "reason": reason, "snapshot_id": snapshot["snapshot_id"]},
            )
            return {"definition_version": target_version, "snapshot": snapshot, "idempotent": False}

        return self._write(operation, priority=0, name="rollback_persona_definition")

    def trait_definitions(self, agent_id: str | None = None) -> list[JsonDict]:
        selected = agent_id or self.agent_id
        definition = self.definition(selected)
        rows = self._read_all(
            """
            SELECT * FROM persona_trait_definition
            WHERE agent_id=? AND definition_version=? ORDER BY trait_key
            """,
            (selected, definition["version"]),
        )
        return [
            {
                "key": row["trait_key"],
                "initial_value": row["initial_value"],
                "min_bound": row["min_bound"],
                "max_bound": row["max_bound"],
                "inertia": row["inertia"],
                "per_event_limit": row["per_event_limit"],
                "daily_limit": row["daily_limit"],
                "weekly_limit": row["weekly_limit"],
                "monthly_limit": row["monthly_limit"],
                "description": row["description"],
            }
            for row in rows
        ]

    def active_snapshot(self, agent_id: str | None = None) -> JsonDict:
        selected = agent_id or self.agent_id
        row = self._read_one(
            "SELECT * FROM persona_snapshot WHERE agent_id=? AND status='active'",
            (selected,),
        )
        if row is None:
            raise RuntimeError(f"active Persona snapshot not found for {selected}")
        return self._snapshot_from_row(row)

    def latest_frame(self, *, agent_id: str | None = None, user_id: str | None = None) -> JsonDict | None:
        selected = agent_id or self.agent_id
        if user_id is None:
            row = self._read_one(
                """
                SELECT * FROM persona_frame
                WHERE agent_id=? AND user_id IS NULL AND status='active'
                ORDER BY created_at DESC LIMIT 1
                """,
                (selected,),
            )
        else:
            row = self._read_one(
                """
                SELECT * FROM persona_frame
                WHERE agent_id=? AND user_id=? AND status='active'
                ORDER BY created_at DESC LIMIT 1
                """,
                (selected, user_id),
            )
        if row is None:
            return None
        return {
            **json_loads(row["frame_json"], {}),
            "frame_id": row["id"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "stale_until": row["stale_until"],
        }

    def save_frame(self, frame: Mapping[str, Any], *, user_id: str | None) -> JsonDict:
        now = utc_now()
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        stale_until = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
        frame_id = new_id("pframe")
        snapshot_id = str(frame["snapshot_id"])

        def operation(conn: sqlite3.Connection) -> None:
            if user_id is None:
                conn.execute(
                    "UPDATE persona_frame SET status='superseded' WHERE agent_id=? AND user_id IS NULL AND status='active'",
                    (self.agent_id,),
                )
            else:
                conn.execute(
                    "UPDATE persona_frame SET status='superseded' WHERE agent_id=? AND user_id=? AND status='active'",
                    (self.agent_id, user_id),
                )
            conn.execute(
                """
                INSERT INTO persona_frame(
                    id, agent_id, user_id, snapshot_id, schema_version, frame_json,
                    status, created_at, expires_at, stale_until
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    frame_id,
                    self.agent_id,
                    user_id,
                    snapshot_id,
                    PERSONA_FRAME_CONTRACT,
                    json_dumps(dict(frame)),
                    now,
                    expires_at,
                    stale_until,
                ),
            )

        self._write(operation, priority=2, name="save_persona_frame")
        return self.latest_frame(user_id=user_id) or dict(frame)

    def ingest_projection(self, event: Mapping[str, Any]) -> JsonDict:
        event_id = str(event.get("event_id") or "")
        if not event_id:
            raise ValueError("ProjectionEvent event_id is required")
        memory = dict(event.get("memory") or {})
        source_ref = str(memory.get("id") or (event.get("source") or {}).get("source_ref") or event_id)
        payload_hash = str((event.get("idempotency") or {}).get("payload_hash") or _hash_json(event))
        action = str(event.get("action") or "upsert")

        def operation(conn: sqlite3.Connection) -> JsonDict:
            existing = conn.execute("SELECT * FROM persona_inbox WHERE event_id=?", (event_id,)).fetchone()
            if existing is not None:
                return {
                    "status": "duplicate",
                    "candidate_id": None,
                    "source_ref": existing["source_ref"],
                    "payload_hash": existing["payload_hash"],
                }
            if self._is_tombstoned(conn, source_ref):
                inbox_status = "rejected_policy"
                conn.execute(
                    "INSERT INTO persona_inbox VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (event_id, payload_hash, source_ref, inbox_status, json_dumps(event), utc_now(), utc_now()),
                )
                return {"status": inbox_status, "candidate_id": None, "source_ref": source_ref, "payload_hash": payload_hash}
            if action == "rollback" or str(event.get("event_type")) == "memory_rollback":
                conn.execute(
                    "INSERT INTO persona_inbox VALUES (?, ?, ?, 'accepted', ?, ?, ?)",
                    (event_id, payload_hash, source_ref, json_dumps(event), utc_now(), utc_now()),
                )
                result = self._rollback_source(conn, source_ref, reason=f"projection:{action}")
                self._insert_audit(
                    conn,
                    action="projection_source_rolled_back",
                    entity_type="source_ref",
                    entity_id=source_ref,
                    details={"event_id": event_id, **result},
                    correlation_id=str(event.get("correlation_id") or "") or None,
                )
                return {
                    "status": "rolled_back",
                    "candidate_id": None,
                    "source_ref": source_ref,
                    "payload_hash": payload_hash,
                    **result,
                }
            if action in {"delete", "tombstone", "forget"} or str(event.get("event_type")) in {"memory_tombstone", "memory_delete"}:
                conn.execute(
                    "INSERT INTO persona_inbox VALUES (?, ?, ?, 'tombstoned', ?, ?, ?)",
                    (event_id, payload_hash, source_ref, json_dumps(event), utc_now(), utc_now()),
                )
                result = self._forget_source(conn, source_ref, reason=f"projection:{action}")
                return {"status": "tombstoned", "candidate_id": None, "source_ref": source_ref, "payload_hash": payload_hash, **result}
            event_key = str((event.get("idempotency") or {}).get("event_key") or event_id)
            idempotency_key = f"persona.ingest.v2:{event_key}"
            duplicate = conn.execute(
                "SELECT id FROM persona_candidate WHERE agent_id=? AND idempotency_key=? AND payload_hash=?",
                (self.agent_id, idempotency_key, payload_hash),
            ).fetchone()
            if duplicate is not None:
                conn.execute(
                    "INSERT INTO persona_inbox VALUES (?, ?, ?, 'duplicate', ?, ?, ?)",
                    (event_id, payload_hash, source_ref, json_dumps(event), utc_now(), utc_now()),
                )
                return {"status": "duplicate", "candidate_id": duplicate["id"], "source_ref": source_ref, "payload_hash": payload_hash}
            candidate_id = new_id("pcand")
            now = utc_now()
            conn.execute(
                "INSERT INTO persona_inbox VALUES (?, ?, ?, 'accepted', ?, ?, NULL)",
                (event_id, payload_hash, source_ref, json_dumps(event), now),
            )
            conn.execute(
                """
                INSERT INTO persona_candidate(
                    id, agent_id, user_id, source_kind, source_ref, idempotency_key,
                    payload_hash, status, source_payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'memory_projection', ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    candidate_id,
                    self.agent_id,
                    memory.get("owner_user_id"),
                    source_ref,
                    idempotency_key,
                    payload_hash,
                    json_dumps(event),
                    now,
                    now,
                ),
            )
            self._insert_audit(
                conn,
                action="projection_ingested",
                entity_type="candidate",
                entity_id=candidate_id,
                details={"event_id": event_id, "source_ref": source_ref},
                correlation_id=str(event.get("correlation_id") or "") or None,
            )
            return {"status": "accepted", "candidate_id": candidate_id, "source_ref": source_ref, "payload_hash": payload_hash}

        return self._write(operation, priority=1, name="ingest_projection")

    def claim_candidate(self) -> JsonDict | None:
        def operation(conn: sqlite3.Connection) -> JsonDict | None:
            row = conn.execute(
                "SELECT * FROM persona_candidate WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE persona_candidate SET status='evaluating', updated_at=? WHERE id=? AND status='queued'",
                (utc_now(), row["id"]),
            )
            return self._candidate_from_row(conn.execute("SELECT * FROM persona_candidate WHERE id=?", (row["id"],)).fetchone())

        return self._write(operation, priority=1, name="claim_candidate")

    def recover_interrupted_candidates(self) -> int:
        def operation(conn: sqlite3.Connection) -> int:
            recovered = conn.execute(
                "UPDATE persona_candidate SET status='queued', updated_at=? WHERE status='evaluating'",
                (utc_now(),),
            ).rowcount
            if recovered:
                self._insert_audit(
                    conn,
                    action="interrupted_candidates_requeued",
                    entity_type="worker_recovery",
                    entity_id=None,
                    details={"count": recovered},
                )
            return recovered

        return int(self._write(operation, priority=0, name="recover_interrupted_candidates"))

    def materialize_candidate(
        self,
        candidate_id: str,
        candidate: PersonaGrowthCandidate,
        *,
        status: str,
        gate_reason: str,
        gate_decision: str | None = None,
        model_trace: Mapping[str, Any],
    ) -> JsonDict:
        payload = candidate.to_dict()

        def operation(conn: sqlite3.Connection) -> JsonDict:
            source_row = conn.execute(
                "SELECT source_payload_json,payload_hash FROM persona_candidate WHERE id=?",
                (candidate_id,),
            ).fetchone()
            if source_row is None:
                raise ValueError(f"Persona candidate not found: {candidate_id}")
            source_payload = json_loads(source_row["source_payload_json"], {})
            memory = dict(source_payload.get("memory") or {})
            source_revision = str(
                memory.get("updated_at")
                or memory.get("created_at")
                or (source_payload.get("idempotency") or {}).get("payload_hash")
                or source_row["payload_hash"]
            )
            target_idempotency_key = (
                f"persona.v2:{candidate.agent_id}:{candidate.source_ref}:{source_revision}:"
                f"trait:{candidate.target_scope}:{candidate.target_key}:{PERSONA_POLICY_VERSION}"
            )
            duplicate = conn.execute(
                """
                SELECT id,status FROM persona_candidate
                WHERE agent_id=? AND idempotency_key=? AND id!=?
                ORDER BY created_at LIMIT 1
                """,
                (candidate.agent_id, target_idempotency_key, candidate_id),
            ).fetchone()
            if duplicate is not None:
                duplicate_payload = dict(payload)
                duplicate_payload["metadata"] = {
                    **dict(duplicate_payload.get("metadata") or {}),
                    "gate_decision": gate_decision or status,
                    "duplicate_of": duplicate["id"],
                }
                conn.execute(
                    """
                    UPDATE persona_candidate
                    SET user_id=?, target_scope=?, target_key=?, direction=?, status='rejected',
                        candidate_json=?, gate_reason='duplicate_target_candidate',
                        model_trace_json=?, updated_at=?, decided_at=?
                    WHERE id=?
                    """,
                    (
                        candidate.user_id,
                        candidate.target_scope,
                        candidate.target_key,
                        candidate.direction,
                        json_dumps(duplicate_payload),
                        json_dumps(model_trace),
                        utc_now(),
                        utc_now(),
                        candidate_id,
                    ),
                )
                self._insert_audit(
                    conn,
                    action="candidate_rejected",
                    entity_type="candidate",
                    entity_id=candidate_id,
                    details={"gate_reason": "duplicate_target_candidate", "duplicate_of": duplicate["id"]},
                )
                return self._candidate_from_row(
                    conn.execute("SELECT * FROM persona_candidate WHERE id=?", (candidate_id,)).fetchone()
                )
            payload["metadata"] = {
                **dict(payload.get("metadata") or {}),
                "gate_decision": gate_decision or status,
                "target_idempotency_key": target_idempotency_key,
            }
            conn.execute(
                """
                UPDATE persona_candidate
                SET user_id=?, target_scope=?, target_key=?, direction=?, status=?,
                    idempotency_key=?, candidate_json=?, gate_reason=?, model_trace_json=?, updated_at=?,
                    decided_at=CASE WHEN ? IN ('rejected','quarantined') THEN ? ELSE decided_at END
                WHERE id=?
                """,
                (
                    candidate.user_id,
                    candidate.target_scope,
                    candidate.target_key,
                    candidate.direction,
                    status,
                    target_idempotency_key,
                    json_dumps(payload),
                    gate_reason,
                    json_dumps(model_trace),
                    utc_now(),
                    status,
                    utc_now(),
                    candidate_id,
                ),
            )
            source_event_id = str((json_loads(conn.execute("SELECT source_payload_json FROM persona_candidate WHERE id=?", (candidate_id,)).fetchone()[0], {}) or {}).get("event_id") or "")
            if source_event_id:
                conn.execute("UPDATE persona_inbox SET processed_at=? WHERE event_id=?", (utc_now(), source_event_id))
            self._insert_audit(
                conn,
                action=f"candidate_{gate_decision or status}",
                entity_type="candidate",
                entity_id=candidate_id,
                details={
                    "gate_reason": gate_reason,
                    "gate_decision": gate_decision or status,
                    "support": candidate.support(),
                    "target_idempotency_key": target_idempotency_key,
                },
            )
            row = conn.execute("SELECT * FROM persona_candidate WHERE id=?", (candidate_id,)).fetchone()
            if row is None:
                raise ValueError(f"Persona candidate not found: {candidate_id}")
            return self._candidate_from_row(row)

        return self._write(operation, priority=1, name="materialize_candidate")

    def fail_candidate(
        self,
        candidate_id: str,
        *,
        error_type: str,
        error: str,
        model_trace: Mapping[str, Any] | None = None,
    ) -> JsonDict:
        def operation(conn: sqlite3.Connection) -> JsonDict:
            conn.execute(
                """
                UPDATE persona_candidate SET status='quarantined', error_type=?, error=?,
                    gate_reason='provider_failure', model_trace_json=?, updated_at=?, decided_at=?
                WHERE id=?
                """,
                (error_type, error[:2000], json_dumps(model_trace or {}), utc_now(), utc_now(), candidate_id),
            )
            source_event_id = str((json_loads(conn.execute("SELECT source_payload_json FROM persona_candidate WHERE id=?", (candidate_id,)).fetchone()[0], {}) or {}).get("event_id") or "")
            if source_event_id:
                conn.execute("UPDATE persona_inbox SET processed_at=? WHERE event_id=?", (utc_now(), source_event_id))
            self._insert_audit(
                conn,
                action="candidate_quarantined",
                entity_type="candidate",
                entity_id=candidate_id,
                details={"error_type": error_type, "error": error[:500]},
            )
            return self._candidate_from_row(conn.execute("SELECT * FROM persona_candidate WHERE id=?", (candidate_id,)).fetchone())

        return self._write(operation, priority=1, name="fail_candidate")

    def reject_empty_candidate(self, candidate_id: str, *, trace: Mapping[str, Any]) -> JsonDict:
        def operation(conn: sqlite3.Connection) -> JsonDict:
            conn.execute(
                """
                UPDATE persona_candidate SET status='rejected', gate_reason='provider_no_candidate',
                    model_trace_json=?, updated_at=?, decided_at=? WHERE id=?
                """,
                (json_dumps(trace), utc_now(), utc_now(), candidate_id),
            )
            source_event_id = str((json_loads(conn.execute("SELECT source_payload_json FROM persona_candidate WHERE id=?", (candidate_id,)).fetchone()[0], {}) or {}).get("event_id") or "")
            if source_event_id:
                conn.execute("UPDATE persona_inbox SET processed_at=? WHERE event_id=?", (utc_now(), source_event_id))
            self._insert_audit(
                conn,
                action="candidate_rejected",
                entity_type="candidate",
                entity_id=candidate_id,
                details={"gate_reason": "provider_no_candidate"},
            )
            return self._candidate_from_row(conn.execute("SELECT * FROM persona_candidate WHERE id=?", (candidate_id,)).fetchone())

        return self._write(operation, priority=1, name="reject_empty_candidate")

    def contribution_budget(self, *, target_scope: str, target_key: str, user_id: str | None) -> JsonDict:
        now = datetime.now(timezone.utc)
        windows = {"daily": 1, "weekly": 7, "monthly": 30}
        result: JsonDict = {}
        with self._lock:
            for name, days in windows.items():
                cutoff = (now - timedelta(days=days)).isoformat()
                if user_id is None:
                    value = self._conn.execute(
                        """
                        SELECT COALESCE(SUM(ABS(delta)),0) FROM persona_contribution
                        WHERE agent_id=? AND target_scope=? AND target_key=? AND user_id IS NULL
                          AND active=1 AND created_at>=?
                        """,
                        (self.agent_id, target_scope, target_key, cutoff),
                    ).fetchone()[0]
                else:
                    value = self._conn.execute(
                        """
                        SELECT COALESCE(SUM(ABS(delta)),0) FROM persona_contribution
                        WHERE agent_id=? AND target_scope=? AND target_key=? AND user_id=?
                          AND active=1 AND created_at>=?
                        """,
                        (self.agent_id, target_scope, target_key, user_id, cutoff),
                    ).fetchone()[0]
                result[name] = float(value or 0.0)
        return result

    def commit_candidate(self, candidate_id: str, *, delta: float, support: float, actor: str) -> JsonDict:
        def operation(conn: sqlite3.Connection) -> JsonDict:
            row = conn.execute("SELECT * FROM persona_candidate WHERE id=?", (candidate_id,)).fetchone()
            if row is None:
                raise ValueError(f"Persona candidate not found: {candidate_id}")
            if row["status"] == "committed":
                existing = conn.execute("SELECT * FROM persona_contribution WHERE candidate_id=?", (candidate_id,)).fetchone()
                return {"candidate": self._candidate_from_row(row), "contribution": self._contribution_from_row(existing), "snapshot": self._active_snapshot_conn(conn)}
            if row["status"] != "approved":
                raise ValueError(f"Persona candidate {candidate_id} cannot be committed from {row['status']}")
            candidate = PersonaGrowthCandidate.from_dict(json_loads(row["candidate_json"], {}))
            contribution_id = new_id("pcontrib")
            now = utc_now()
            conn.execute(
                """
                INSERT INTO persona_contribution(
                    id, candidate_id, agent_id, user_id, target_scope, target_key,
                    delta, support, source_ref, evidence_refs_json, policy_version,
                    active, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    contribution_id,
                    candidate_id,
                    candidate.agent_id,
                    candidate.user_id,
                    candidate.target_scope,
                    candidate.target_key,
                    delta,
                    support,
                    candidate.source_ref,
                    json_dumps(list(candidate.evidence_refs)),
                    PERSONA_POLICY_VERSION,
                    now,
                ),
            )
            conn.execute(
                "UPDATE persona_candidate SET status='committed', decided_at=?, updated_at=? WHERE id=?",
                (now, now, candidate_id),
            )
            if candidate.target_scope == "agent":
                snapshot = self._rebuild_snapshot(conn, reason="candidate_commit")
            else:
                self._rebuild_user_adaptation(conn, candidate.user_id or "", candidate.target_key)
                snapshot = self._active_snapshot_conn(conn)
            self._insert_outbox(
                conn,
                event_type="persona_contribution_committed",
                aggregate_ref=contribution_id,
                payload={"candidate_id": candidate_id, "contribution_id": contribution_id, "snapshot_id": snapshot["snapshot_id"]},
            )
            self._insert_audit(
                conn,
                actor=actor,
                action="candidate_committed",
                entity_type="contribution",
                entity_id=contribution_id,
                details={"candidate_id": candidate_id, "delta": delta, "support": support, "target_scope": candidate.target_scope},
            )
            contribution = conn.execute("SELECT * FROM persona_contribution WHERE id=?", (contribution_id,)).fetchone()
            return {"candidate": self._candidate_from_row(conn.execute("SELECT * FROM persona_candidate WHERE id=?", (candidate_id,)).fetchone()), "contribution": self._contribution_from_row(contribution), "snapshot": snapshot}

        return self._write(operation, priority=1, name="commit_candidate")

    def set_candidate_status(self, candidate_id: str, status: str, *, reason: str, actor: str) -> JsonDict:
        if status not in {"approved", "rejected", "pending_review"}:
            raise ValueError(f"unsupported Persona candidate status: {status}")

        def operation(conn: sqlite3.Connection) -> JsonDict:
            row = conn.execute("SELECT * FROM persona_candidate WHERE id=?", (candidate_id,)).fetchone()
            if row is None:
                raise ValueError(f"Persona candidate not found: {candidate_id}")
            if row["status"] == "committed":
                raise ValueError("committed Persona candidate must be rolled back, not relabeled")
            if status == "approved" and row["status"] != "pending_review":
                raise ValueError(
                    f"Persona candidate {candidate_id} requires pending_review before approval; current={row['status']}"
                )
            if row["status"] in {"forgotten", "rejected", "quarantined"} and status != "rejected":
                raise ValueError(f"Persona candidate {candidate_id} is terminal: {row['status']}")
            conn.execute(
                "UPDATE persona_candidate SET status=?, gate_reason=?, updated_at=?, decided_at=? WHERE id=?",
                (status, reason, utc_now(), utc_now() if status in {"approved", "rejected"} else None, candidate_id),
            )
            self._insert_audit(conn, actor=actor, action=f"candidate_{status}", entity_type="candidate", entity_id=candidate_id, details={"reason": reason})
            return self._candidate_from_row(conn.execute("SELECT * FROM persona_candidate WHERE id=?", (candidate_id,)).fetchone())

        return self._write(operation, priority=1, name=f"candidate_{status}")

    def rollback_contribution(self, contribution_id: str, *, reason: str, actor: str) -> JsonDict:
        def operation(conn: sqlite3.Connection) -> JsonDict:
            row = conn.execute("SELECT * FROM persona_contribution WHERE id=?", (contribution_id,)).fetchone()
            if row is None:
                raise ValueError(f"Persona contribution not found: {contribution_id}")
            if not row["active"]:
                return {"contribution": self._contribution_from_row(row), "snapshot": self._active_snapshot_conn(conn), "idempotent": True}
            conn.execute(
                "UPDATE persona_contribution SET active=0, deactivated_at=?, deactivation_reason=? WHERE id=?",
                (utc_now(), reason, contribution_id),
            )
            if row["target_scope"] == "agent":
                snapshot = self._rebuild_snapshot(conn, reason="rollback")
            else:
                self._rebuild_user_adaptation(conn, row["user_id"] or "", row["target_key"])
                snapshot = self._active_snapshot_conn(conn)
            self._insert_audit(conn, actor=actor, action="contribution_rolled_back", entity_type="contribution", entity_id=contribution_id, details={"reason": reason})
            return {"contribution": self._contribution_from_row(conn.execute("SELECT * FROM persona_contribution WHERE id=?", (contribution_id,)).fetchone()), "snapshot": snapshot, "idempotent": False}

        return self._write(operation, priority=0, name="rollback_contribution")

    def forget_source(self, source_ref: str, *, reason: str, actor: str = "user") -> JsonDict:
        def operation(conn: sqlite3.Connection) -> JsonDict:
            result = self._forget_source(conn, source_ref, reason=reason)
            self._insert_audit(conn, actor=actor, action="source_forgotten", entity_type="source_ref_hash", entity_id=_hash_text(source_ref), details={"reason": reason, **result})
            return result

        return self._write(operation, priority=0, name="forget_source")

    def rollback_source(self, source_ref: str, *, reason: str, actor: str = "system") -> JsonDict:
        def operation(conn: sqlite3.Connection) -> JsonDict:
            result = self._rollback_source(conn, source_ref, reason=reason)
            self._insert_audit(
                conn,
                actor=actor,
                action="source_rolled_back",
                entity_type="source_ref",
                entity_id=source_ref,
                details={"reason": reason, **result},
            )
            return result

        return self._write(operation, priority=0, name="rollback_source")

    def rebuild(self, *, reason: str, actor: str = "system") -> JsonDict:
        def operation(conn: sqlite3.Connection) -> JsonDict:
            before = self._active_snapshot_conn(conn)
            run_id = new_id("prebuild")
            created_at = utc_now()
            conn.execute(
                "INSERT INTO persona_rebuild_run VALUES (?, ?, 'running', ?, ?, NULL, NULL, '{}', ?, NULL)",
                (run_id, self.agent_id, reason, before["checksum"], created_at),
            )
            after = self._rebuild_snapshot(conn, reason=reason)
            independent_values = self._calculate_agent_values(conn)
            parity_ok = _hash_json(independent_values) == after["checksum"]
            details = {"independent_values": independent_values, "snapshot_values": after["trait_values"]}
            conn.execute(
                """
                UPDATE persona_rebuild_run SET status=?, after_checksum=?, parity_ok=?,
                    details_json=?, completed_at=? WHERE id=?
                """,
                ("completed" if parity_ok else "failed_parity", after["checksum"], int(parity_ok), json_dumps(details), utc_now(), run_id),
            )
            self._insert_audit(conn, actor=actor, action="rebuild_completed", entity_type="rebuild_run", entity_id=run_id, details={"parity_ok": parity_ok, "reason": reason})
            return {"run_id": run_id, "status": "completed" if parity_ok else "failed_parity", "parity_ok": parity_ok, "before": before, "after": after}

        return self._write(operation, priority=0, name="rebuild_persona")

    def get_control(self, *, user_id: str | None = None) -> JsonDict:
        row = self._read_one(
            "SELECT * FROM persona_control WHERE agent_id=? AND user_id=?",
            (self.agent_id, user_id or ""),
        )
        if row is None:
            return {"agent_id": self.agent_id, "user_id": user_id, "growth_paused": False, "self_reference_permission": "implicit"}
        return {
            "agent_id": row["agent_id"],
            "user_id": row["user_id"] or None,
            "growth_paused": bool(row["growth_paused"]),
            "self_reference_permission": row["self_reference_permission"],
            "updated_at": row["updated_at"],
        }

    def set_control(
        self,
        *,
        user_id: str | None = None,
        growth_paused: bool | None = None,
        self_reference_permission: str | None = None,
        actor: str = "user",
    ) -> JsonDict:
        if self_reference_permission not in {None, "silent", "implicit", "explicit"}:
            raise ValueError("invalid Persona self-reference permission")

        def operation(conn: sqlite3.Connection) -> JsonDict:
            current = self.get_control(user_id=user_id)
            paused = current["growth_paused"] if growth_paused is None else bool(growth_paused)
            permission = current["self_reference_permission"] if self_reference_permission is None else self_reference_permission
            conn.execute(
                """
                INSERT INTO persona_control(agent_id,user_id,growth_paused,self_reference_permission,updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(agent_id,user_id) DO UPDATE SET
                    growth_paused=excluded.growth_paused,
                    self_reference_permission=excluded.self_reference_permission,
                    updated_at=excluded.updated_at
                """,
                (self.agent_id, user_id or "", int(paused), permission, utc_now()),
            )
            self._insert_audit(conn, actor=actor, action="control_updated", entity_type="control", entity_id=user_id or "global", details={"growth_paused": paused, "self_reference_permission": permission})
            return {"agent_id": self.agent_id, "user_id": user_id, "growth_paused": paused, "self_reference_permission": permission}

        return self._write(operation, priority=0, name="set_persona_control")

    def list_candidates(self, *, status: str | None = None, limit: int = 50) -> list[JsonDict]:
        if status:
            rows = self._read_all(
                "SELECT * FROM persona_candidate WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            rows = self._read_all("SELECT * FROM persona_candidate ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._candidate_from_row(row) for row in rows]

    def get_candidate(self, candidate_id: str) -> JsonDict:
        row = self._read_one("SELECT * FROM persona_candidate WHERE id=?", (candidate_id,))
        if row is None:
            raise ValueError(f"Persona candidate not found: {candidate_id}")
        return self._candidate_from_row(row)

    def list_contributions(self, *, active: bool | None = None, limit: int = 100) -> list[JsonDict]:
        if active is None:
            rows = self._read_all("SELECT * FROM persona_contribution ORDER BY created_at DESC LIMIT ?", (limit,))
        else:
            rows = self._read_all(
                "SELECT * FROM persona_contribution WHERE active=? ORDER BY created_at DESC LIMIT ?",
                (int(active), limit),
            )
        return [self._contribution_from_row(row) for row in rows]

    def user_adaptations(self, user_id: str) -> JsonDict:
        rows = self._read_all(
            "SELECT * FROM persona_user_adaptation WHERE agent_id=? AND user_id=? ORDER BY trait_key",
            (self.agent_id, user_id),
        )
        return {
            row["trait_key"]: {
                "value": row["value"],
                "version": row["version"],
                "evidence_count": row["evidence_count"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        }

    def upsert_interest_state(
        self,
        topic_key: str,
        *,
        sora_interest: float,
        shared_density: float = 0.0,
        user_interest_ref: str | None = None,
        reason_refs: Iterable[str] = (),
        attitude: str = "curious",
        user_id: str | None = None,
        actor: str = "system",
    ) -> JsonDict:
        topic = topic_key.strip()
        refs = sorted({str(item) for item in reason_refs if str(item)})
        if not topic:
            raise ValueError("Persona interest topic_key is required")
        if not 0.0 <= sora_interest <= 1.0 or not 0.0 <= shared_density <= 1.0:
            raise ValueError("Persona interest scores must be in [0, 1]")
        if attitude not in {"curious", "engaged", "neutral", "cautious", "avoided"}:
            raise ValueError("unsupported Persona interest attitude")
        if not refs:
            raise ValueError("Persona self-interest requires reason refs")

        def operation(conn: sqlite3.Connection) -> JsonDict:
            current = conn.execute(
                "SELECT version FROM persona_interest_state WHERE agent_id=? AND user_id=? AND topic_key=?",
                (self.agent_id, user_id or "", topic),
            ).fetchone()
            version = int(current["version"] if current else 0) + 1
            now = utc_now()
            conn.execute(
                """
                INSERT INTO persona_interest_state(
                    agent_id,user_id,topic_key,user_interest_ref,shared_density,
                    sora_interest,reason_refs_json,attitude,version,updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id,user_id,topic_key) DO UPDATE SET
                    user_interest_ref=excluded.user_interest_ref,
                    shared_density=excluded.shared_density,
                    sora_interest=excluded.sora_interest,
                    reason_refs_json=excluded.reason_refs_json,
                    attitude=excluded.attitude,
                    version=excluded.version,
                    updated_at=excluded.updated_at
                """,
                (
                    self.agent_id,
                    user_id or "",
                    topic,
                    user_interest_ref,
                    normalize_persona_value(shared_density),
                    normalize_persona_value(sora_interest),
                    json_dumps(refs),
                    attitude,
                    version,
                    now,
                ),
            )
            if user_id is None:
                conn.execute(
                    "UPDATE persona_frame SET status='superseded' WHERE agent_id=? AND status='active'",
                    (self.agent_id,),
                )
            else:
                conn.execute(
                    """
                    UPDATE persona_frame SET status='superseded'
                    WHERE agent_id=? AND user_id=? AND status='active'
                    """,
                    (self.agent_id, user_id),
                )
            self._insert_outbox(
                conn,
                event_type="persona_interest_updated",
                aggregate_ref=f"{user_id or 'global'}:{topic}",
                payload={"topic_key": topic, "user_id": user_id, "version": version},
            )
            self._insert_audit(
                conn,
                actor=actor,
                action="interest_state_updated",
                entity_type="interest_state",
                entity_id=f"{user_id or 'global'}:{topic}",
                details={"version": version, "reason_ref_count": len(refs)},
            )
            return self._interest_from_row(
                conn.execute(
                    "SELECT * FROM persona_interest_state WHERE agent_id=? AND user_id=? AND topic_key=?",
                    (self.agent_id, user_id or "", topic),
                ).fetchone()
            )

        return self._write(operation, priority=2, name="upsert_persona_interest")

    def list_interest_states(self, *, user_id: str | None = None, limit: int = 20) -> list[JsonDict]:
        if user_id:
            rows = self._read_all(
                """
                SELECT * FROM persona_interest_state
                WHERE agent_id=? AND user_id IN ('', ?)
                ORDER BY sora_interest DESC, shared_density DESC, topic_key
                LIMIT ?
                """,
                (self.agent_id, user_id, limit),
            )
        else:
            rows = self._read_all(
                """
                SELECT * FROM persona_interest_state
                WHERE agent_id=? AND user_id=''
                ORDER BY sora_interest DESC, shared_density DESC, topic_key
                LIMIT ?
                """,
                (self.agent_id, limit),
            )
        return [self._interest_from_row(row) for row in rows]

    def set_ephemeral_state(
        self,
        state_key: str,
        value: Any,
        *,
        ttl_seconds: int,
        user_id: str | None = None,
        source_ref: str | None = None,
        confidence: float = 1.0,
        actor: str = "system",
    ) -> JsonDict:
        if state_key not in {"mood", "energy", "focus", "silence_preference"}:
            raise ValueError("unsupported Persona ephemeral state key")
        if not 30 <= ttl_seconds <= 86400:
            raise ValueError("Persona ephemeral state TTL must be between 30 and 86400 seconds")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("Persona ephemeral confidence must be in [0, 1]")

        def operation(conn: sqlite3.Connection) -> JsonDict:
            now_dt = datetime.now(timezone.utc)
            now = now_dt.isoformat()
            expires_at = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
            conn.execute(
                """
                INSERT INTO persona_ephemeral_state(
                    agent_id,user_id,state_key,value_json,source_ref,confidence,
                    expires_at,created_at,updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id,user_id,state_key) DO UPDATE SET
                    value_json=excluded.value_json,
                    source_ref=excluded.source_ref,
                    confidence=excluded.confidence,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
                """,
                (
                    self.agent_id,
                    user_id or "",
                    state_key,
                    json_dumps(value),
                    source_ref,
                    normalize_persona_value(confidence),
                    expires_at,
                    now,
                    now,
                ),
            )
            if user_id is None:
                conn.execute(
                    "UPDATE persona_frame SET status='superseded' WHERE agent_id=? AND status='active'",
                    (self.agent_id,),
                )
            else:
                conn.execute(
                    """
                    UPDATE persona_frame SET status='superseded'
                    WHERE agent_id=? AND user_id=? AND status='active'
                    """,
                    (self.agent_id, user_id),
                )
            self._insert_audit(
                conn,
                actor=actor,
                action="ephemeral_state_updated",
                entity_type="ephemeral_state",
                entity_id=f"{user_id or 'global'}:{state_key}",
                details={"ttl_seconds": ttl_seconds, "source_ref": source_ref},
            )
            return self._ephemeral_from_row(
                conn.execute(
                    "SELECT * FROM persona_ephemeral_state WHERE agent_id=? AND user_id=? AND state_key=?",
                    (self.agent_id, user_id or "", state_key),
                ).fetchone()
            )

        return self._write(operation, priority=2, name="set_persona_ephemeral_state")

    def active_ephemeral_state(self, *, user_id: str | None = None) -> JsonDict:
        now = utc_now()
        if user_id:
            rows = self._read_all(
                """
                SELECT * FROM persona_ephemeral_state
                WHERE agent_id=? AND user_id IN ('', ?) AND expires_at>?
                ORDER BY CASE WHEN user_id=? THEN 0 ELSE 1 END, updated_at DESC
                """,
                (self.agent_id, user_id, now, user_id),
            )
        else:
            rows = self._read_all(
                """
                SELECT * FROM persona_ephemeral_state
                WHERE agent_id=? AND user_id='' AND expires_at>?
                ORDER BY updated_at DESC
                """,
                (self.agent_id, now),
            )
        selected: JsonDict = {}
        for row in rows:
            selected.setdefault(row["state_key"], self._ephemeral_from_row(row))
        return selected

    def self_reference_readiness(self) -> JsonDict:
        reflection = self.active_reflection()
        if reflection is None:
            return {"ready": False, "reason": "reflection_absent", "reflection_id": None}
        refs = {str(item) for item in reflection.get("evidence_refs") or [] if item}
        if not refs:
            return {"ready": False, "reason": "reflection_has_no_evidence", "reflection_id": reflection["id"]}
        rows = self._read_all(
            "SELECT evidence_refs_json FROM persona_contribution WHERE agent_id=? AND active=1",
            (self.agent_id,),
        )
        active_refs = {
            str(ref)
            for row in rows
            for ref in json_loads(row["evidence_refs_json"], [])
            if ref
        }
        ready = refs <= active_refs
        return {
            "ready": ready,
            "reason": "evidence_active" if ready else "reflection_evidence_inactive",
            "reflection_id": reflection["id"],
            "evidence_ref_count": len(refs),
        }

    def save_reflection(
        self,
        *,
        summary: str,
        facts: list[JsonDict],
        evidence_refs: list[str],
        model_trace: Mapping[str, Any],
        reflection_type: str,
    ) -> JsonDict:
        reflection_id = new_id("preflect")
        snapshot = self.active_snapshot()

        def operation(conn: sqlite3.Connection) -> JsonDict:
            conn.execute("UPDATE persona_reflection SET status='superseded' WHERE agent_id=? AND status='active'", (self.agent_id,))
            conn.execute(
                """
                INSERT INTO persona_reflection(
                    id,agent_id,snapshot_id,reflection_type,summary,facts_json,
                    evidence_refs_json,status,model_trace_json,created_at
                ) VALUES (?,?,?,?,?,?,?,'active',?,?)
                """,
                (reflection_id, self.agent_id, snapshot["snapshot_id"], reflection_type, summary, json_dumps(facts), json_dumps(evidence_refs), json_dumps(model_trace), utc_now()),
            )
            return {"id": reflection_id, "snapshot_id": snapshot["snapshot_id"], "reflection_type": reflection_type, "summary": summary, "facts": facts, "evidence_refs": evidence_refs}

        return self._write(operation, priority=3, name="save_persona_reflection")

    def active_reflection(self) -> JsonDict | None:
        row = self._read_one(
            "SELECT * FROM persona_reflection WHERE agent_id=? AND status='active' ORDER BY created_at DESC LIMIT 1",
            (self.agent_id,),
        )
        if row is None:
            return None
        return {
            "id": row["id"],
            "snapshot_id": row["snapshot_id"],
            "reflection_type": row["reflection_type"],
            "summary": row["summary"],
            "facts": json_loads(row["facts_json"], []),
            "evidence_refs": json_loads(row["evidence_refs_json"], []),
            "created_at": row["created_at"],
        }

    def list_audit(self, *, action: str | None = None, limit: int = 100) -> list[JsonDict]:
        if action:
            rows = self._read_all("SELECT * FROM persona_audit WHERE action=? ORDER BY created_at DESC LIMIT ?", (action, limit))
        else:
            rows = self._read_all("SELECT * FROM persona_audit ORDER BY created_at DESC LIMIT ?", (limit,))
        return [
            {
                "id": row["id"],
                "correlation_id": row["correlation_id"],
                "agent_id": row["agent_id"],
                "actor": row["actor"],
                "action": row["action"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "details": json_loads(row["details_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_outbox(self, *, status: str | None = None, limit: int = 100) -> list[JsonDict]:
        if status:
            rows = self._read_all(
                "SELECT * FROM persona_outbox WHERE status=? ORDER BY created_at LIMIT ?",
                (status, limit),
            )
        else:
            rows = self._read_all("SELECT * FROM persona_outbox ORDER BY created_at LIMIT ?", (limit,))
        return [
            {
                "event_id": row["id"],
                "event_type": row["event_type"],
                "aggregate_ref": row["aggregate_ref"],
                "payload_hash": row["payload_hash"],
                "status": row["status"],
                "attempts": row["attempts"],
                "payload": json_loads(row["payload_json"], {}),
                "created_at": row["created_at"],
                "delivered_at": row["delivered_at"],
                "last_error": row["last_error"],
            }
            for row in rows
        ]

    def acknowledge_outbox(
        self,
        event_id: str,
        *,
        delivered: bool,
        error: str | None = None,
        terminal: bool = False,
    ) -> JsonDict:
        def operation(conn: sqlite3.Connection) -> JsonDict:
            row = conn.execute("SELECT * FROM persona_outbox WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise ValueError(f"Persona outbox event not found: {event_id}")
            if row["status"] == "delivered":
                return {"event_id": event_id, "status": "delivered", "idempotent": True}
            status = "delivered" if delivered else ("dead_letter" if terminal else "pending")
            conn.execute(
                """
                UPDATE persona_outbox SET status=?, attempts=attempts+1,
                    delivered_at=?, last_error=? WHERE id=?
                """,
                (status, utc_now() if delivered else None, error, event_id),
            )
            self._insert_audit(
                conn,
                action=f"outbox_{status}",
                entity_type="outbox_event",
                entity_id=event_id,
                details={"error": error, "terminal": terminal},
            )
            return {"event_id": event_id, "status": status, "idempotent": False}

        return self._write(operation, priority=2, name="acknowledge_persona_outbox")

    def resolve_dead_letter(
        self,
        event_id: str,
        *,
        resolution: str,
        actor: str,
        export_ref: str | None = None,
    ) -> JsonDict:
        if resolution not in {"retry", "resolved", "exported"}:
            raise ValueError("unsupported Persona dead-letter resolution")
        if resolution == "exported" and not export_ref:
            raise ValueError("exported dead-letter resolution requires export_ref")

        def operation(conn: sqlite3.Connection) -> JsonDict:
            row = conn.execute("SELECT * FROM persona_outbox WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise ValueError(f"Persona outbox event not found: {event_id}")
            if row["status"] != "dead_letter":
                raise ValueError(f"Persona outbox event is not dead-lettered: {event_id}")
            now = utc_now()
            conn.execute(
                """
                INSERT INTO persona_dead_letter_resolution(event_id,resolution,export_ref,actor,resolved_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    resolution=excluded.resolution,
                    export_ref=excluded.export_ref,
                    actor=excluded.actor,
                    resolved_at=excluded.resolved_at
                """,
                (event_id, resolution, export_ref, actor, now),
            )
            if resolution == "retry":
                conn.execute(
                    "UPDATE persona_outbox SET status='pending', last_error=NULL WHERE id=?",
                    (event_id,),
                )
            self._insert_audit(
                conn,
                actor=actor,
                action="dead_letter_resolved",
                entity_type="outbox_event",
                entity_id=event_id,
                details={"resolution": resolution, "export_ref": export_ref},
            )
            return {
                "event_id": event_id,
                "status": "pending" if resolution == "retry" else "dead_letter",
                "resolution": resolution,
                "export_ref": export_ref,
                "resolved_at": now,
            }

        return self._write(operation, priority=0, name="resolve_persona_dead_letter")

    def record_metric(self, metric_name: str, value: float, *, labels: Mapping[str, Any] | None = None) -> None:
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO persona_metric VALUES (?, ?, ?, ?, ?)",
                (new_id("pmetric"), metric_name, value, json_dumps(labels or {}), utc_now()),
            )

        self._write(operation, priority=3, name="record_persona_metric")

    def record_audit(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str | None,
        details: Mapping[str, Any],
        actor: str = "system",
        correlation_id: str | None = None,
        priority: int = 2,
    ) -> None:
        def operation(conn: sqlite3.Connection) -> None:
            self._insert_audit(
                conn,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                details=details,
                actor=actor,
                correlation_id=correlation_id,
            )

        self._write(operation, priority=priority, name=f"audit_{action}")

    def cleanup(self, *, now: datetime | None = None, batch_size: int = 500) -> JsonDict:
        if not 1 <= batch_size <= 500:
            raise ValueError("Persona cleanup batch_size must be in [1, 500]")
        selected = now or datetime.now(timezone.utc)
        delivered_cutoff = (selected - timedelta(days=7)).isoformat()
        dead_letter_cutoff = (selected - timedelta(days=90)).isoformat()
        metric_cutoff = (selected - timedelta(days=30)).isoformat()

        def operation(conn: sqlite3.Connection) -> JsonDict:
            delivered_ids = [
                row["id"]
                for row in conn.execute(
                    """
                    SELECT id FROM persona_outbox
                    WHERE status='delivered' AND delivered_at<?
                    ORDER BY delivered_at LIMIT ?
                    """,
                    (delivered_cutoff, batch_size),
                ).fetchall()
            ]
            metric_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM persona_metric WHERE created_at<? ORDER BY created_at LIMIT ?",
                    (metric_cutoff, batch_size),
                ).fetchall()
            ]
            ephemeral_keys = [
                (row["agent_id"], row["user_id"], row["state_key"])
                for row in conn.execute(
                    """
                    SELECT agent_id,user_id,state_key FROM persona_ephemeral_state
                    WHERE expires_at<=? ORDER BY expires_at LIMIT ?
                    """,
                    (selected.isoformat(), batch_size),
                ).fetchall()
            ]
            resolved_dead_letters = [
                row["id"]
                for row in conn.execute(
                    """
                    SELECT o.id FROM persona_outbox o
                    JOIN persona_dead_letter_resolution r ON r.event_id=o.id
                    WHERE o.status='dead_letter' AND o.created_at<?
                      AND r.resolution IN ('resolved','exported')
                    ORDER BY o.created_at LIMIT ?
                    """,
                    (dead_letter_cutoff, batch_size),
                ).fetchall()
            ]
            unresolved_over_retention = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM persona_outbox o
                    LEFT JOIN persona_dead_letter_resolution r ON r.event_id=o.id
                    WHERE o.status='dead_letter' AND o.created_at<? AND r.event_id IS NULL
                    """,
                    (dead_letter_cutoff,),
                ).fetchone()[0]
            )
            _delete_ids(conn, "persona_outbox", delivered_ids)
            _delete_ids(conn, "persona_metric", metric_ids)
            _delete_ids(conn, "persona_outbox", resolved_dead_letters)
            for agent_id, user_id, state_key in ephemeral_keys:
                conn.execute(
                    "DELETE FROM persona_ephemeral_state WHERE agent_id=? AND user_id=? AND state_key=?",
                    (agent_id, user_id, state_key),
                )
            return {
                "batch_size": batch_size,
                "delivered_outbox_deleted": len(delivered_ids),
                "resolved_dead_letters_deleted": len(resolved_dead_letters),
                "unresolved_dead_letters_over_retention": unresolved_over_retention,
                "expired_ephemeral_states_deleted": len(ephemeral_keys),
                "metrics_deleted": len(metric_ids),
                "dead_letter_retention_days": 90,
            }

        return self._write(operation, priority=3, name="cleanup_persona")

    def table_counts(self) -> JsonDict:
        tables = (
            "persona_instance",
            "persona_candidate",
            "persona_contribution",
            "persona_snapshot",
            "persona_user_adaptation",
            "persona_interest_state",
            "persona_ephemeral_state",
            "persona_reflection",
            "persona_outbox",
            "persona_dead_letter_resolution",
            "persona_tombstone",
            "persona_audit",
        )
        with self._lock:
            return {table: int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}

    def pragma_health(self) -> JsonDict:
        with self._lock:
            integrity = str(self._conn.execute("PRAGMA quick_check").fetchone()[0])
            journal = str(self._conn.execute("PRAGMA journal_mode").fetchone()[0])
            foreign_keys = int(self._conn.execute("PRAGMA foreign_keys").fetchone()[0])
            schema_version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        return {
            "integrity": integrity,
            "journal_mode": journal,
            "foreign_keys": bool(foreign_keys),
            "schema_version": schema_version,
            "queue_depth": self.queue_depth,
            "backpressure": self.under_backpressure,
        }

    def _seed_if_needed(self, conn: sqlite3.Connection) -> None:
        existing = conn.execute(
            "SELECT 1 FROM persona_definition WHERE agent_id=? AND status='active'",
            (self.agent_id,),
        ).fetchone()
        if existing is not None:
            active_version = conn.execute(
                "SELECT version FROM persona_definition WHERE agent_id=? AND status='active'",
                (self.agent_id,),
            ).fetchone()[0]
            now = utc_now()
            conn.execute(
                """
                INSERT OR IGNORE INTO persona_instance(
                    agent_id,status,active_definition_version,created_at,updated_at
                ) VALUES (?, 'active', ?, ?, ?)
                """,
                (self.agent_id, active_version, now, now),
            )
            return
        now = utc_now()
        definition_version = 1
        conn.execute(
            "INSERT INTO persona_definition VALUES (?, ?, ?, 'active', ?, ?, ?)",
            (
                self.agent_id,
                definition_version,
                str(self._definition_seed.get("schema_version") or "PersonaDefinition.v1"),
                json_dumps(self._definition_seed),
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO persona_instance(agent_id,status,active_definition_version,created_at,updated_at) VALUES (?, 'active', ?, ?, ?)",
            (self.agent_id, definition_version, now, now),
        )
        for item in self._trait_seed:
            conn.execute(
                """
                INSERT INTO persona_trait_definition VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.agent_id,
                    definition_version,
                    item.key,
                    item.initial_value,
                    item.min_bound,
                    item.max_bound,
                    item.inertia,
                    item.per_event_limit,
                    item.daily_limit,
                    item.weekly_limit,
                    item.monthly_limit,
                    item.description,
                ),
            )
        values = {item.key: item.initial_value for item in self._trait_seed}
        self._insert_snapshot(conn, values, definition_version=definition_version, generation="seed", reason="initial_seed")
        conn.execute(
            "INSERT INTO persona_control(agent_id,user_id,growth_paused,self_reference_permission,updated_at) VALUES (?, '', 0, 'implicit', ?)",
            (self.agent_id, now),
        )
        self._insert_audit(conn, action="persona_seeded", entity_type="definition", entity_id=f"{self.agent_id}:1", details={"taxonomy_version": self._definition_seed.get("taxonomy_version")})

    def _write(self, operation: Callable[[sqlite3.Connection], Any], *, priority: int, name: str) -> Any:
        def transaction() -> Any:
            with self._lock:
                for attempt in range(4):
                    try:
                        self._conn.execute("BEGIN IMMEDIATE")
                        result = operation(self._conn)
                        self._conn.commit()
                        return result
                    except sqlite3.OperationalError as exc:
                        self._conn.rollback()
                        locked = "locked" in str(exc).lower() or "busy" in str(exc).lower()
                        if not locked or attempt == 3:
                            raise
                        time.sleep(0.01 * (2**attempt))
                    except BaseException:
                        self._conn.rollback()
                        raise
                raise RuntimeError(f"Persona write retry exhausted: {name}")

        return self._coordinator.submit(transaction, priority=priority, name=name)

    def _read_one(self, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _read_all(self, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _active_snapshot_conn(self, conn: sqlite3.Connection) -> JsonDict:
        row = conn.execute(
            "SELECT * FROM persona_snapshot WHERE agent_id=? AND status='active'",
            (self.agent_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("active Persona snapshot missing")
        return self._snapshot_from_row(row)

    def _calculate_agent_values(self, conn: sqlite3.Connection) -> JsonDict:
        definitions = conn.execute(
            """
            SELECT t.* FROM persona_trait_definition t
            JOIN persona_definition d ON d.agent_id=t.agent_id AND d.version=t.definition_version
            WHERE t.agent_id=? AND d.status='active' ORDER BY t.trait_key
            """,
            (self.agent_id,),
        ).fetchall()
        values = {row["trait_key"]: Decimal(str(row["initial_value"])) for row in definitions}
        bounds = {
            row["trait_key"]: (Decimal(str(row["min_bound"])), Decimal(str(row["max_bound"])))
            for row in definitions
        }
        rows = conn.execute(
            """
            SELECT target_key, delta FROM persona_contribution
            WHERE agent_id=? AND target_scope='agent' AND active=1
            ORDER BY created_at, id
            """,
            (self.agent_id,),
        ).fetchall()
        for row in rows:
            key = row["target_key"]
            if key not in values:
                continue
            low, high = bounds[key]
            values[key] = max(low, min(high, values[key] + Decimal(str(row["delta"]))))
        return {key: normalize_persona_value(value) for key, value in sorted(values.items())}

    def _rebuild_snapshot(self, conn: sqlite3.Connection, *, reason: str) -> JsonDict:
        values = self._calculate_agent_values(conn)
        definition = conn.execute(
            "SELECT version FROM persona_definition WHERE agent_id=? AND status='active'",
            (self.agent_id,),
        ).fetchone()
        return self._insert_snapshot(
            conn,
            values,
            definition_version=int(definition["version"]),
            generation=f"rebuild:{reason}",
            reason=reason,
        )

    def _insert_snapshot(
        self,
        conn: sqlite3.Connection,
        values: Mapping[str, Any],
        *,
        definition_version: int,
        generation: str,
        reason: str,
    ) -> JsonDict:
        previous = conn.execute(
            "SELECT COALESCE(MAX(version),0) FROM persona_snapshot WHERE agent_id=?",
            (self.agent_id,),
        ).fetchone()[0]
        version = int(previous) + 1
        snapshot_id = new_id("psnap")
        checksum = _hash_json(values)
        count = int(
            conn.execute(
                "SELECT COUNT(*) FROM persona_contribution WHERE agent_id=? AND target_scope='agent' AND active=1",
                (self.agent_id,),
            ).fetchone()[0]
        )
        conn.execute("UPDATE persona_snapshot SET status='superseded' WHERE agent_id=? AND status='active'", (self.agent_id,))
        conn.execute(
            """
            INSERT INTO persona_snapshot(
                id,agent_id,version,definition_version,schema_version,trait_values_json,
                source_contribution_count,checksum,status,generation,created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (snapshot_id, self.agent_id, version, definition_version, PERSONA_SNAPSHOT_CONTRACT, json_dumps(values), count, checksum, generation, utc_now()),
        )
        self._insert_outbox(
            conn,
            event_type="persona_snapshot_updated",
            aggregate_ref=snapshot_id,
            payload={"snapshot_id": snapshot_id, "version": version, "checksum": checksum, "reason": reason},
        )
        return self._snapshot_from_row(conn.execute("SELECT * FROM persona_snapshot WHERE id=?", (snapshot_id,)).fetchone())

    def _rebuild_user_adaptation(self, conn: sqlite3.Connection, user_id: str, target_key: str) -> None:
        definition = conn.execute(
            """
            SELECT t.* FROM persona_trait_definition t
            JOIN persona_definition d ON d.agent_id=t.agent_id AND d.version=t.definition_version
            WHERE t.agent_id=? AND d.status='active' AND t.trait_key=?
            """,
            (self.agent_id, target_key),
        ).fetchone()
        if definition is None:
            return
        delta_sum, count = conn.execute(
            """
            SELECT COALESCE(SUM(delta),0), COUNT(*) FROM persona_contribution
            WHERE agent_id=? AND target_scope='per_user' AND user_id=? AND target_key=? AND active=1
            """,
            (self.agent_id, user_id, target_key),
        ).fetchone()
        canonical = Decimal(str(self._calculate_agent_values(conn)[target_key]))
        value = max(
            Decimal(str(definition["min_bound"])),
            min(
                Decimal(str(definition["max_bound"])),
                canonical + Decimal(str(delta_sum or 0.0)),
            ),
        )
        current = conn.execute(
            "SELECT version FROM persona_user_adaptation WHERE agent_id=? AND user_id=? AND trait_key=?",
            (self.agent_id, user_id, target_key),
        ).fetchone()
        version = int(current["version"] if current else 0) + 1
        conn.execute(
            """
            INSERT INTO persona_user_adaptation(agent_id,user_id,trait_key,value,version,evidence_count,updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id,user_id,trait_key) DO UPDATE SET
                value=excluded.value, version=excluded.version,
                evidence_count=excluded.evidence_count, updated_at=excluded.updated_at
            """,
            (self.agent_id, user_id, target_key, normalize_persona_value(value), version, int(count), utc_now()),
        )

    def _forget_source(self, conn: sqlite3.Connection, source_ref: str, *, reason: str) -> JsonDict:
        source_hash = _hash_text(source_ref)
        conn.execute(
            "INSERT OR IGNORE INTO persona_tombstone VALUES (?, ?, ?, ?, ?, NULL)",
            (new_id("ptomb"), self.agent_id, source_hash, reason, utc_now()),
        )
        candidates = conn.execute(
            """
            UPDATE persona_candidate SET status='forgotten', updated_at=?, decided_at=?
            WHERE agent_id=? AND source_ref=? AND status!='forgotten'
            """,
            (utc_now(), utc_now(), self.agent_id, source_ref),
        ).rowcount
        rows = conn.execute(
            "SELECT DISTINCT user_id,target_key,target_scope FROM persona_contribution WHERE agent_id=? AND source_ref=? AND active=1",
            (self.agent_id, source_ref),
        ).fetchall()
        contributions = conn.execute(
            """
            UPDATE persona_contribution SET active=0, deactivated_at=?, deactivation_reason=?
            WHERE agent_id=? AND source_ref=? AND active=1
            """,
            (utc_now(), f"forget:{reason}", self.agent_id, source_ref),
        ).rowcount
        for row in rows:
            if row["target_scope"] == "per_user" and row["user_id"]:
                self._rebuild_user_adaptation(conn, row["user_id"], row["target_key"])
        snapshot = self._rebuild_snapshot(conn, reason="forget")
        return {"source_ref_hash": source_hash, "candidates_forgotten": candidates, "contributions_deactivated": contributions, "snapshot": snapshot}

    def _rollback_source(self, conn: sqlite3.Connection, source_ref: str, *, reason: str) -> JsonDict:
        rows = conn.execute(
            """
            SELECT DISTINCT user_id,target_key,target_scope
            FROM persona_contribution
            WHERE agent_id=? AND source_ref=? AND active=1
            """,
            (self.agent_id, source_ref),
        ).fetchall()
        contributions = conn.execute(
            """
            UPDATE persona_contribution
            SET active=0, deactivated_at=?, deactivation_reason=?
            WHERE agent_id=? AND source_ref=? AND active=1
            """,
            (utc_now(), f"source_rollback:{reason}", self.agent_id, source_ref),
        ).rowcount
        for row in rows:
            if row["target_scope"] == "per_user" and row["user_id"]:
                self._rebuild_user_adaptation(conn, row["user_id"], row["target_key"])
        snapshot = self._rebuild_snapshot(conn, reason="source_rollback")
        return {
            "source_ref": source_ref,
            "contributions_deactivated": contributions,
            "snapshot": snapshot,
            "tombstone_created": False,
        }

    def _is_tombstoned(self, conn: sqlite3.Connection, source_ref: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM persona_tombstone WHERE agent_id=? AND source_ref_hash=?",
            (self.agent_id, _hash_text(source_ref)),
        ).fetchone()
        return row is not None

    def _insert_outbox(self, conn: sqlite3.Connection, *, event_type: str, aggregate_ref: str, payload: Mapping[str, Any]) -> None:
        payload_hash = _hash_json(payload)
        conn.execute(
            """
            INSERT OR IGNORE INTO persona_outbox(
                id,event_type,aggregate_ref,payload_hash,status,attempts,payload_json,created_at
            ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (new_id("pout"), event_type, aggregate_ref, payload_hash, json_dumps(payload), utc_now()),
        )

    def _insert_audit(
        self,
        conn: sqlite3.Connection,
        *,
        action: str,
        entity_type: str,
        entity_id: str | None,
        details: Mapping[str, Any],
        actor: str = "system",
        correlation_id: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO persona_audit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id("paudit"), correlation_id, self.agent_id, actor, action, entity_type, entity_id, json_dumps(details), utc_now()),
        )

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> JsonDict:
        values = json_loads(row["trait_values_json"], {})
        return {
            "schema_version": row["schema_version"],
            "snapshot_id": row["id"],
            "agent_id": row["agent_id"],
            "version": row["version"],
            "definition_version": row["definition_version"],
            "trait_values": values,
            "trait_bands": {key: trait_band(float(value)) for key, value in values.items()},
            "source_contribution_count": row["source_contribution_count"],
            "checksum": row["checksum"],
            "generation": row["generation"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _interest_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "agent_id": row["agent_id"],
            "user_id": row["user_id"] or None,
            "topic_key": row["topic_key"],
            "user_interest_ref": row["user_interest_ref"],
            "shared_density": row["shared_density"],
            "sora_interest": row["sora_interest"],
            "reason_refs": json_loads(row["reason_refs_json"], []),
            "attitude": row["attitude"],
            "version": row["version"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _ephemeral_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "agent_id": row["agent_id"],
            "user_id": row["user_id"] or None,
            "state_key": row["state_key"],
            "value": json_loads(row["value_json"], None),
            "source_ref": row["source_ref"],
            "confidence": row["confidence"],
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _candidate_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "candidate_id": row["id"],
            "agent_id": row["agent_id"],
            "user_id": row["user_id"],
            "target_scope": row["target_scope"],
            "target_key": row["target_key"],
            "direction": row["direction"],
            "source_kind": row["source_kind"],
            "source_ref": row["source_ref"],
            "status": row["status"],
            "idempotency_key": row["idempotency_key"],
            "candidate": json_loads(row["candidate_json"], {}),
            "source_payload": json_loads(row["source_payload_json"], {}),
            "gate_reason": row["gate_reason"],
            "error_type": row["error_type"],
            "error": row["error"],
            "model_trace": json_loads(row["model_trace_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "decided_at": row["decided_at"],
        }

    @staticmethod
    def _contribution_from_row(row: sqlite3.Row | None) -> JsonDict | None:
        if row is None:
            return None
        return {
            "contribution_id": row["id"],
            "candidate_id": row["candidate_id"],
            "agent_id": row["agent_id"],
            "user_id": row["user_id"],
            "target_scope": row["target_scope"],
            "target_key": row["target_key"],
            "delta": row["delta"],
            "support": row["support"],
            "source_ref": row["source_ref"],
            "evidence_refs": json_loads(row["evidence_refs_json"], []),
            "policy_version": row["policy_version"],
            "active": bool(row["active"]),
            "created_at": row["created_at"],
            "deactivated_at": row["deactivated_at"],
            "deactivation_reason": row["deactivation_reason"],
        }
