from __future__ import annotations

import sqlite3
from typing import Any, Callable

from .memory_embedding import EmbeddingProvider
from .memory_graph_lite import delete_graph_edges, upsert_graph_edges
from .memory_index_document import (
    ACTIVE_INDEX_GENERATION,
    build_index_document,
    index_document_params,
    stable_hash,
    upsert_index_document_sql,
)
from .memory_schema import JsonDict, json_dumps, json_loads, new_id, utc_now
from .memory_vector_index import VectorIndex, create_vector_index_from_env


INDEX_BACKENDS = ("fts", "vector", "graph")
TRANSIENT_ERROR_TYPES = {"transient", "rate_limited", "timeout"}


class PermanentIndexError(RuntimeError):
    pass


class MemoryIndexOutbox:
    """At-least-once index synchronization for FTS, vector, and graph-lite."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        lock: Any,
        get_memory: Callable[[str], JsonDict],
        evidence_for_memory: Callable[[str], list[JsonDict]],
        sync_fts: Callable[[str, str, str, list[str]], None],
        remove_from_fts: Callable[[str], None],
        embedding_provider: EmbeddingProvider,
        vector_index: VectorIndex | None = None,
        vector_index_factory: Callable[[sqlite3.Connection, EmbeddingProvider], VectorIndex] | None = None,
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._get_memory = get_memory
        self._evidence_for_memory = evidence_for_memory
        self._sync_fts = sync_fts
        self._remove_from_fts = remove_from_fts
        if vector_index is not None:
            self.vector_index = vector_index
        elif vector_index_factory is not None:
            self.vector_index = vector_index_factory(conn, embedding_provider)
        else:
            self.vector_index = create_vector_index_from_env(conn, embedding_provider)
        self.index_backends = ("fts", self.vector_index.backend_name, "graph")

    def enqueue_memory(
        self,
        memory: JsonDict,
        *,
        action: str,
        reason: str,
        source_ref: str | None = None,
        backends: tuple[str, ...] | None = None,
        index_generation: str = ACTIVE_INDEX_GENERATION,
    ) -> list[JsonDict]:
        payload = {
            "memory_id": memory["id"],
            "summary": memory.get("summary"),
            "type": memory.get("type"),
            "tags": memory.get("tags", []),
            "status": memory.get("status"),
            "reason": reason,
            "source_ref": source_ref,
            "index_generation": index_generation,
        }
        payload_hash = stable_hash(payload)
        queued: list[JsonDict] = []
        target_backends = self._normalize_backends(backends)
        with self._lock:
            for backend in target_backends:
                outbox_id = new_id("idx")
                self._conn.execute(
                    """
                    INSERT INTO memory_index_outbox (
                        id, backend, memory_id, action, status, attempts,
                        max_attempts, payload_json, payload_hash, result_json,
                        index_generation, created_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, 3, ?, ?, '{}', ?, ?)
                    """,
                    (
                        outbox_id,
                        backend,
                        memory["id"],
                        action,
                        json_dumps(payload),
                        payload_hash,
                        index_generation,
                        utc_now(),
                    ),
                )
                queued.append(self.get_item(outbox_id))
        return queued

    def process_pending(self, *, limit: int = 100, commit: bool = True) -> JsonDict:
        processed: list[JsonDict] = []
        failed: list[JsonDict] = []
        dead_letter: list[JsonDict] = []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM memory_index_outbox
                WHERE status IN ('pending', 'failed')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (utc_now(), limit),
            ).fetchall()
            for row in rows:
                self._conn.execute(
                    """
                    UPDATE memory_index_outbox
                    SET status = 'processing', last_attempt_at = ?
                    WHERE id = ?
                    """,
                    (utc_now(), row["id"]),
                )
                try:
                    result = self._process_row(row)
                    self._mark_processed(row, result)
                    processed.append(self.get_item(row["id"]))
                except Exception as exc:  # pragma: no cover - defensive branches are covered via behavior tests.
                    item = self._mark_failed(row, exc)
                    if item["status"] == "dead_letter":
                        dead_letter.append(item)
                    else:
                        failed.append(item)
            if commit:
                self._conn.commit()
        return {
            "status": "ok" if not failed and not dead_letter else "partial_failure",
            "processed_count": len(processed),
            "failed_count": len(failed),
            "dead_letter_count": len(dead_letter),
            "processed": processed,
            "failed": failed,
            "dead_letter": dead_letter,
        }

    def rebuild_indexes(
        self,
        *,
        backends: tuple[str, ...] | None = None,
        index_generation: str = ACTIVE_INDEX_GENERATION,
        batch_size: int = 100,
        process: bool = True,
    ) -> JsonDict:
        run_id = new_id("rebuild")
        now = utc_now()
        target_backends = self._normalize_backends(backends)
        backend_label = ",".join(target_backends)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM memory_record
                WHERE status = 'active' AND retrieval_permission = 'allow'
                ORDER BY id ASC
                """
            ).fetchall()
            self._conn.execute(
                """
                INSERT INTO memory_index_rebuild_run (
                    id, backend, index_generation, status, total_count,
                    processed_count, failed_count, watermark_memory_id,
                    payload_json, result_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, 0, 0, NULL, ?, '{}', ?, ?)
                """,
                (
                    run_id,
                    backend_label,
                    index_generation,
                    len(rows),
                    json_dumps({"backends": list(target_backends), "batch_size": batch_size}),
                    now,
                    now,
                ),
            )
            for row in rows:
                memory = self._memory_from_row(row)
                self.enqueue_memory(
                    memory,
                    action="upsert",
                    reason="manual_rebuild",
                    source_ref=run_id,
                    backends=target_backends,
                    index_generation=index_generation,
                )
                self._conn.execute(
                    """
                    UPDATE memory_index_rebuild_run
                    SET processed_count = processed_count + 1,
                        watermark_memory_id = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (memory["id"], utc_now(), run_id),
                )
                if process and (self.get_rebuild_run(run_id)["processed_count"] % batch_size == 0):
                    self.process_pending(limit=batch_size, commit=False)
            if process:
                self.process_pending(limit=max(batch_size, len(rows) * len(target_backends)), commit=False)
            failed_count = int(
                self._conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM memory_index_outbox
                    WHERE json_extract(payload_json, '$.source_ref') = ?
                      AND status IN ('failed', 'dead_letter')
                    """,
                    (run_id,),
                ).fetchone()[0]
            )
            completed = utc_now()
            self._conn.execute(
                """
                UPDATE memory_index_rebuild_run
                SET status = ?, failed_count = ?, result_json = ?, updated_at = ?,
                    completed_at = ?
                WHERE id = ?
                """,
                (
                    "completed" if failed_count == 0 else "completed_with_errors",
                    failed_count,
                    json_dumps({"queued_count": len(rows) * len(target_backends)}),
                    completed,
                    completed,
                    run_id,
                ),
            )
            self._conn.commit()
        return self.get_rebuild_run(run_id)

    def requeue_dead_letters(self, *, limit: int = 50, reason: str = "manual_requeue") -> JsonDict:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM memory_index_outbox
                WHERE status = 'dead_letter'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            now = utc_now()
            for row in rows:
                payload = json_loads(row["payload_json"], {})
                payload.setdefault("requeue_reasons", []).append(reason)
                self._conn.execute(
                    """
                    UPDATE memory_index_outbox
                    SET status = 'pending', error = NULL, error_type = NULL,
                        next_attempt_at = NULL, payload_json = ?
                    WHERE id = ?
                    """,
                    (json_dumps(payload), row["id"]),
                )
            self._conn.commit()
        return {"status": "ok", "requeued_count": len(rows), "ids": [row["id"] for row in rows]}

    def pending_count(self) -> int:
        return self._count_status("pending")

    def failed_count(self) -> int:
        return self._count_status("failed")

    def dead_letter_count(self) -> int:
        return self._count_status("dead_letter")

    def list_items(self, *, status: str | None = None, limit: int = 50) -> list[JsonDict]:
        sql = "SELECT * FROM memory_index_outbox"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._from_row(row) for row in rows]

    def list_rebuild_runs(self, *, limit: int = 20) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM memory_index_rebuild_run
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._rebuild_from_row(row) for row in rows]

    def get_rebuild_run(self, run_id: str) -> JsonDict:
        row = self._conn.execute(
            "SELECT * FROM memory_index_rebuild_run WHERE id = ?",
            (run_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"index rebuild run not found: {run_id}")
        return self._rebuild_from_row(row)

    def get_item(self, outbox_id: str) -> JsonDict:
        row = self._conn.execute(
            "SELECT * FROM memory_index_outbox WHERE id = ?",
            (outbox_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"index outbox item not found: {outbox_id}")
        return self._from_row(row)

    def health(self) -> JsonDict:
        return {
            "pending": self.pending_count(),
            "failed": self.failed_count(),
            "dead_letter": self.dead_letter_count(),
            "index_backends": list(self.index_backends),
            "vector": self.vector_index.health(),
        }

    def _process_row(self, row: sqlite3.Row) -> JsonDict:
        backend = row["backend"]
        action = row["action"]
        memory_id = row["memory_id"]
        payload = json_loads(row["payload_json"], {})
        generation = row["index_generation"] or payload.get("index_generation") or ACTIVE_INDEX_GENERATION
        if backend not in {"fts", "graph"} and not self._is_vector_backend(backend):
            raise PermanentIndexError(f"unknown index backend: {backend}")
        if action == "upsert":
            memory = self._get_memory(memory_id)
            if memory["status"] != "active" or memory["retrieval_permission"] != "allow":
                return self._delete_backend(backend, memory_id, generation, reason="inactive_memory")
            evidence = self._evidence_for_memory(memory_id)
            document = build_index_document(memory, evidence, index_generation=generation)
            self._conn.execute(upsert_index_document_sql(), index_document_params(document))
            if backend == "fts":
                self._sync_fts(
                    memory_id,
                    document["text_for_fts"],
                    memory["type"],
                    memory["tags"],
                )
                result = {
                    "backend": backend,
                    "action": "upsert",
                    "applied": True,
                    "payload_hash": document["payload_hash"],
                    "schema_version": document["schema_version"],
                    "index_generation": generation,
                }
            elif self._is_vector_backend(backend):
                result = self.vector_index.upsert(document)
                result["payload_hash"] = document["payload_hash"]
                result["schema_version"] = document["schema_version"]
            else:
                result = upsert_graph_edges(self._conn, document, index_generation=generation)
                result["payload_hash"] = document["payload_hash"]
                result["schema_version"] = document["schema_version"]
            self._conn.execute(
                "UPDATE memory_record SET index_watermark = ? WHERE id = ?",
                (utc_now(), memory_id),
            )
            return result
        try:
            current = self._get_memory(memory_id)
        except ValueError:
            current = None
        if current is not None and current["status"] == "active" and current["retrieval_permission"] == "allow":
            return {"backend": backend, "action": action, "status": "rejected_policy", "applied": False, "reason": "source_state_superseded"}
        return self._delete_backend(backend, memory_id, generation, reason=action)

    def _delete_backend(
        self,
        backend: str,
        memory_id: str,
        index_generation: str,
        *,
        reason: str,
    ) -> JsonDict:
        if backend == "fts":
            self._remove_from_fts(memory_id)
            return {"backend": backend, "action": "delete", "applied": True, "reason": reason}
        if self._is_vector_backend(backend):
            return self.vector_index.delete(memory_id, index_generation=index_generation)
        if backend == "graph":
            return delete_graph_edges(self._conn, memory_id, index_generation=index_generation)
        raise PermanentIndexError(f"unknown index backend: {backend}")

    def _normalize_backends(self, backends: tuple[str, ...] | None) -> tuple[str, ...]:
        requested = backends or INDEX_BACKENDS
        normalized: list[str] = []
        for backend in requested:
            if self._is_vector_backend(backend):
                backend = self.vector_index.backend_name
            if backend not in normalized:
                normalized.append(backend)
        return tuple(normalized)

    def _is_vector_backend(self, backend: str) -> bool:
        return backend in {"vector", "vector_local", self.vector_index.backend_name}

    def _mark_processed(self, row: sqlite3.Row, result: JsonDict) -> None:
        payload = json_loads(row["payload_json"], {})
        payload["result"] = result
        now = utc_now()
        self._conn.execute(
            """
            UPDATE memory_index_outbox
            SET status = 'processed', attempts = attempts + 1,
                error = NULL, error_type = NULL, next_attempt_at = NULL,
                processed_at = ?, payload_json = ?, payload_hash = ?,
                result_json = ?
            WHERE id = ?
            """,
            (
                now,
                json_dumps(payload),
                result.get("payload_hash") or row["payload_hash"],
                json_dumps(result),
                row["id"],
            ),
        )

    def _mark_failed(self, row: sqlite3.Row, exc: Exception) -> JsonDict:
        attempts = int(row["attempts"]) + 1
        max_attempts = int(row["max_attempts"] or 3)
        permanent = isinstance(exc, PermanentIndexError)
        status = "dead_letter" if permanent or attempts >= max_attempts else "failed"
        error_type = "permanent" if permanent else "transient"
        now = utc_now()
        self._conn.execute(
            """
            UPDATE memory_index_outbox
            SET status = ?, attempts = ?, error = ?, error_type = ?,
                last_attempt_at = ?, next_attempt_at = ?,
                processed_at = CASE WHEN ? = 'dead_letter' THEN ? ELSE processed_at END
            WHERE id = ?
            """,
            (
                status,
                attempts,
                str(exc),
                error_type,
                now,
                None if status == "dead_letter" else now,
                status,
                now,
                row["id"],
            ),
        )
        return self.get_item(row["id"])

    def _count_status(self, status: str) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM memory_index_outbox WHERE status = ?",
                    (status,),
                ).fetchone()[0]
            )

    def _memory_from_row(self, row: sqlite3.Row) -> JsonDict:
        return {
            "id": row["id"],
            "owner_user_id": row["owner_user_id"],
            "agent_id": row["agent_id"],
            "type": row["type"],
            "summary": row["summary"],
            "tags": json_loads(row["tags_json"], []),
            "status": row["status"],
            "confidence": row["confidence"],
            "importance": row["importance"],
            "sensitivity": row["sensitivity"],
            "evidence_ids": [],
            "retrieval_permission": row["retrieval_permission"],
            "use_permission": row["use_permission"],
            "disclosure_permission": row["disclosure_permission"],
            "retention_policy": row["retention_policy"],
            "access_count": row["access_count"],
            "last_accessed_at": row["last_accessed_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "id": row["id"],
            "backend": row["backend"],
            "memory_id": row["memory_id"],
            "action": row["action"],
            "status": row["status"],
            "attempts": row["attempts"],
            "max_attempts": row["max_attempts"],
            "payload_hash": row["payload_hash"],
            "payload": json_loads(row["payload_json"], {}),
            "result": json_loads(row["result_json"], {}),
            "error": row["error"],
            "error_type": row["error_type"],
            "last_attempt_at": row["last_attempt_at"],
            "next_attempt_at": row["next_attempt_at"],
            "index_generation": row["index_generation"],
            "created_at": row["created_at"],
            "processed_at": row["processed_at"],
        }

    @staticmethod
    def _rebuild_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "id": row["id"],
            "backend": row["backend"],
            "index_generation": row["index_generation"],
            "status": row["status"],
            "total_count": row["total_count"],
            "processed_count": row["processed_count"],
            "failed_count": row["failed_count"],
            "watermark_memory_id": row["watermark_memory_id"],
            "payload": json_loads(row["payload_json"], {}),
            "result": json_loads(row["result_json"], {}),
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
        }
