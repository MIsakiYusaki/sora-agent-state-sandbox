from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from .memory_schema import JsonDict, json_dumps, json_loads, new_id, utc_now


PROJECTOR_TYPES = {
    "relationship": {"relationship_signal", "shared_episode"},
    "worldops": {"worldops_episode", "world_ops"},
    "persona": {"persona_delta", "profile"},
}

PROJECTION_GATEWAY_VERSION = "memory_projection_gateway.v1"
PROJECTION_EVENT_CONTRACT = "ProjectionEvent.v1"
PROJECTION_ACK_CONTRACT = "ProjectionAck.v1"
PROJECTION_SNAPSHOT_CONTRACT = "ProjectionSnapshot.v1"
PROJECTION_FRAME_CONTRACT = "ProjectionFrame.v1"

ACK_PROCESSED_STATUSES = {"applied", "skipped_duplicate", "rejected_policy"}
ACK_RETRY_STATUSES = {"failed_retryable", "throttled"}


class ProjectionAdapter(Protocol):
    """External subsystem adapter boundary for projection delivery."""

    def deliver(self, event: JsonDict) -> JsonDict:
        """Deliver a ProjectionEvent.v1 and return ProjectionAck.v1."""


class ContractProjectionAdapter:
    """Local contract adapter used until an external subsystem is attached.

    It verifies and records the ProjectionEvent/Ack/Snapshot flow without owning
    relationship, persona, or WorldOps domain logic.
    """

    adapter_name = "local_contract_projection_adapter"

    def deliver(self, event: JsonDict) -> JsonDict:
        subsystem = str(event.get("target_subsystem") or event.get("projector_name") or "unknown")
        memory = event.get("memory", {})
        memory_id = memory.get("id")
        source_memory_ids = [memory_id] if memory_id else []
        event_type = str(event.get("event_type") or "memory_upsert")
        payload_hash = str(event.get("idempotency", {}).get("payload_hash") or "")
        snapshot_ref = f"{subsystem}:contract"
        snapshot_version = f"{payload_hash[:12] or event.get('event_id')}:{event_type}"
        frame = {
            "contract": PROJECTION_FRAME_CONTRACT,
            "subsystem": subsystem,
            "adapter": self.adapter_name,
            "status": "active" if event_type == "memory_upsert" else "tombstoned",
            "snapshot_ref": snapshot_ref,
            "snapshot_version": snapshot_version,
            "source_memory_ids": source_memory_ids,
            "state": self._state_for(subsystem, event),
        }
        snapshot = {
            "contract": PROJECTION_SNAPSHOT_CONTRACT,
            "subsystem": subsystem,
            "snapshot_ref": snapshot_ref,
            "snapshot_version": snapshot_version,
            "frame": frame,
            "source_memory_ids": source_memory_ids,
            "updated_at": utc_now(),
            "adapter": self.adapter_name,
            "metadata": {
                "gateway_version": PROJECTION_GATEWAY_VERSION,
                "event_type": event_type,
                "memory_type": memory.get("type"),
                "external_subsystem_attached": False,
            },
        }
        return {
            "contract": PROJECTION_ACK_CONTRACT,
            "event_id": event["event_id"],
            "target_subsystem": subsystem,
            "status": "applied",
            "idempotency": event.get("idempotency", {}),
            "snapshot_ref": snapshot_ref,
            "snapshot_version": snapshot_version,
            "snapshot": snapshot,
            "proposal_requests": [],
            "warnings": ["contract_adapter_recorded_no_external_subsystem"],
            "processed_at": utc_now(),
        }

    @staticmethod
    def _state_for(subsystem: str, event: JsonDict) -> JsonDict:
        memory = event.get("memory", {})
        if subsystem == "relationship":
            return {
                "relationship_state_ref": "external_relationship_system",
                "incoming_signal_type": memory.get("type"),
                "lifecycle": event.get("lifecycle", {}),
            }
        if subsystem == "persona":
            return {
                "persona_state_ref": "external_persona_system",
                "incoming_delta_type": memory.get("type"),
                "bounds_version": event.get("adapter_config", {}).get("bounds_version"),
            }
        if subsystem == "worldops":
            return {
                "worldops_state_ref": "external_worldops_system",
                "incoming_episode_type": memory.get("type"),
                "content_pack_ref": event.get("adapter_config", {}).get("content_pack_ref"),
            }
        return {"external_state_ref": f"external_{subsystem}_system"}


class MemoryProjectorBus:
    """Projection gateway outbox for committed memory changes and tombstones."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        lock: Any,
        adapters: dict[str, ProjectionAdapter] | None = None,
        get_memory: Callable[[str], JsonDict] | None = None,
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._get_memory = get_memory
        self._default_adapter = ContractProjectionAdapter()
        self._adapters: dict[str, ProjectionAdapter] = dict(adapters or {})

    def register_adapter(self, projector_name: str, adapter: ProjectionAdapter) -> None:
        self._adapters[projector_name] = adapter

    def enqueue_memory_change(
        self,
        memory: JsonDict,
        *,
        action: str,
        reason: str,
        source_ref: str | None = None,
        generation: str | None = None,
    ) -> list[JsonDict]:
        projector_names = self._projectors_for(memory, action=action)
        queued: list[JsonDict] = []
        with self._lock:
            for projector_name in projector_names:
                payload = self._projection_event(
                    memory,
                    projector_name=projector_name,
                    action=action,
                    reason=reason,
                    source_ref=source_ref,
                    generation=generation,
                )
                existing = self._find_existing_event(
                    payload["idempotency"]["event_key"],
                    payload["idempotency"]["payload_hash"],
                )
                if existing is not None:
                    queued.append(self._from_row(existing))
                    continue
                superseded = self._find_event_with_same_key(
                    payload["idempotency"]["event_key"],
                )
                self._conn.execute(
                    """
                    INSERT INTO memory_projector_outbox (
                        id, projector_name, memory_id, action, event_type, event_key,
                        payload_hash, target_subsystem, projector_version, status,
                        attempts, max_attempts, payload_json, result_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 3, ?, '{}', ?)
                    """,
                    (
                        payload["event_id"],
                        projector_name,
                        memory["id"],
                        action,
                        payload["event_type"],
                        payload["idempotency"]["event_key"],
                        payload["idempotency"]["payload_hash"],
                        projector_name,
                        PROJECTION_GATEWAY_VERSION,
                        json_dumps(payload),
                        utc_now(),
                    ),
                )
                if superseded is not None:
                    self._record_audit(
                        action="payload_superseded",
                        subsystem=projector_name,
                        event_id=payload["event_id"],
                        memory_id=memory["id"],
                        details={
                            "event_key": payload["idempotency"]["event_key"],
                            "old_event_id": superseded["id"],
                            "old_payload_hash": row_value(superseded, "payload_hash"),
                            "new_payload_hash": payload["idempotency"]["payload_hash"],
                            "old_status": superseded["status"],
                        },
                    )
                queued.append(self.get_item(payload["event_id"]))
        return queued

    def process_pending(self, *, limit: int = 100, commit: bool = True) -> JsonDict:
        processed: list[JsonDict] = []
        failed: list[JsonDict] = []
        with self._lock:
            now = utc_now()
            rows = self._conn.execute(
                """
                SELECT * FROM memory_projector_outbox
                WHERE status = 'pending'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            for row in rows:
                try:
                    item = self._process_row(row)
                except Exception as exc:  # pragma: no cover - defensive path.
                    item = self._record_failure(
                        row,
                        status="failed_retryable",
                        error_type="gateway_exception",
                        error=str(exc),
                    )
                if item["status"] == "processed":
                    processed.append(item)
                else:
                    failed.append(item)
            if commit:
                self._conn.commit()
        return {
            "status": "ok" if not failed else "partial_failure",
            "processed_count": len(processed),
            "failed_count": len(failed),
            "processed": processed,
            "failed": failed,
        }

    def pending_count(self) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM memory_projector_outbox WHERE status = 'pending'"
                ).fetchone()[0]
            )

    def failed_count(self) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    """
                    SELECT COUNT(*) FROM memory_projector_outbox
                    WHERE status IN ('failed', 'dead_letter')
                    """
                ).fetchone()[0]
            )

    def dead_letter_count(self) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM memory_projector_outbox WHERE status = 'dead_letter'"
                ).fetchone()[0]
            )

    def list_items(self, *, status: str | None = None, limit: int = 50) -> list[JsonDict]:
        sql = "SELECT * FROM memory_projector_outbox"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._from_row(row) for row in rows]

    def list_checkpoints(self) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memory_projector_checkpoint ORDER BY projector_name"
            ).fetchall()
        return [
            {
                "projector_name": row["projector_name"],
                "last_event_id": row["last_event_id"],
                "last_memory_id": row["last_memory_id"],
                "status": row["status"],
                "processed_count": row["processed_count"],
                "last_snapshot_ref": row_value(row, "last_snapshot_ref"),
                "last_snapshot_version": row_value(row, "last_snapshot_version"),
                "last_ack_status": row_value(row, "last_ack_status"),
                "last_generation": row_value(row, "last_generation"),
                "updated_at": row["updated_at"],
                "metadata": json_loads(row["metadata_json"], {}),
            }
            for row in rows
        ]

    def list_snapshots(
        self,
        *,
        subsystem: str | None = None,
        status: str | None = "active",
        limit: int = 50,
    ) -> list[JsonDict]:
        sql = "SELECT * FROM memory_projection_snapshot"
        params: list[Any] = []
        clauses: list[str] = []
        if subsystem:
            clauses.append("subsystem = ?")
            params.append(subsystem)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._snapshot_from_row(row) for row in rows]

    def latest_frame(self, subsystem: str) -> JsonDict:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM memory_projection_snapshot
                WHERE subsystem = ? AND status = 'active'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (subsystem,),
            ).fetchone()
        if row is None:
            return {
                "contract": PROJECTION_FRAME_CONTRACT,
                "subsystem": subsystem,
                "status": "missing",
                "source_memory_ids": [],
                "snapshot_ref": None,
                "snapshot_version": None,
            }
        return self._snapshot_from_row(row)["frame"]

    def record_proposal_request(
        self,
        *,
        subsystem: str,
        proposal: JsonDict,
        source_event_id: str | None = None,
        source_snapshot_ref: str | None = None,
        commit: bool = True,
    ) -> JsonDict:
        proposal_id = new_id("proj_prop")
        now = utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_projection_proposal (
                    id, subsystem, source_snapshot_ref, source_event_id, status,
                    proposal_json, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    proposal_id,
                    subsystem,
                    source_snapshot_ref,
                    source_event_id,
                    json_dumps(proposal),
                    now,
                ),
            )
            if commit:
                self._conn.commit()
        return self.get_proposal_request(proposal_id)

    def get_proposal_request(self, proposal_id: str) -> JsonDict:
        row = self._conn.execute(
            "SELECT * FROM memory_projection_proposal WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"projection proposal not found: {proposal_id}")
        return {
            "id": row["id"],
            "subsystem": row["subsystem"],
            "source_snapshot_ref": row["source_snapshot_ref"],
            "source_event_id": row["source_event_id"],
            "status": row["status"],
            "proposal": json_loads(row["proposal_json"], {}),
            "routed_candidate_id": row["routed_candidate_id"],
            "error": row["error"],
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
        }

    def list_proposal_requests(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[JsonDict]:
        sql = "SELECT * FROM memory_projection_proposal"
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
                "subsystem": row["subsystem"],
                "source_snapshot_ref": row["source_snapshot_ref"],
                "source_event_id": row["source_event_id"],
                "status": row["status"],
                "proposal": json_loads(row["proposal_json"], {}),
                "routed_candidate_id": row["routed_candidate_id"],
                "error": row["error"],
                "created_at": row["created_at"],
                "resolved_at": row["resolved_at"],
            }
            for row in rows
        ]

    def rebuild_from_memories(
        self,
        memories: list[JsonDict],
        *,
        subsystems: list[str] | None = None,
        limit: int = 500,
        reason: str = "projection_rebuild",
    ) -> JsonDict:
        requested = tuple(subsystems or PROJECTOR_TYPES.keys())
        invalid = sorted(set(requested) - set(PROJECTOR_TYPES))
        if invalid:
            raise ValueError(f"unknown projection subsystems: {', '.join(invalid)}")
        run_id = new_id("proj_rebuild")
        generation = f"{PROJECTION_GATEWAY_VERSION}:{run_id}"
        now = utc_now()
        scoped_memories = [
            memory
            for memory in memories
            if memory.get("status") == "active"
            and any(name in requested for name in self._projectors_for(memory, action="upsert"))
        ][:limit]
        with self._lock:
            before_snapshots = self._active_snapshot_index(requested)
            self._conn.execute(
                """
                INSERT INTO memory_projection_rebuild_run (
                    id, generation, status, subsystems_json, total_count,
                    processed_count, failed_count, payload_json, result_json,
                    created_at, updated_at
                ) VALUES (?, ?, 'running', ?, ?, 0, 0, ?, '{}', ?, ?)
                """,
                (
                    run_id,
                    generation,
                    json_dumps(list(requested)),
                    len(scoped_memories),
                    json_dumps({"reason": reason, "limit": limit}),
                    now,
                    now,
                ),
            )
            queued_count = 0
            for memory in scoped_memories:
                targets = [
                    name
                    for name in self._projectors_for(memory, action="upsert")
                    if name in requested
                ]
                for name in targets:
                    payload = self._projection_event(
                        memory,
                        projector_name=name,
                        action="upsert",
                        reason=reason,
                        source_ref=run_id,
                        generation=generation,
                    )
                    self._conn.execute(
                        """
                        INSERT INTO memory_projector_outbox (
                            id, projector_name, memory_id, action, event_type, event_key,
                            payload_hash, target_subsystem, projector_version, status,
                            attempts, max_attempts, payload_json, result_json, created_at
                        ) VALUES (?, ?, ?, 'upsert', ?, ?, ?, ?, ?, 'pending', 0, 3, ?, '{}', ?)
                        """,
                        (
                            payload["event_id"],
                            name,
                            memory["id"],
                            payload["event_type"],
                            payload["idempotency"]["event_key"],
                            payload["idempotency"]["payload_hash"],
                            name,
                            PROJECTION_GATEWAY_VERSION,
                            json_dumps(payload),
                            utc_now(),
                        ),
                    )
                    queued_count += 1
            result = self.process_pending(limit=max(queued_count, 1), commit=False)
            after_snapshots = self._active_snapshot_index(requested)
            parity = self._rebuild_parity_report(
                before=before_snapshots,
                after=after_snapshots,
                requested=requested,
            )
            completed_at = utc_now()
            status = "completed" if result["failed_count"] == 0 else "completed_with_errors"
            self._conn.execute(
                """
                UPDATE memory_projection_rebuild_run
                SET status = ?, processed_count = ?, failed_count = ?,
                    result_json = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    result["processed_count"],
                    result["failed_count"],
                    json_dumps({**result, "parity": parity}),
                    completed_at,
                    completed_at,
                    run_id,
                ),
            )
            self._record_audit(
                action="rebuild_parity_checked",
                subsystem=",".join(requested),
                event_id=run_id,
                memory_id=None,
                details=parity,
            )
            self._conn.commit()
        return self.get_rebuild_run(run_id)

    def get_rebuild_run(self, run_id: str) -> JsonDict:
        row = self._conn.execute(
            "SELECT * FROM memory_projection_rebuild_run WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"projection rebuild run not found: {run_id}")
        return self._rebuild_from_row(row)

    def list_rebuild_runs(self, *, limit: int = 20) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM memory_projection_rebuild_run
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._rebuild_from_row(row) for row in rows]

    def list_audit(
        self,
        *,
        action: str | None = None,
        subsystem: str | None = None,
        limit: int = 50,
    ) -> list[JsonDict]:
        sql = "SELECT * FROM memory_projection_audit"
        params: list[Any] = []
        clauses: list[str] = []
        if action:
            clauses.append("action = ?")
            params.append(action)
        if subsystem:
            clauses.append("subsystem = ?")
            params.append(subsystem)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._audit_from_row(row) for row in rows]

    def get_item(self, event_id: str) -> JsonDict:
        row = self._conn.execute(
            "SELECT * FROM memory_projector_outbox WHERE id = ?",
            (event_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"projector outbox item not found: {event_id}")
        return self._from_row(row)

    def _process_row(self, row: sqlite3.Row) -> JsonDict:
        payload = json_loads(row["payload_json"], {})
        event = self._event_from_payload(row, payload)
        if self._get_memory is not None:
            try:
                current = self._get_memory(row["memory_id"])
            except ValueError:
                return self._record_dead_letter(row, error_type="source_unavailable", error="Canonical source unavailable")
            if event.get("memory") != self._memory_payload(current) or event.get("policy") != self._policy_payload(current):
                ack = self._normalize_ack(event, {"status": "rejected_policy", "reason": "source_state_superseded"})
                return self._record_processed(row, event, ack)
        adapter = self._adapters.get(row["projector_name"], self._default_adapter)
        try:
            ack = self._normalize_ack(event, adapter.deliver(event))
        except Exception as exc:
            return self._record_failure(
                row,
                status="failed_retryable",
                error_type="adapter_exception",
                error=str(exc),
            )
        ack_status = str(ack.get("status") or "failed_permanent")
        if ack_status in ACK_PROCESSED_STATUSES:
            return self._record_processed(row, event, ack)
        if ack_status in ACK_RETRY_STATUSES:
            return self._record_failure(
                row,
                status=ack_status,
                error_type=str(ack.get("error_type") or ack_status),
                error=str(ack.get("error") or ack.get("message") or ack_status),
                result=ack,
            )
        return self._record_dead_letter(
            row,
            error_type=str(ack.get("error_type") or ack_status),
            error=str(ack.get("error") or ack.get("message") or ack_status),
            result=ack,
        )

    def _record_processed(
        self,
        row: sqlite3.Row,
        event: JsonDict,
        ack: JsonDict,
    ) -> JsonDict:
        now = utc_now()
        self._conn.execute(
            """
            UPDATE memory_projector_outbox
            SET status = 'processed', attempts = attempts + 1,
                result_json = ?, error_type = NULL, error = NULL,
                last_attempt_at = ?, next_attempt_at = NULL, processed_at = ?
            WHERE id = ?
            """,
            (json_dumps(ack), now, now, row["id"]),
        )
        self._cache_snapshot(ack, now=now)
        self._update_checkpoint(row, event, ack, now=now)
        for proposal in ack.get("proposal_requests", []) or []:
            if isinstance(proposal, dict):
                self.record_proposal_request(
                    subsystem=row["projector_name"],
                    proposal=proposal,
                    source_event_id=row["id"],
                    source_snapshot_ref=ack.get("snapshot_ref"),
                    commit=False,
                )
        return self.get_item(row["id"])

    def _record_failure(
        self,
        row: sqlite3.Row,
        *,
        status: str,
        error_type: str,
        error: str,
        result: JsonDict | None = None,
    ) -> JsonDict:
        attempts = int(row["attempts"]) + 1
        max_attempts = int(row_value(row, "max_attempts", 3) or 3)
        if attempts >= max_attempts:
            return self._record_dead_letter(
                row,
                error_type=error_type,
                error=error,
                result=result,
                attempts=attempts,
            )
        now = utc_now()
        next_attempt_at = (
            datetime.now(timezone.utc) + timedelta(minutes=max(1, attempts))
        ).isoformat()
        ack = result or {
            "contract": PROJECTION_ACK_CONTRACT,
            "event_id": row["id"],
            "target_subsystem": row["projector_name"],
            "status": status,
            "error_type": error_type,
            "error": error,
        }
        self._conn.execute(
            """
            UPDATE memory_projector_outbox
            SET status = 'pending', attempts = ?, result_json = ?,
                error_type = ?, error = ?, last_attempt_at = ?,
                next_attempt_at = ?, processed_at = NULL
            WHERE id = ?
            """,
            (
                attempts,
                json_dumps(ack),
                error_type,
                error,
                now,
                next_attempt_at,
                row["id"],
            ),
        )
        return self.get_item(row["id"])

    def _record_dead_letter(
        self,
        row: sqlite3.Row,
        *,
        error_type: str,
        error: str,
        result: JsonDict | None = None,
        attempts: int | None = None,
    ) -> JsonDict:
        now = utc_now()
        ack = result or {
            "contract": PROJECTION_ACK_CONTRACT,
            "event_id": row["id"],
            "target_subsystem": row["projector_name"],
            "status": "failed_permanent",
            "error_type": error_type,
            "error": error,
        }
        self._conn.execute(
            """
            UPDATE memory_projector_outbox
            SET status = 'dead_letter', attempts = ?, result_json = ?,
                error_type = ?, error = ?, last_attempt_at = ?,
                next_attempt_at = NULL, processed_at = ?
            WHERE id = ?
            """,
            (
                attempts if attempts is not None else int(row["attempts"]) + 1,
                json_dumps(ack),
                error_type,
                error,
                now,
                now,
                row["id"],
            ),
        )
        self._update_checkpoint(row, self._event_from_payload(row, json_loads(row["payload_json"], {})), ack, now=now)
        return self.get_item(row["id"])

    def _update_checkpoint(
        self,
        row: sqlite3.Row,
        event: JsonDict,
        ack: JsonDict,
        *,
        now: str,
    ) -> None:
        checkpoint_status = "ok" if ack.get("status") in ACK_PROCESSED_STATUSES else "error"
        self._conn.execute(
            """
            INSERT INTO memory_projector_checkpoint (
                projector_name, last_event_id, last_memory_id, status,
                processed_count, updated_at, last_snapshot_ref,
                last_snapshot_version, last_ack_status, last_generation, metadata_json
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(projector_name) DO UPDATE SET
                last_event_id = excluded.last_event_id,
                last_memory_id = excluded.last_memory_id,
                status = excluded.status,
                processed_count = memory_projector_checkpoint.processed_count + 1,
                updated_at = excluded.updated_at,
                last_snapshot_ref = excluded.last_snapshot_ref,
                last_snapshot_version = excluded.last_snapshot_version,
                last_ack_status = excluded.last_ack_status,
                last_generation = excluded.last_generation,
                metadata_json = excluded.metadata_json
            """,
            (
                row["projector_name"],
                row["id"],
                row["memory_id"],
                checkpoint_status,
                now,
                ack.get("snapshot_ref"),
                ack.get("snapshot_version"),
                ack.get("status"),
                event.get("generation"),
                json_dumps(
                    {
                        "last_action": row["action"],
                        "event_type": event.get("event_type"),
                        "ack_status": ack.get("status"),
                        "gateway_version": PROJECTION_GATEWAY_VERSION,
                    }
                ),
            ),
        )

    def _cache_snapshot(self, ack: JsonDict, *, now: str) -> None:
        snapshot = ack.get("snapshot")
        if not isinstance(snapshot, dict):
            return
        subsystem = str(snapshot.get("subsystem") or ack.get("target_subsystem"))
        frame = snapshot.get("frame")
        if not isinstance(frame, dict):
            frame = {
                "contract": PROJECTION_FRAME_CONTRACT,
                "subsystem": subsystem,
                "status": "active",
                "source_memory_ids": snapshot.get("source_memory_ids", []),
                "snapshot_ref": snapshot.get("snapshot_ref") or ack.get("snapshot_ref"),
                "snapshot_version": snapshot.get("snapshot_version") or ack.get("snapshot_version"),
            }
        self._conn.execute(
            """
            UPDATE memory_projection_snapshot
            SET status = 'superseded', updated_at = ?
            WHERE subsystem = ? AND status = 'active'
            """,
            (now, subsystem),
        )
        self._conn.execute(
            """
            INSERT INTO memory_projection_snapshot (
                id, subsystem, snapshot_ref, snapshot_version, frame_json,
                source_memory_ids_json, status, created_at, updated_at,
                metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                new_id("proj_snap"),
                subsystem,
                str(snapshot.get("snapshot_ref") or ack.get("snapshot_ref")),
                str(snapshot.get("snapshot_version") or ack.get("snapshot_version")),
                json_dumps(frame),
                json_dumps(snapshot.get("source_memory_ids", [])),
                now,
                now,
                json_dumps(snapshot.get("metadata", {})),
            ),
        )

    def _projection_event(
        self,
        memory: JsonDict,
        *,
        projector_name: str,
        action: str,
        reason: str,
        source_ref: str | None,
        generation: str | None,
    ) -> JsonDict:
        event_type = self._event_type_for(action)
        memory_payload = self._memory_payload(memory)
        event_body = {
            "contract": PROJECTION_EVENT_CONTRACT,
            "target_subsystem": projector_name,
            "projector_name": projector_name,
            "event_type": event_type,
            "action": action,
            "reason": reason,
            "source": {
                "source_ref": source_ref,
                "source_kind": "memory_runtime",
                "committed_only": True,
            },
            "memory": memory_payload,
            "policy": self._policy_payload(memory),
            "lifecycle": memory.get("lifecycle", {}),
            "adapter_config": self._adapter_config(projector_name),
            "generation": generation,
            "gateway_version": PROJECTION_GATEWAY_VERSION,
        }
        payload_hash = self._hash(event_body)
        event_key = self._event_key(projector_name, memory["id"], action, source_ref, reason, generation)
        event_id = new_id("proj")
        return {
            **event_body,
            "event_id": event_id,
            "idempotency": {
                "event_key": event_key,
                "payload_hash": payload_hash,
                "projector_version": PROJECTION_GATEWAY_VERSION,
            },
            "created_at": utc_now(),
        }

    def _event_from_payload(self, row: sqlite3.Row, payload: JsonDict) -> JsonDict:
        if payload.get("contract") == PROJECTION_EVENT_CONTRACT:
            return payload
        legacy = {
            "contract": PROJECTION_EVENT_CONTRACT,
            "event_id": row["id"],
            "target_subsystem": row["projector_name"],
            "projector_name": row["projector_name"],
            "event_type": row_value(row, "event_type") or self._event_type_for(row["action"]),
            "action": row["action"],
            "reason": payload.get("reason"),
            "source": {
                "source_ref": payload.get("source_ref"),
                "source_kind": "memory_runtime",
                "committed_only": True,
            },
            "memory": {
                "id": row["memory_id"],
                "type": payload.get("memory_type"),
                "status": payload.get("memory_status"),
                "summary": payload.get("summary"),
                "tags": payload.get("tags", []),
                "evidence_ids": [],
            },
            "policy": {},
            "lifecycle": {},
            "adapter_config": self._adapter_config(row["projector_name"]),
            "generation": None,
            "gateway_version": PROJECTION_GATEWAY_VERSION,
            "idempotency": {
                "event_key": row_value(row, "event_key") or f"legacy:{row['id']}",
                "payload_hash": row_value(row, "payload_hash") or self._hash(payload),
                "projector_version": PROJECTION_GATEWAY_VERSION,
            },
            "created_at": row["created_at"],
        }
        return legacy

    @staticmethod
    def _normalize_ack(event: JsonDict, ack: JsonDict) -> JsonDict:
        normalized = dict(ack or {})
        normalized.setdefault("contract", PROJECTION_ACK_CONTRACT)
        normalized.setdefault("event_id", event["event_id"])
        normalized.setdefault("target_subsystem", event["target_subsystem"])
        normalized.setdefault("status", "applied")
        normalized.setdefault("idempotency", event.get("idempotency", {}))
        normalized.setdefault("proposal_requests", [])
        normalized.setdefault("processed_at", utc_now())
        return normalized

    @staticmethod
    def _memory_payload(memory: JsonDict) -> JsonDict:
        return {
            "id": memory["id"],
            "owner_user_id": memory.get("owner_user_id"),
            "agent_id": memory.get("agent_id"),
            "type": memory.get("type"),
            "status": memory.get("status"),
            "summary": memory.get("summary"),
            "tags": memory.get("tags", []),
            "confidence": memory.get("confidence"),
            "importance": memory.get("importance"),
            "sensitivity": memory.get("sensitivity"),
            "evidence_ids": memory.get("evidence_ids", []),
            "evidence_episode_ids": memory.get("evidence_episode_ids", []),
            "supersedes_id": memory.get("supersedes_id"),
            "valid_from": memory.get("valid_from"),
            "valid_until": memory.get("valid_until"),
            "created_at": memory.get("created_at"),
            "updated_at": memory.get("updated_at"),
        }

    @staticmethod
    def _policy_payload(memory: JsonDict) -> JsonDict:
        return {
            "retrieval_permission": memory.get("retrieval_permission"),
            "use_permission": memory.get("use_permission"),
            "disclosure_permission": memory.get("disclosure_permission"),
            "surface_permission": memory.get("surface_permission"),
            "surface_cooldown_until": memory.get("surface_cooldown_until"),
            "retention_policy": memory.get("retention_policy"),
        }

    @staticmethod
    def _adapter_config(projector_name: str) -> JsonDict:
        if projector_name == "persona":
            return {
                "bounds_owner": "external_persona_system",
                "bounds_version": "persona_bounds.external.v1",
            }
        if projector_name == "worldops":
            return {
                "content_pack_owner": "external_worldops_system",
                "content_pack_ref": "worldops.content_pack.external",
            }
        if projector_name == "relationship":
            return {
                "state_owner": "external_relationship_system",
                "archived_memory_semantics": "external_system_decides",
            }
        return {}

    @staticmethod
    def _event_type_for(action: str) -> str:
        if action == "upsert":
            return "memory_upsert"
        if action == "rollback":
            return "memory_rollback"
        return "memory_tombstone"

    @staticmethod
    def _event_key(
        projector_name: str,
        memory_id: str,
        action: str,
        source_ref: str | None,
        reason: str,
        generation: str | None,
    ) -> str:
        source = source_ref or reason or "memory_runtime"
        if generation:
            source = f"{generation}:{source}"
        return f"{PROJECTION_GATEWAY_VERSION}:{projector_name}:{memory_id}:{action}:{source}"

    @staticmethod
    def _hash(value: Any) -> str:
        return hashlib.sha256(json_dumps(value).encode("utf-8")).hexdigest()

    def _find_existing_event(self, event_key: str, payload_hash: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT * FROM memory_projector_outbox
            WHERE event_key = ? AND payload_hash = ?
              AND status IN ('pending', 'processed')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (event_key, payload_hash),
        ).fetchone()

    def _find_event_with_same_key(self, event_key: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT * FROM memory_projector_outbox
            WHERE event_key = ?
              AND status IN ('pending', 'processed', 'dead_letter')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (event_key,),
        ).fetchone()

    def _active_snapshot_index(self, subsystems: tuple[str, ...]) -> dict[str, JsonDict | None]:
        snapshots: dict[str, JsonDict | None] = {}
        for subsystem in subsystems:
            row = self._conn.execute(
                """
                SELECT * FROM memory_projection_snapshot
                WHERE subsystem = ? AND status = 'active'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (subsystem,),
            ).fetchone()
            snapshots[subsystem] = self._snapshot_from_row(row) if row is not None else None
        return snapshots

    @staticmethod
    def _rebuild_parity_report(
        *,
        before: dict[str, JsonDict | None],
        after: dict[str, JsonDict | None],
        requested: tuple[str, ...],
    ) -> JsonDict:
        diffs: list[JsonDict] = []
        for subsystem in requested:
            before_item = before.get(subsystem)
            after_item = after.get(subsystem)
            before_frame = before_item.get("frame", {}) if before_item else {}
            after_frame = after_item.get("frame", {}) if after_item else {}
            before_source_ids = sorted(before_item.get("source_memory_ids", [])) if before_item else []
            after_source_ids = sorted(after_item.get("source_memory_ids", [])) if after_item else []
            frame_state_equal = before_frame.get("state") == after_frame.get("state")
            source_ids_equal = before_source_ids == after_source_ids
            if before_item is None and after_item is None:
                parity_status = "empty"
            elif frame_state_equal and source_ids_equal:
                parity_status = "matched"
            else:
                parity_status = "changed"
                diffs.append(
                    {
                        "subsystem": subsystem,
                        "before_snapshot_ref": before_item.get("snapshot_ref") if before_item else None,
                        "after_snapshot_ref": after_item.get("snapshot_ref") if after_item else None,
                        "before_snapshot_version": before_item.get("snapshot_version") if before_item else None,
                        "after_snapshot_version": after_item.get("snapshot_version") if after_item else None,
                        "source_ids_equal": source_ids_equal,
                        "frame_state_equal": frame_state_equal,
                    }
                )
            if parity_status == "empty":
                continue
        return {
            "contract": "ProjectionRebuildParityReport.v1",
            "status": "matched" if not diffs else "changed",
            "diff_count": len(diffs),
            "diffs": diffs,
            "checked_subsystems": list(requested),
        }

    def _record_audit(
        self,
        *,
        action: str,
        subsystem: str,
        event_id: str | None,
        memory_id: str | None,
        details: JsonDict,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO memory_projection_audit (
                id, action, subsystem, event_id, memory_id, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("proj_audit"),
                action,
                subsystem,
                event_id,
                memory_id,
                json_dumps(details),
                utc_now(),
            ),
        )

    @staticmethod
    def _projectors_for(memory: JsonDict, *, action: str) -> tuple[str, ...]:
        if action != "upsert":
            return tuple(PROJECTOR_TYPES)
        memory_type = str(memory.get("type") or "")
        return tuple(
            projector
            for projector, memory_types in PROJECTOR_TYPES.items()
            if memory_type in memory_types
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> JsonDict:
        payload = json_loads(row["payload_json"], {})
        result = json_loads(row["result_json"], {})
        return {
            "id": row["id"],
            "projector_name": row["projector_name"],
            "target_subsystem": row_value(row, "target_subsystem") or row["projector_name"],
            "memory_id": row["memory_id"],
            "action": row["action"],
            "event_type": row_value(row, "event_type"),
            "event_key": row_value(row, "event_key"),
            "payload_hash": row_value(row, "payload_hash"),
            "projector_version": row_value(row, "projector_version"),
            "status": row["status"],
            "attempts": row["attempts"],
            "max_attempts": row_value(row, "max_attempts", 3),
            "payload": payload,
            "result": result,
            "error_type": row_value(row, "error_type"),
            "error": row["error"],
            "created_at": row["created_at"],
            "last_attempt_at": row_value(row, "last_attempt_at"),
            "next_attempt_at": row_value(row, "next_attempt_at"),
            "processed_at": row["processed_at"],
        }

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "id": row["id"],
            "subsystem": row["subsystem"],
            "snapshot_ref": row["snapshot_ref"],
            "snapshot_version": row["snapshot_version"],
            "frame": json_loads(row["frame_json"], {}),
            "source_memory_ids": json_loads(row["source_memory_ids_json"], []),
            "status": row["status"],
            "metadata": json_loads(row["metadata_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _rebuild_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "id": row["id"],
            "generation": row["generation"],
            "status": row["status"],
            "subsystems": json_loads(row["subsystems_json"], []),
            "total_count": row["total_count"],
            "processed_count": row["processed_count"],
            "failed_count": row["failed_count"],
            "payload": json_loads(row["payload_json"], {}),
            "result": json_loads(row["result_json"], {}),
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
        }

    @staticmethod
    def _audit_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "id": row["id"],
            "action": row["action"],
            "subsystem": row["subsystem"],
            "event_id": row["event_id"],
            "memory_id": row["memory_id"],
            "details": json_loads(row["details_json"], {}),
            "created_at": row["created_at"],
        }


def row_value(row: sqlite3.Row | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except (IndexError, KeyError):
        return default
