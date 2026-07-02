from __future__ import annotations

import sqlite3
from typing import Any, Callable

from .memory_schema import JsonDict, json_dumps, json_loads, new_id, utc_now


class MemoryRetentionService:
    """Retention, redaction, and user forget workflows for canonical memory."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        lock: Any,
        delete_evidence: Callable[..., JsonDict],
        set_memory_status: Callable[..., JsonDict],
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._delete_evidence = delete_evidence
        self._set_memory_status = set_memory_status

    def process_expired(self, *, now: str | None = None, limit: int = 100) -> JsonDict:
        cutoff = now or utc_now()
        expired_evidence: list[JsonDict] = []
        expired_memories: list[JsonDict] = []
        with self._lock:
            evidence_rows = self._conn.execute(
                """
                SELECT id
                FROM evidence
                WHERE deleted_at IS NULL
                  AND expires_at IS NOT NULL
                  AND expires_at <= ?
                ORDER BY expires_at ASC
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
        for row in evidence_rows:
            expired_evidence.append(
                self._delete_evidence(
                    row["id"],
                    reason="retention_evidence_expired",
                    actor="retention_worker",
                )
            )

        remaining = max(0, limit - len(expired_evidence))
        if remaining:
            with self._lock:
                memory_rows = self._conn.execute(
                    """
                    SELECT id
                    FROM memory_record
                    WHERE status = 'active'
                      AND valid_until IS NOT NULL
                      AND valid_until <= ?
                    ORDER BY valid_until ASC
                    LIMIT ?
                    """,
                    (cutoff, remaining),
                ).fetchall()
            for row in memory_rows:
                expired_memories.append(
                    self._set_memory_status(
                        row["id"],
                        "disabled",
                        reason="retention_memory_expired",
                        actor="retention_worker",
                    )
                )
        return {
            "status": "ok",
            "expired_evidence_count": len(expired_evidence),
            "expired_memory_count": len(expired_memories),
            "expired_evidence": expired_evidence,
            "expired_memories": expired_memories,
        }

    def request_forget(
        self,
        *,
        target_type: str,
        target_id: str,
        mode: str = "delete",
        reason: str = "user_forget_request",
        actor: str = "user",
    ) -> JsonDict:
        if target_type not in {"memory", "evidence"}:
            raise ValueError("target_type must be memory or evidence")
        if mode not in {"delete", "redact"}:
            raise ValueError("mode must be delete or redact")
        request_id = new_id("frg")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_forget_request (
                    id, target_type, target_id, mode, status, reason, actor,
                    created_at, result_json
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, '{}')
                """,
                (request_id, target_type, target_id, mode, reason, actor, utc_now()),
            )
            self._conn.commit()

        result = self._apply_forget(
            target_type=target_type,
            target_id=target_id,
            mode=mode,
            reason=reason,
            actor=actor,
        )
        with self._lock:
            self._conn.execute(
                """
                UPDATE memory_forget_request
                SET status = 'completed', completed_at = ?, result_json = ?
                WHERE id = ?
                """,
                (utc_now(), json_dumps(result), request_id),
            )
            self._conn.commit()
            return self.get_forget_request(request_id)

    def redact_evidence(
        self,
        evidence_id: str,
        *,
        reason: str = "evidence_redacted",
    ) -> JsonDict:
        with self._lock:
            now = utc_now()
            self._conn.execute(
                """
                UPDATE evidence
                SET raw_content = NULL,
                    raw_pointer = NULL,
                    visibility = 'redacted',
                    deleted_at = COALESCE(deleted_at, ?),
                    delete_reason = COALESCE(delete_reason, ?)
                WHERE id = ?
                """,
                (now, reason, evidence_id),
            )
            row = self._conn.execute(
                "SELECT * FROM evidence WHERE id = ?",
                (evidence_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"evidence not found: {evidence_id}")
            self._conn.commit()
            return {
                "id": row["id"],
                "visibility": row["visibility"],
                "deleted_at": row["deleted_at"],
                "delete_reason": row["delete_reason"],
            }

    def backlog_count(self, *, now: str | None = None) -> int:
        cutoff = now or utc_now()
        with self._lock:
            evidence_count = int(
                self._conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM evidence
                    WHERE deleted_at IS NULL
                      AND expires_at IS NOT NULL
                      AND expires_at <= ?
                    """,
                    (cutoff,),
                ).fetchone()[0]
            )
            memory_count = int(
                self._conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM memory_record
                    WHERE status = 'active'
                      AND valid_until IS NOT NULL
                      AND valid_until <= ?
                    """,
                    (cutoff,),
                ).fetchone()[0]
            )
        return evidence_count + memory_count

    def list_forget_requests(self, *, limit: int = 50) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM memory_forget_request
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._request_from_row(row) for row in rows]

    def get_forget_request(self, request_id: str) -> JsonDict:
        row = self._conn.execute(
            "SELECT * FROM memory_forget_request WHERE id = ?",
            (request_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"forget request not found: {request_id}")
        return self._request_from_row(row)

    def _apply_forget(
        self,
        *,
        target_type: str,
        target_id: str,
        mode: str,
        reason: str,
        actor: str,
    ) -> JsonDict:
        if target_type == "evidence":
            if mode == "redact":
                return {"redacted_evidence": [self.redact_evidence(target_id, reason=reason)]}
            return {"deleted_evidence": [self._delete_evidence(target_id, reason=reason, actor=actor)]}

        with self._lock:
            evidence_rows = self._conn.execute(
                "SELECT evidence_id FROM memory_evidence WHERE memory_id = ? ORDER BY evidence_id",
                (target_id,),
            ).fetchall()
        memory = self._set_memory_status(target_id, "deleted", reason=reason, actor=actor)
        affected_evidence: list[JsonDict] = []
        for row in evidence_rows:
            evidence_id = row["evidence_id"]
            if mode == "redact":
                affected_evidence.append(self.redact_evidence(evidence_id, reason=reason))
            else:
                affected_evidence.append(
                    self._delete_evidence(evidence_id, reason=reason, actor=actor)
                )
        return {
            "memory": memory,
            "affected_evidence_count": len(affected_evidence),
            "affected_evidence": affected_evidence,
        }

    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "id": row["id"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "mode": row["mode"],
            "status": row["status"],
            "reason": row["reason"],
            "actor": row["actor"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
            "result": json_loads(row["result_json"], {}),
        }
