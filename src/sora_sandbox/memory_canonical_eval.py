from __future__ import annotations

import sqlite3
from typing import Any, Callable

from .memory_schema import JsonDict, json_dumps, json_loads, new_id, utc_now


class MemoryCanonicalEvalService:
    """Runtime eval harness for canonical store invariants."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        lock: Any,
        store_health: Callable[[], JsonDict],
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._store_health = store_health

    def run(self) -> JsonDict:
        health = self._store_health()
        cases = [
            self._case(
                "store_audit_clean",
                health["audit"]["issue_count"] == 0,
                health["audit"],
            ),
            self._case(
                "index_outbox_drained",
                health["index_outbox"]["pending"] == 0 and health["index_outbox"]["failed"] == 0,
                health["index_outbox"],
            ),
            self._case(
                "projector_outbox_drained",
                health["projectors"]["pending"] == 0 and health["projectors"]["failed"] == 0,
                health["projectors"],
            ),
            self._case(
                "retention_backlog_empty",
                health["retention"]["backlog"] == 0,
                health["retention"],
            ),
        ]
        passed = sum(1 for case in cases if case["status"] == "passed")
        failed = len(cases) - passed
        run_id = new_id("ceval")
        status = "passed" if failed == 0 else "failed"
        with self._lock:
            now = utc_now()
            self._conn.execute(
                """
                INSERT INTO memory_canonical_eval_run (
                    id, status, total_cases, passed_cases, failed_cases,
                    created_at, completed_at, summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    status,
                    len(cases),
                    passed,
                    failed,
                    now,
                    now,
                    json_dumps({"health_status": health["status"], "blockers": health["blockers"]}),
                ),
            )
            for case in cases:
                self._conn.execute(
                    """
                    INSERT INTO memory_canonical_eval_case (
                        id, run_id, case_name, status, details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("cecase"),
                        run_id,
                        case["case_name"],
                        case["status"],
                        json_dumps(case["details"]),
                        now,
                    ),
                )
            self._conn.commit()
        return self.get_run(run_id)

    def list_runs(self, *, limit: int = 20) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM memory_canonical_eval_run
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def get_run(self, run_id: str) -> JsonDict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_canonical_eval_run WHERE id = ?",
                (run_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"canonical eval run not found: {run_id}")
            case_rows = self._conn.execute(
                """
                SELECT * FROM memory_canonical_eval_case
                WHERE run_id = ?
                ORDER BY created_at ASC
                """,
                (run_id,),
            ).fetchall()
        run = self._run_from_row(row)
        run["cases"] = [self._case_from_row(case_row) for case_row in case_rows]
        return run

    @staticmethod
    def _case(case_name: str, passed: bool, details: JsonDict) -> JsonDict:
        return {
            "case_name": case_name,
            "status": "passed" if passed else "failed",
            "details": details,
        }

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "id": row["id"],
            "status": row["status"],
            "total_cases": row["total_cases"],
            "passed_cases": row["passed_cases"],
            "failed_cases": row["failed_cases"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
            "summary": json_loads(row["summary_json"], {}),
        }

    @staticmethod
    def _case_from_row(row: sqlite3.Row) -> JsonDict:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "case_name": row["case_name"],
            "status": row["status"],
            "details": json_loads(row["details_json"], {}),
            "created_at": row["created_at"],
        }
