from __future__ import annotations

import sqlite3
import re
from typing import Any, Callable

from .memory_graph_lite import upsert_graph_edges
from .memory_index_document import build_index_document, index_document_params, upsert_index_document_sql
from .memory_index_fts import index_text
from .memory_schema import JsonDict, json_dumps, json_loads, new_id, utc_now


class MemoryRecallEvalService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        lock: Any,
        recall: Callable[[str], JsonDict],
        list_memories: Callable[..., list[JsonDict]],
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._recall = recall
        self._list_memories = list_memories

    def run(self, cases: list[JsonDict] | None = None) -> JsonDict:
        eval_cases = cases or self._default_cases()
        rows: list[JsonDict] = []
        for case in eval_cases:
            result = self._recall(str(case["query"]))
            retrieved = set(result.get("retrieved_ids", []))
            expected = set(case.get("expected_memory_ids", []))
            forbidden = set(case.get("forbidden_memory_ids", []))
            passed = expected.issubset(retrieved) and not forbidden.intersection(retrieved)
            if not expected and not forbidden:
                passed = not retrieved
            rows.append(
                {
                    "case_name": str(case["case_name"]),
                    "status": "passed" if passed else "failed",
                    "query": case["query"],
                    "expected_memory_ids": sorted(expected),
                    "forbidden_memory_ids": sorted(forbidden),
                    "retrieved_ids": result.get("retrieved_ids", []),
                    "trace_id": result.get("recall_trace_id"),
                    "surface_action": result.get("surface_action"),
                }
            )
        return self._store_run(rows)

    def list_runs(self, *, limit: int = 20) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM memory_recall_eval_run
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._run_from_row(row, include_cases=False) for row in rows]

    def _default_cases(self) -> list[JsonDict]:
        memories = self._list_memories(status="active")
        cases: list[JsonDict] = []
        direct_memories = [
            memory
            for memory in memories
            if "eval_golden" not in memory.get("tags", []) and _stable_direct_query(memory["summary"])
        ]
        for memory in direct_memories[:5]:
            cases.append(
                {
                    "case_name": f"direct_{memory['id']}",
                    "query": memory["summary"],
                    "expected_memory_ids": [memory["id"]],
                    "forbidden_memory_ids": [],
                }
            )
        self._ensure_default_golden_memories()
        cases.append(
            {
                "case_name": "no_answer_smoke",
                "query": "qzxj unlikely token",
                "expected_memory_ids": [],
                "forbidden_memory_ids": self._golden_memory_ids(),
            }
        )
        cases.extend(self._multi_memory_golden_cases())
        return cases

    def _golden_memory_ids(self) -> list[str]:
        return [
            "eval_gradual_pref_1",
            "eval_gradual_pref_2",
            "eval_shared_episode_lighthouse",
            "eval_conflict_current_coffee",
            "eval_conflict_old_tea",
            "eval_graph_ada_project_nyx",
        ]

    def _multi_memory_golden_cases(self) -> list[JsonDict]:
        return [
            {
                "case_name": "golden_gradual_preference_multi_memory",
                "query": "gradual preference quiet companion pacing",
                "expected_memory_ids": ["eval_gradual_pref_1", "eval_gradual_pref_2"],
                "forbidden_memory_ids": [],
            },
            {
                "case_name": "golden_shared_episode",
                "query": "remember shared lighthouse debugging episode",
                "expected_memory_ids": ["eval_shared_episode_lighthouse"],
                "forbidden_memory_ids": [],
            },
            {
                "case_name": "golden_conflict_prefers_current_memory",
                "query": "current caffeine preference coffee",
                "expected_memory_ids": ["eval_conflict_current_coffee"],
                "forbidden_memory_ids": ["eval_conflict_old_tea"],
            },
            {
                "case_name": "golden_graph_entity_related_memory",
                "query": "who is related to ada project_nyx",
                "expected_memory_ids": ["eval_graph_ada_project_nyx"],
                "forbidden_memory_ids": [],
            },
            {
                "case_name": "golden_negative_empty_or_proactive_no_memory",
                "query": "",
                "expected_memory_ids": [],
                "forbidden_memory_ids": self._golden_memory_ids(),
            },
        ]

    def _ensure_default_golden_memories(self) -> None:
        fixtures = [
            {
                "id": "eval_gradual_pref_1",
                "type": "preference",
                "summary": "User gradually prefers quiet companion pacing during late night work.",
                "tags": ["eval_golden", "gradual_preference", "quiet_companion"],
                "importance": 0.72,
            },
            {
                "id": "eval_gradual_pref_2",
                "type": "preference",
                "summary": "User again prefers quiet companion pacing when focus is fragile.",
                "tags": ["eval_golden", "gradual_preference", "quiet_companion"],
                "importance": 0.74,
            },
            {
                "id": "eval_shared_episode_lighthouse",
                "type": "shared_episode",
                "summary": "Sora and the user shared a lighthouse debugging episode together.",
                "tags": ["eval_golden", "shared_episode", "lighthouse", "debugging"],
                "importance": 0.82,
            },
            {
                "id": "eval_conflict_old_tea",
                "type": "preference",
                "summary": "Old caffeine preference was tea during morning planning.",
                "tags": ["eval_golden", "conflict", "tea"],
                "importance": 0.45,
                "status": "disabled",
            },
            {
                "id": "eval_conflict_current_coffee",
                "type": "preference",
                "summary": "Current caffeine preference is coffee during morning planning.",
                "tags": ["eval_golden", "conflict", "coffee", "current"],
                "importance": 0.86,
            },
            {
                "id": "eval_graph_ada_project_nyx",
                "type": "relationship_signal",
                "summary": "Ada is related to Project Nyx handoff discussions with the user.",
                "tags": ["eval_golden", "ada", "project_nyx", "relationship"],
                "importance": 0.78,
            },
        ]
        with self._lock:
            for fixture in fixtures:
                self._upsert_eval_memory(fixture)
            self._conn.commit()

    def _upsert_eval_memory(self, fixture: JsonDict) -> None:
        now = utc_now()
        memory_id = str(fixture["id"])
        tags = [str(tag) for tag in fixture.get("tags", [])]
        self._conn.execute(
            """
            INSERT INTO memory_record (
                id, owner_user_id, agent_id, type, summary, tags_json, status,
                confidence, importance, sensitivity, supersedes_id, valid_from,
                valid_until, retention_policy, retrieval_permission,
                use_permission, disclosure_permission, schema_version,
                index_watermark, access_count, last_accessed_at, created_at, updated_at
            ) VALUES (
                ?, 'local-user', 'sora_default', ?, ?, ?, ?,
                0.94, ?, 'low', NULL, ?, NULL, 'persistent', 'allow',
                'allow', 'gist_only', 1, ?, 0, NULL, ?, ?
            )
            ON CONFLICT(id) DO UPDATE SET
                type = excluded.type,
                summary = excluded.summary,
                tags_json = excluded.tags_json,
                status = excluded.status,
                confidence = excluded.confidence,
                importance = excluded.importance,
                sensitivity = excluded.sensitivity,
                retrieval_permission = 'allow',
                use_permission = 'allow',
                disclosure_permission = 'gist_only',
                updated_at = excluded.updated_at
            """,
            (
                memory_id,
                fixture["type"],
                fixture["summary"],
                json_dumps(tags),
                str(fixture.get("status") or "active"),
                float(fixture.get("importance", 0.7)),
                now,
                now,
                now,
                now,
            ),
        )
        self._conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
        self._conn.execute(
            "INSERT INTO memory_fts (memory_id, search_text) VALUES (?, ?)",
            (memory_id, index_text(str(fixture["summary"]), str(fixture["type"]), tags)),
        )
        row = self._conn.execute("SELECT * FROM memory_record WHERE id = ?", (memory_id,)).fetchone()
        memory = {
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
        document = build_index_document(memory, [], index_generation="active")
        self._conn.execute(upsert_index_document_sql(), index_document_params(document))
        upsert_graph_edges(self._conn, document, index_generation="active")

    def _store_run(self, rows: list[JsonDict]) -> JsonDict:
        run_id = new_id("recall_eval")
        now = utc_now()
        passed = sum(1 for row in rows if row["status"] == "passed")
        failed = len(rows) - passed
        status = "passed" if failed == 0 else "failed"
        summary = {
            "case_count": len(rows),
            "passed": passed,
            "failed": failed,
            "pass_rate": round(passed / len(rows), 6) if rows else 1.0,
        }
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_recall_eval_run (
                    id, status, total_cases, passed_cases, failed_cases,
                    created_at, completed_at, summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, status, len(rows), passed, failed, now, now, json_dumps(summary)),
            )
            for row in rows:
                self._conn.execute(
                    """
                    INSERT INTO memory_recall_eval_case (
                        id, run_id, case_name, status, details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("recall_case"),
                        run_id,
                        row["case_name"],
                        row["status"],
                        json_dumps(row),
                        now,
                    ),
                )
            self._conn.commit()
            stored = self._conn.execute(
                "SELECT * FROM memory_recall_eval_run WHERE id = ?",
                (run_id,),
            ).fetchone()
        return self._run_from_row(stored, include_cases=True)

    def _run_from_row(self, row: sqlite3.Row, *, include_cases: bool) -> JsonDict:
        result = {
            "id": row["id"],
            "status": row["status"],
            "total_cases": row["total_cases"],
            "passed_cases": row["passed_cases"],
            "failed_cases": row["failed_cases"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
            "summary": json_loads(row["summary_json"], {}),
        }
        if include_cases:
            case_rows = self._conn.execute(
                """
                SELECT * FROM memory_recall_eval_case
                WHERE run_id = ?
                ORDER BY created_at ASC, case_name ASC
                """,
                (row["id"],),
            ).fetchall()
            result["cases"] = [
                {
                    "id": case["id"],
                    "case_name": case["case_name"],
                    "status": case["status"],
                    "details": json_loads(case["details_json"], {}),
                    "created_at": case["created_at"],
                }
                for case in case_rows
            ]
        return result


def _stable_direct_query(summary: str) -> bool:
    return len(re.findall(r"[A-Za-z0-9]", str(summary))) >= 3
