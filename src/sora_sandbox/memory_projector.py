from __future__ import annotations

import sqlite3
from typing import Any

from .memory_schema import JsonDict, json_dumps, json_loads, new_id, utc_now


PROJECTOR_TYPES = {
    "relationship": {"relationship_signal", "shared_episode"},
    "worldops": {"worldops_episode", "world_ops"},
    "persona": {"persona_delta", "profile"},
}


class MemoryProjectorBus:
    """Projector outbox contract for committed memory projections and tombstones."""

    def __init__(self, conn: sqlite3.Connection, *, lock: Any) -> None:
        self._conn = conn
        self._lock = lock

    def enqueue_memory_change(
        self,
        memory: JsonDict,
        *,
        action: str,
        reason: str,
        source_ref: str | None = None,
    ) -> list[JsonDict]:
        projector_names = self._projectors_for(memory, action=action)
        queued: list[JsonDict] = []
        with self._lock:
            for projector_name in projector_names:
                event_id = new_id("proj")
                payload = {
                    "contract": "projector_event.v1",
                    "projector_name": projector_name,
                    "memory_id": memory["id"],
                    "memory_type": memory.get("type"),
                    "memory_status": memory.get("status"),
                    "action": action,
                    "reason": reason,
                    "source_ref": source_ref,
                    "summary": memory.get("summary") if action == "upsert" else None,
                    "tags": memory.get("tags", []),
                    "committed_only": True,
                }
                self._conn.execute(
                    """
                    INSERT INTO memory_projector_outbox (
                        id, projector_name, memory_id, action, status, attempts,
                        payload_json, result_json, created_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, ?, '{}', ?)
                    """,
                    (
                        event_id,
                        projector_name,
                        memory["id"],
                        action,
                        json_dumps(payload),
                        utc_now(),
                    ),
                )
                queued.append(self.get_item(event_id))
        return queued

    def process_pending(self, *, limit: int = 100, commit: bool = True) -> JsonDict:
        processed: list[JsonDict] = []
        failed: list[JsonDict] = []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM memory_projector_outbox
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            for row in rows:
                try:
                    processed.append(self._process_row(row))
                except Exception as exc:  # pragma: no cover - defensive path.
                    now = utc_now()
                    self._conn.execute(
                        """
                        UPDATE memory_projector_outbox
                        SET status = 'failed', attempts = attempts + 1,
                            error = ?, processed_at = ?
                        WHERE id = ?
                        """,
                        (str(exc), now, row["id"]),
                    )
                    failed.append(self.get_item(row["id"]))
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
                    "SELECT COUNT(*) FROM memory_projector_outbox WHERE status = 'failed'"
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
                "updated_at": row["updated_at"],
                "metadata": json_loads(row["metadata_json"], {}),
            }
            for row in rows
        ]

    def get_item(self, event_id: str) -> JsonDict:
        row = self._conn.execute(
            "SELECT * FROM memory_projector_outbox WHERE id = ?",
            (event_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"projector outbox item not found: {event_id}")
        return self._from_row(row)

    def _process_row(self, row: sqlite3.Row) -> JsonDict:
        now = utc_now()
        payload = json_loads(row["payload_json"], {})
        result = {
            "projector_name": row["projector_name"],
            "action": row["action"],
            "memory_id": row["memory_id"],
            "applied": True,
            "stub": True,
            "contract": payload.get("contract"),
        }
        self._conn.execute(
            """
            UPDATE memory_projector_outbox
            SET status = 'processed', attempts = attempts + 1,
                result_json = ?, error = NULL, processed_at = ?
            WHERE id = ?
            """,
            (json_dumps(result), now, row["id"]),
        )
        self._conn.execute(
            """
            INSERT INTO memory_projector_checkpoint (
                projector_name, last_event_id, last_memory_id, status,
                processed_count, updated_at, metadata_json
            ) VALUES (?, ?, ?, 'ok', 1, ?, ?)
            ON CONFLICT(projector_name) DO UPDATE SET
                last_event_id = excluded.last_event_id,
                last_memory_id = excluded.last_memory_id,
                status = excluded.status,
                processed_count = memory_projector_checkpoint.processed_count + 1,
                updated_at = excluded.updated_at,
                metadata_json = excluded.metadata_json
            """,
            (
                row["projector_name"],
                row["id"],
                row["memory_id"],
                now,
                json_dumps({"last_action": row["action"], "stub": True}),
            ),
        )
        return self.get_item(row["id"])

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
        return {
            "id": row["id"],
            "projector_name": row["projector_name"],
            "memory_id": row["memory_id"],
            "action": row["action"],
            "status": row["status"],
            "attempts": row["attempts"],
            "payload": json_loads(row["payload_json"], {}),
            "result": json_loads(row["result_json"], {}),
            "error": row["error"],
            "created_at": row["created_at"],
            "processed_at": row["processed_at"],
        }
