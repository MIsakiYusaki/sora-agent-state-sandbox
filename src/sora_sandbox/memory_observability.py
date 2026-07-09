from __future__ import annotations

import sqlite3
from typing import Any, Callable

from .memory_schema import JsonDict


class MemoryObservabilityService:
    """Aggregates canonical store health across audit, indexing, retention, and projectors."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        lock: Any,
        store_audit: Callable[[], JsonDict],
        index_pending_count: Callable[[], int],
        index_failed_count: Callable[[], int],
        index_dead_letter_count: Callable[[], int],
        index_health: Callable[[], JsonDict],
        projector_pending_count: Callable[[], int],
        projector_failed_count: Callable[[], int],
        projector_dead_letter_count: Callable[[], int],
        retention_backlog_count: Callable[[], int],
        lifecycle_health: Callable[[], JsonDict],
        projector_checkpoints: Callable[[], list[JsonDict]],
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._store_audit = store_audit
        self._index_pending_count = index_pending_count
        self._index_failed_count = index_failed_count
        self._index_dead_letter_count = index_dead_letter_count
        self._index_health = index_health
        self._projector_pending_count = projector_pending_count
        self._projector_failed_count = projector_failed_count
        self._projector_dead_letter_count = projector_dead_letter_count
        self._retention_backlog_count = retention_backlog_count
        self._lifecycle_health = lifecycle_health
        self._projector_checkpoints = projector_checkpoints

    def store_health(self) -> JsonDict:
        audit = self._store_audit()
        index_pending = self._index_pending_count()
        index_failed = self._index_failed_count()
        index_dead_letter = self._index_dead_letter_count()
        index_health = self._index_health()
        projector_pending = self._projector_pending_count()
        projector_failed = self._projector_failed_count()
        projector_dead_letter = self._projector_dead_letter_count()
        retention_backlog = self._retention_backlog_count()
        lifecycle = self._lifecycle_health()
        with self._lock:
            active_memories = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM memory_record WHERE status = 'active'"
                ).fetchone()[0]
            )
            deleted_evidence = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM evidence WHERE deleted_at IS NOT NULL"
                ).fetchone()[0]
            )
            forget_requests = int(
                self._conn.execute("SELECT COUNT(*) FROM memory_forget_request").fetchone()[0]
            )
        blockers = []
        if audit["issue_count"]:
            blockers.append("store_audit_issues")
        if index_failed:
            blockers.append("index_outbox_failed")
        if index_dead_letter:
            blockers.append("index_outbox_dead_letter")
        if projector_failed:
            blockers.append("projector_failed")
        if projector_dead_letter:
            blockers.append("projector_dead_letter")
        warnings = []
        if index_pending:
            warnings.append("index_outbox_pending")
        if projector_pending:
            warnings.append("projector_pending")
        if retention_backlog:
            warnings.append("retention_backlog")
        if lifecycle.get("pending_consolidation_plans"):
            warnings.append("lifecycle_consolidation_pending")
        status = "error" if blockers else ("warn" if warnings else "ok")
        return {
            "status": status,
            "blockers": blockers,
            "warnings": warnings,
            "active_memories": active_memories,
            "deleted_evidence": deleted_evidence,
            "forget_requests": forget_requests,
            "audit": {
                "status": audit["status"],
                "issue_count": audit["issue_count"],
                "issue_breakdown": audit.get("issue_breakdown", {}),
            },
            "index_outbox": {
                "pending": index_pending,
                "failed": index_failed,
                "dead_letter": index_dead_letter,
                "health": index_health,
            },
            "projectors": {
                "pending": projector_pending,
                "failed": projector_failed,
                "dead_letter": projector_dead_letter,
                "checkpoints": self._projector_checkpoints(),
            },
            "retention": {
                "backlog": retention_backlog,
            },
            "lifecycle": lifecycle,
        }
