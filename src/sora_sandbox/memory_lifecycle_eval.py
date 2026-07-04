from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from .memory_schema import JsonDict, json_dumps, json_loads, new_id, utc_now


@dataclass
class MemoryLifecycleEvalService:
    conn: sqlite3.Connection
    lock: Any
    run_lifecycle: Callable[..., JsonDict]
    lifecycle_health: Callable[[], JsonDict]
    list_states: Callable[..., list[JsonDict]]
    list_tombstones: Callable[..., list[JsonDict]]

    def run(self) -> JsonDict:
        rows = [
            self._case_heat_scores_in_range(),
            self._case_lifecycle_health_shape(),
            self._case_tombstone_table_shape(),
        ]
        passed = [row for row in rows if row["status"] == "passed"]
        failed = [row for row in rows if row["status"] == "failed"]
        run_id = new_id("lifecycle_eval")
        created_at = utc_now()
        summary = {
            "case_count": len(rows),
            "passed": len(passed),
            "failed": len(failed),
            "pass_rate": round(len(passed) / len(rows), 6) if rows else 1.0,
            "failed_cases": [row["case_name"] for row in failed],
        }
        status = "passed" if not failed else "failed"
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO memory_lifecycle_eval_run (
                    id, status, total_cases, passed_cases, failed_cases,
                    created_at, completed_at, summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, status, len(rows), len(passed), len(failed), created_at, utc_now(), json_dumps(summary)),
            )
            for row in rows:
                self.conn.execute(
                    """
                    INSERT INTO memory_lifecycle_eval_case (
                        id, run_id, case_name, status, details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (new_id("lifecycle_case"), run_id, row["case_name"], row["status"], json_dumps(row), utc_now()),
                )
            self.conn.commit()
        return {
            "run_id": run_id,
            "status": status,
            "total_cases": len(rows),
            "passed_cases": len(passed),
            "failed_cases": len(failed),
            "summary": summary,
            "cases": rows,
        }

    def list_runs(self, *, limit: int = 20) -> list[JsonDict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM memory_lifecycle_eval_run ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "status": row["status"],
                "total_cases": row["total_cases"],
                "passed_cases": row["passed_cases"],
                "failed_cases": row["failed_cases"],
                "summary": json_loads(row["summary_json"], {}),
                "created_at": row["created_at"],
                "completed_at": row["completed_at"],
            }
            for row in rows
        ]

    def _case_heat_scores_in_range(self) -> JsonDict:
        run = self.run_lifecycle(mode="dry_run", limit=50)
        snapshots = run.get("snapshots", [])
        invalid = [
            item
            for item in snapshots
            if not (0.0 <= float(item.get("heat_score", -1.0)) <= 1.0)
            or item.get("tier_after") not in {"hot", "warm", "cold", "archived"}
        ]
        return {
            "case_name": "heat_scores_in_range",
            "status": "passed" if not invalid else "failed",
            "snapshot_count": len(snapshots),
            "invalid": invalid,
        }

    def _case_lifecycle_health_shape(self) -> JsonDict:
        health = self.lifecycle_health()
        passed = "tier_counts" in health and "policy_version" in health and "tombstones" in health
        return {
            "case_name": "lifecycle_health_shape",
            "status": "passed" if passed else "failed",
            "health": health,
        }

    def _case_tombstone_table_shape(self) -> JsonDict:
        tombstones = self.list_tombstones(limit=5)
        valid = all(
            {"id", "target_type", "target_id", "content_hash", "entity_hash", "retention_stage"}.issubset(item)
            for item in tombstones
        )
        return {
            "case_name": "tombstone_table_shape",
            "status": "passed" if valid else "failed",
            "sample_count": len(tombstones),
        }
