from __future__ import annotations

import sqlite3
from typing import Any, Callable

from .memory_schema import JsonDict, json_dumps, json_loads, new_id, utc_now


class MemoryRecallPlannerEvalService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        lock: Any,
        plan_recall: Callable[..., JsonDict],
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._plan_recall = plan_recall

    def run(self, cases: list[JsonDict] | None = None) -> JsonDict:
        eval_cases = cases or self._default_cases()
        rows: list[JsonDict] = []
        for case in eval_cases:
            plan = self._plan_recall(
                str(case.get("query", "")),
                turn_frame=case.get("turn_frame"),
                context_use_policy=case.get("context_use_policy"),
                top_k=int(case.get("top_k") or 5),
                planner_context=case.get("planner_context"),
            )
            checks = _evaluate_plan(plan, case)
            rows.append(
                {
                    "case_name": str(case["case_name"]),
                    "status": "passed" if checks["passed"] else "failed",
                    "query": case.get("query", ""),
                    "expected": {
                        key: case.get(key)
                        for key in (
                            "expected_decision",
                            "expected_intent",
                            "expected_backends",
                            "expected_memory_types",
                            "expected_skip_reason",
                        )
                    },
                    "plan": plan,
                    "checks": checks,
                }
            )
        return self._store_run(rows)

    def list_runs(self, *, limit: int = 20) -> list[JsonDict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM memory_planner_eval_run
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._run_from_row(row, include_cases=False) for row in rows]

    def _default_cases(self) -> list[JsonDict]:
        return [
            {
                "case_name": "explicit_memory_lookup",
                "query": "Do you remember what we said last time about my writing workflow?",
                "expected_decision": "deep_recall",
                "expected_intent": "explicit_memory_lookup",
                "expected_backends": ["fts", "vector", "temporal", "graph"],
            },
            {
                "case_name": "domain_continuity",
                "query": "继续推进我的创作工作流设计",
                "turn_frame": {"scene": "planning", "recent_topics": ["创作工作流", "角色设定"]},
                "expected_decision": "light_recall",
                "expected_intent": "domain_continuity",
                "expected_memory_types": ["domain_principle"],
            },
            {
                "case_name": "preference_lookup",
                "query": "我是不是更喜欢当前最优方案，而不是最小闭环？",
                "expected_decision": "light_recall",
                "expected_intent": "preference_lookup",
                "expected_memory_types": ["preference"],
            },
            {
                "case_name": "relationship_context",
                "query": "陪伴项目里我们的关系节奏之前怎么定的？",
                "expected_decision": "light_recall",
                "expected_intent": "relationship_context",
                "expected_memory_types": ["relationship_signal", "shared_episode"],
                "expected_backends": ["vector", "temporal", "graph"],
            },
            {
                "case_name": "negative_no_recall",
                "query": "hello",
                "expected_decision": "skip",
                "expected_intent": "negative_or_no_recall",
            },
            {
                "case_name": "write_related_memory",
                "query": "User wants current-best architecture instead of minimal loops.",
                "planner_context": {"mode": "write_related_memory"},
                "expected_decision": "deep_recall",
                "expected_intent": "write_related_memory",
                "expected_backends": ["fts", "vector", "temporal", "graph"],
            },
            {
                "case_name": "prefetch_domain_context",
                "query": "next steps for my writing workflow design",
                "planner_context": {"mode": "prefetch"},
                "expected_decision": "prefetch_only",
                "expected_intent": "domain_continuity",
            },
            {
                "case_name": "blocked_by_policy",
                "query": "remember our previous decision",
                "context_use_policy": {"blocked_context_sources": ["memory_recollection"]},
                "expected_decision": "skip",
                "expected_skip_reason": "memory_recollection_blocked_by_context_policy",
            },
        ]

    def _store_run(self, rows: list[JsonDict]) -> JsonDict:
        run_id = new_id("planner_eval")
        now = utc_now()
        passed = sum(1 for row in rows if row["status"] == "passed")
        failed = len(rows) - passed
        summary = {
            "case_count": len(rows),
            "passed": passed,
            "failed": failed,
            "pass_rate": round(passed / len(rows), 6) if rows else 1.0,
            "decision_accuracy": _rate(rows, "decision"),
            "intent_accuracy": _rate(rows, "intent"),
            "scope_accuracy": _rate(rows, "scope"),
            "trace_completeness": _rate(rows, "trace"),
        }
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_planner_eval_run (
                    id, status, total_cases, passed_cases, failed_cases,
                    created_at, completed_at, summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    "passed" if failed == 0 else "failed",
                    len(rows),
                    passed,
                    failed,
                    now,
                    now,
                    json_dumps(summary),
                ),
            )
            for row in rows:
                self._conn.execute(
                    """
                    INSERT INTO memory_planner_eval_case (
                        id, run_id, case_name, status, details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("planner_case"),
                        run_id,
                        row["case_name"],
                        row["status"],
                        json_dumps(row),
                        now,
                    ),
                )
            self._conn.commit()
            stored = self._conn.execute(
                "SELECT * FROM memory_planner_eval_run WHERE id = ?",
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
                SELECT * FROM memory_planner_eval_case
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


def _evaluate_plan(plan: JsonDict, case: JsonDict) -> JsonDict:
    expected_decision = case.get("expected_decision")
    expected_intent = case.get("expected_intent")
    expected_backends = set(case.get("expected_backends") or [])
    expected_memory_types = set(case.get("expected_memory_types") or [])
    expected_skip_reason = case.get("expected_skip_reason")
    subqueries = plan.get("subqueries", [])
    actual_memory_types = {
        memory_type
        for subquery in subqueries
        for memory_type in subquery.get("memory_types", [])
    }
    actual_backends = set(plan.get("backends", []))
    checks = {
        "decision": not expected_decision or plan.get("decision") == expected_decision,
        "intent": not expected_intent or plan.get("query_intent") == expected_intent,
        "backends": not expected_backends or expected_backends.issubset(actual_backends),
        "scope": not expected_memory_types or bool(expected_memory_types.intersection(actual_memory_types)),
        "skip_reason": not expected_skip_reason or plan.get("skip_reason") == expected_skip_reason,
        "trace": bool(plan.get("plan_id")) and bool(plan.get("planner_version")) and "decision_reasons" in plan,
    }
    checks["passed"] = all(checks.values())
    return checks


def _rate(rows: list[JsonDict], key: str) -> float:
    if not rows:
        return 1.0
    return round(
        sum(1 for row in rows if row.get("checks", {}).get(key)) / len(rows),
        6,
    )
