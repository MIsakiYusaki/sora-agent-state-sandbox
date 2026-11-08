from __future__ import annotations

import sqlite3
from typing import Any, Callable

from .memory_index_fts import index_text as _index_text
from .memory_schema import JsonDict, utc_now as _utc_now
from .memory_store import evidence_from_row


class MemoryCanonicalStore:
    """Canonical memory/evidence store governance and audit helpers."""

    _COUNT_QUERIES = {
        ("memory_record", None): "SELECT COUNT(*) FROM memory_record",
        ("memory_record", "status = 'active'"): (
            "SELECT COUNT(*) FROM memory_record WHERE status = 'active'"
        ),
        ("evidence", None): "SELECT COUNT(*) FROM evidence",
        ("evidence", "deleted_at IS NULL"): (
            "SELECT COUNT(*) FROM evidence WHERE deleted_at IS NULL"
        ),
        ("evidence", "deleted_at IS NOT NULL"): (
            "SELECT COUNT(*) FROM evidence WHERE deleted_at IS NOT NULL"
        ),
        ("memory_evidence", None): "SELECT COUNT(*) FROM memory_evidence",
    }

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        lock: Any,
        get_memory: Callable[[str], JsonDict],
        get_candidate: Callable[[str], JsonDict],
        append_revision: Callable[..., None],
        sync_fts: Callable[[str, str, str, list[str]], None],
        remove_from_fts: Callable[[str], None],
        record_delete_propagation: Callable[..., JsonDict],
        publish_memory_change: Callable[..., None],
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._get_memory = get_memory
        self._get_candidate = get_candidate
        self._append_revision = append_revision
        self._sync_fts = sync_fts
        self._remove_from_fts = remove_from_fts
        self._record_delete_propagation = record_delete_propagation
        self._publish_memory_change = publish_memory_change

    def get_evidence(self, evidence_id: str) -> JsonDict:
        with self._lock:
            return self._get_evidence_unlocked(evidence_id)

    def list_evidence(
        self,
        *,
        status: str | None = "active",
        source_type: str | None = None,
        sensitivity: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[JsonDict]:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        sql = "SELECT * FROM evidence WHERE 1 = 1"
        params: list[Any] = []
        if status == "active":
            sql += " AND deleted_at IS NULL"
        elif status == "deleted":
            sql += " AND deleted_at IS NOT NULL"
        elif status not in {None, "all"}:
            raise ValueError("status must be active, deleted, all, or None")
        if source_type:
            sql += " AND source_type = ?"
            params.append(source_type)
        if sensitivity:
            sql += " AND sensitivity = ?"
            params.append(sensitivity)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [evidence_from_row(row) for row in rows]

    def find_evidence_by_source(self, *, source_type: str, source_ref: str) -> JsonDict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM evidence
                WHERE source_type = ? AND source_ref = ? AND deleted_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (source_type, source_ref),
            ).fetchone()
        return evidence_from_row(row) if row is not None else None

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

    def link_evidence(
        self,
        memory_id: str,
        evidence_id: str,
        *,
        reason: str = "manual_link_evidence",
        actor: str = "manual_repl",
    ) -> JsonDict:
        with self._lock, self._conn:
            before = self._get_memory(memory_id)
            self._assert_evidence_link_allowed(before)
            evidence = self._get_evidence_unlocked(evidence_id)
            if evidence.get("deleted_at"):
                raise ValueError(f"cannot link deleted evidence: {evidence_id}")
            self._conn.execute(
                "INSERT OR IGNORE INTO memory_evidence (memory_id, evidence_id) VALUES (?, ?)",
                (memory_id, evidence_id),
            )
            after = self._get_memory(memory_id)
            self._append_revision(
                memory_id,
                action="evidence_linked",
                before=before,
                after=after,
                reason=reason,
                actor=actor,
            )
            self._conn.commit()
        return {"status": "linked", "memory": after, "evidence": evidence}

    def unlink_evidence(
        self,
        memory_id: str,
        evidence_id: str,
        *,
        reason: str = "manual_unlink_evidence",
        actor: str = "manual_repl",
        disable_if_empty: bool = True,
    ) -> JsonDict:
        with self._lock, self._conn:
            before = self._get_memory(memory_id)
            self._get_evidence_unlocked(evidence_id)
            self._conn.execute(
                "DELETE FROM memory_evidence WHERE memory_id = ? AND evidence_id = ?",
                (memory_id, evidence_id),
            )
            after = self._get_memory(memory_id)
            self._append_revision(
                memory_id,
                action="evidence_unlinked",
                before=before,
                after=after,
                reason=reason,
                actor=actor,
            )
            disabled = None
            if disable_if_empty and self._active_evidence_count(memory_id) == 0:
                disabled = self._disable_memory_without_evidence(
                    memory_id,
                    reason=f"{reason}:no_active_evidence",
                    actor=actor,
                )
            self._conn.commit()
        return {
            "status": "unlinked",
            "memory": disabled or self._get_memory(memory_id),
            "disabled": disabled,
        }

    def soft_delete_evidence(
        self,
        evidence_id: str,
        *,
        reason: str = "manual_evidence_delete",
        actor: str = "manual_repl",
    ) -> JsonDict:
        with self._lock, self._conn:
            evidence = self._get_evidence_unlocked(evidence_id)
            if evidence.get("deleted_at"):
                return {
                    "status": "already_deleted",
                    "evidence": evidence,
                    "rejected_candidates": [],
                    "disabled_memories": [],
                }
            now = _utc_now()
            self._conn.execute(
                "UPDATE evidence SET deleted_at = ?, delete_reason = ? WHERE id = ?",
                (now, reason, evidence_id),
            )
            self._record_delete_propagation(
                source_ref=evidence_id,
                source_type="evidence",
                target_type="evidence",
                target_id=evidence_id,
                action="soft_delete",
                reason=reason,
                metadata={"actor": actor},
            )
            rejected_candidate_ids = self._reject_candidates_for_evidence(evidence_id, reason)
            disabled_memories = self._disable_memories_without_active_evidence(
                evidence_id,
                reason=reason,
                actor=actor,
            )
            deleted_evidence = self._get_evidence_unlocked(evidence_id)
            self._conn.commit()
        rejected_candidates = [
            self._get_candidate(candidate_id) for candidate_id in rejected_candidate_ids
        ]
        return {
            "status": "deleted",
            "evidence": deleted_evidence,
            "rejected_candidates": rejected_candidates,
            "disabled_memories": disabled_memories,
        }

    def store_audit(self) -> JsonDict:
        with self._lock:
            dangling_links = self._rows(
                """
                SELECT me.memory_id, me.evidence_id
                FROM memory_evidence me
                LEFT JOIN memory_record m ON m.id = me.memory_id
                LEFT JOIN evidence e ON e.id = me.evidence_id
                WHERE m.id IS NULL OR e.id IS NULL
                """
            )
            active_without_evidence = self._rows(
                """
                SELECT m.id
                FROM memory_record m
                WHERE m.status = 'active'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM memory_evidence me
                    JOIN evidence e ON e.id = me.evidence_id
                    WHERE me.memory_id = m.id AND e.deleted_at IS NULL
                  )
                ORDER BY m.updated_at DESC
                """
            )
            orphan_evidence = self._rows(
                """
                SELECT e.id
                FROM evidence e
                WHERE e.deleted_at IS NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM memory_evidence me WHERE me.evidence_id = e.id
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM memory_candidate c, json_each(c.evidence_ids_json) j
                    WHERE j.value = e.id
                  )
                ORDER BY e.created_at DESC
                """
            )
            fts_missing = self._rows(
                """
                SELECT m.id
                FROM memory_record m
                LEFT JOIN memory_fts f ON f.memory_id = m.id
                WHERE m.status = 'active' AND f.memory_id IS NULL
                ORDER BY m.updated_at DESC
                """
            )
            fts_orphan = self._rows(
                """
                SELECT f.memory_id
                FROM memory_fts f
                LEFT JOIN memory_record m ON m.id = f.memory_id
                WHERE m.id IS NULL OR m.status != 'active'
                """
            )
            counts = {
                "active_memories": self._count("memory_record", "status = 'active'"),
                "all_memories": self._count("memory_record"),
                "active_evidence": self._count("evidence", "deleted_at IS NULL"),
                "deleted_evidence": self._count("evidence", "deleted_at IS NOT NULL"),
                "memory_evidence_links": self._count("memory_evidence"),
            }
        issues = {
            "dangling_memory_evidence_links": dangling_links,
            "active_memories_without_active_evidence": active_without_evidence,
            "orphan_active_evidence": orphan_evidence,
            "fts_missing_active_memories": fts_missing,
            "fts_orphan_or_inactive_memories": fts_orphan,
        }
        issue_breakdown = {name: len(items) for name, items in issues.items()}
        issue_count = sum(len(items) for items in issues.values())
        return {
            "status": "ok" if issue_count == 0 else "issues_found",
            "issue_count": issue_count,
            "issue_breakdown": issue_breakdown,
            "counts": counts,
            "issues": issues,
        }

    def repair_fts_index(self, *, actor: str = "store_audit") -> JsonDict:
        with self._lock:
            try:
                active_rows = self._conn.execute(
                    "SELECT * FROM memory_record WHERE status = 'active'"
                ).fetchall()
                active_ids = {row["id"] for row in active_rows}
                fts_rows = self._conn.execute(
                    "SELECT memory_id, search_text FROM memory_fts"
                ).fetchall()
                fts_by_memory: dict[str, list[str]] = {}
                for row in fts_rows:
                    fts_by_memory.setdefault(row["memory_id"], []).append(row["search_text"])

                removed = sorted(memory_id for memory_id in fts_by_memory if memory_id not in active_ids)
                for memory_id in removed:
                    self._remove_from_fts(memory_id)

                repaired: list[str] = []
                for row in active_rows:
                    tags = _loads_json(row["tags_json"], [])
                    expected_text = _index_text(row["summary"], row["type"], tags)
                    existing_texts = fts_by_memory.get(row["id"], [])
                    if len(existing_texts) == 1 and existing_texts[0] == expected_text:
                        continue
                    memory = {
                        "id": row["id"],
                        "summary": row["summary"],
                        "type": row["type"],
                        "tags": tags,
                    }
                    self._sync_fts(memory["id"], memory["summary"], memory["type"], memory["tags"])
                    self._append_revision(
                        memory["id"],
                        action="fts_repaired",
                        before=None,
                        after=memory,
                        reason="repair_fts_index",
                        actor=actor,
                    )
                    repaired.append(memory["id"])
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {
            "status": "repaired",
            "repaired_count": len(repaired),
            "removed_count": len(removed),
            "memory_ids": repaired,
            "removed_memory_ids": removed,
        }

    def _reject_candidates_for_evidence(self, evidence_id: str, reason: str) -> list[str]:
        rejected: list[str] = []
        rows = self._conn.execute(
            """
            SELECT id
            FROM memory_candidate
            WHERE status IN ('pending', 'needs_review', 'deferred')
              AND EXISTS (
                SELECT 1
                FROM json_each(memory_candidate.evidence_ids_json) j
                WHERE j.value = ?
              )
            ORDER BY created_at DESC
            """,
            (evidence_id,),
        ).fetchall()
        for row in rows:
            candidate_id = row["id"]
            self._conn.execute(
                """
                UPDATE memory_candidate
                SET status = 'rejected', resolved_at = ?, reason = ?
                WHERE id = ?
                """,
                (_utc_now(), f"{reason}:source_evidence_deleted", candidate_id),
            )
            self._record_delete_propagation(
                source_ref=evidence_id,
                source_type="evidence",
                target_type="memory_candidate",
                target_id=candidate_id,
                action="reject",
                reason=reason,
                metadata={"affected_evidence_id": evidence_id},
            )
            rejected.append(candidate_id)
        return rejected

    def _disable_memories_without_active_evidence(
        self,
        evidence_id: str,
        *,
        reason: str,
        actor: str,
    ) -> list[JsonDict]:
        rows = self._conn.execute(
            "SELECT DISTINCT memory_id FROM memory_evidence WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchall()
        disabled: list[JsonDict] = []
        for row in rows:
            memory_id = row["memory_id"]
            memory = self._get_memory(memory_id)
            if memory["status"] != "active" or self._active_evidence_count(memory_id) != 0:
                continue
            disabled.append(
                self._disable_memory_without_evidence(
                    memory_id,
                    reason=f"{reason}:no_active_evidence",
                    actor=actor,
                    source_ref=evidence_id,
                )
            )
        return disabled

    def _disable_memory_without_evidence(
        self,
        memory_id: str,
        *,
        reason: str,
        actor: str,
        source_ref: str | None = None,
    ) -> JsonDict:
        before = self._get_memory(memory_id)
        self._conn.execute(
            "UPDATE memory_record SET status = 'disabled', updated_at = ? WHERE id = ?",
            (_utc_now(), memory_id),
        )
        self._remove_from_fts(memory_id)
        after = self._get_memory(memory_id)
        self._append_revision(
            memory_id,
            action="disable",
            before=before,
            after=after,
            reason=reason,
            actor=actor,
        )
        self._record_delete_propagation(
            source_ref=source_ref or memory_id,
            source_type="evidence",
            target_type="memory_record",
            target_id=memory_id,
            action="disable",
            reason=reason,
            metadata={"active_evidence_count": 0},
        )
        self._record_delete_propagation(
            source_ref=source_ref or memory_id,
            source_type="evidence",
            target_type="memory_fts",
            target_id=memory_id,
            action="remove_index",
            reason=reason,
            metadata={},
        )
        self._publish_memory_change(
            after,
            action="tombstone",
            reason=reason,
            source_ref=source_ref,
        )
        return after

    def _active_evidence_count(self, memory_id: str) -> int:
        return int(
            self._conn.execute(
                """
                SELECT COUNT(*)
                FROM memory_evidence me
                JOIN evidence e ON e.id = me.evidence_id
                WHERE me.memory_id = ? AND e.deleted_at IS NULL
                """,
                (memory_id,),
            ).fetchone()[0]
        )

    def _rows(self, sql: str) -> list[JsonDict]:
        return [dict(row) for row in self._conn.execute(sql).fetchall()]

    def _count(self, table: str, where: str | None = None) -> int:
        sql = self._COUNT_QUERIES.get((table, where))
        if sql is None:
            raise ValueError(f"unsupported count query: {table} {where or ''}".strip())
        return int(self._conn.execute(sql).fetchone()[0])

    def _get_evidence_unlocked(self, evidence_id: str) -> JsonDict:
        row = self._conn.execute(
            "SELECT * FROM evidence WHERE id = ?",
            (evidence_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"evidence not found: {evidence_id}")
        return evidence_from_row(row)

    @staticmethod
    def _assert_evidence_link_allowed(memory: JsonDict) -> None:
        if memory.get("retrieval_permission") != "allow":
            raise ValueError(
                "cannot link evidence to memory with "
                f"retrieval_permission={memory.get('retrieval_permission')}: {memory.get('id')}"
            )
        if memory.get("use_permission") not in {"allow", "allow_implicit"}:
            raise ValueError(
                "cannot link evidence to memory with "
                f"use_permission={memory.get('use_permission')}: {memory.get('id')}"
            )


def _loads_json(value: str, default: Any) -> Any:
    import json

    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default
