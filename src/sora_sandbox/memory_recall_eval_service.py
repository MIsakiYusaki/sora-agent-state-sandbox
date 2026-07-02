from __future__ import annotations

import sqlite3
from typing import Any, Callable

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
        for memory in memories[:5]:
            cases.append(
                {
                    "case_name": f"direct_{memory['id']}",
                    "query": memory["summary"],
                    "expected_memory_ids": [memory["id"]],
                    "forbidden_memory_ids": [],
                }
            )
        cases.append(
            {
                "case_name": "no_answer_smoke",
                "query": "qzxj unlikely token",
                "expected_memory_ids": [],
                "forbidden_memory_ids": [],
            }
        )
        return cases

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
