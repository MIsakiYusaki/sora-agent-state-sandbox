from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .memory_context_packer import PackedMemory
from .memory_schema import JsonDict, RecallBudget, RecallPlan, json_dumps, new_id, utc_now
from .memory_surface_policy import SurfacePolicyEngine


@dataclass
class MemorySurfaceEvalService:
    conn: sqlite3.Connection
    lock: Any
    surface_policy: SurfacePolicyEngine

    def run(self, cases: list[JsonDict] | None = None) -> JsonDict:
        rows = [self._evaluate_case(case) for case in (cases or default_surface_eval_cases())]
        passed = [row for row in rows if row["status"] == "passed"]
        failed = [row for row in rows if row["status"] == "failed"]
        run_id = new_id("surface_eval")
        created_at = utc_now()
        summary = {
            "action_accuracy": round(len(passed) / len(rows), 6) if rows else 1.0,
            "failed_cases": [row["case_name"] for row in failed],
        }
        status = "passed" if not failed else "failed"
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO memory_surface_eval_run (
                    id, status, total_cases, passed_cases, failed_cases,
                    created_at, completed_at, summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    status,
                    len(rows),
                    len(passed),
                    len(failed),
                    created_at,
                    utc_now(),
                    json_dumps(summary),
                ),
            )
            for row in rows:
                self.conn.execute(
                    """
                    INSERT INTO memory_surface_eval_case (
                        id, run_id, case_name, status, details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("surface_case"),
                        run_id,
                        row["case_name"],
                        row["status"],
                        json_dumps(row),
                        utc_now(),
                    ),
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
                "SELECT * FROM memory_surface_eval_run ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "status": row["status"],
                "total_cases": row["total_cases"],
                "passed_cases": row["passed_cases"],
                "failed_cases": row["failed_cases"],
                "summary": _loads(row["summary_json"]),
                "created_at": row["created_at"],
                "completed_at": row["completed_at"],
            }
            for row in rows
        ]

    def _evaluate_case(self, case: JsonDict) -> JsonDict:
        plan = RecallPlan(
            query_text=str(case.get("query_text") or "what do you remember?"),
            top_k=1,
            explicit_recall=bool(case.get("explicit_recall")),
            surface_hint=str(case.get("surface_hint") or "allow_gist"),
            query_intent=str(case.get("query_intent") or "open_chat"),
            budget=RecallBudget(token_budget=int(case.get("token_budget") or 120)),
        )
        memory = _memory_payload(case.get("memory") or {})
        candidate = PackedMemory(
            score=float(case.get("score") or 0.7),
            memory=memory,
            matched_backends=("surface_eval",),
            why_recalled="surface policy eval candidate",
        )
        decision = self.surface_policy.decide(
            plan,
            [candidate],
            token_budget=plan.budget.token_budget,
            now=str(case.get("now") or utc_now()),
        )
        expected_action = str(case.get("expected_action") or "implicit_continuity")
        expected_legacy = str(case.get("expected_legacy_action") or "implicit")
        expected_selected = bool(case.get("expected_selected", True))
        selected = bool(decision.selections)
        passed = (
            decision.action == expected_action
            and decision.legacy_action == expected_legacy
            and selected == expected_selected
        )
        return {
            "case_name": str(case.get("case_name") or case.get("id") or "surface_case"),
            "status": "passed" if passed else "failed",
            "expected_action": expected_action,
            "actual_action": decision.action,
            "expected_legacy_action": expected_legacy,
            "actual_legacy_action": decision.legacy_action,
            "expected_selected": expected_selected,
            "actual_selected": selected,
            "decision": decision.public_dict(),
        }


def default_surface_eval_cases() -> list[JsonDict]:
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    return [
        {
            "case_name": "explicit_low_risk_answers_memory_question",
            "explicit_recall": True,
            "expected_action": "answer_memory_question",
            "expected_legacy_action": "gist",
        },
        {
            "case_name": "implicit_low_risk_continuity",
            "explicit_recall": False,
            "expected_action": "implicit_continuity",
            "expected_legacy_action": "implicit",
        },
        {
            "case_name": "silent_only_explicit_requires_consent",
            "explicit_recall": True,
            "memory": {"disclosure_permission": "silent_only"},
            "expected_action": "ask_consent",
            "expected_legacy_action": "none",
            "expected_selected": False,
        },
        {
            "case_name": "denied_disclosure_suppresses_implicit",
            "explicit_recall": False,
            "memory": {"disclosure_permission": "deny"},
            "expected_action": "none",
            "expected_legacy_action": "none",
            "expected_selected": False,
        },
        {
            "case_name": "surface_override_hidden_suppresses",
            "explicit_recall": True,
            "memory": {"surface_permission": "none"},
            "expected_action": "none",
            "expected_legacy_action": "none",
            "expected_selected": False,
        },
        {
            "case_name": "cooldown_suppresses_proactive_surface",
            "explicit_recall": False,
            "memory": {"surface_cooldown_until": future},
            "expected_action": "none",
            "expected_legacy_action": "none",
            "expected_selected": False,
        },
        {
            "case_name": "relationship_context_uses_companion_safe_action",
            "explicit_recall": False,
            "query_intent": "relationship_context",
            "memory": {
                "type": "relationship_signal",
                "summary": "The user values warm continuity without possessive framing.",
                "relationship_impact": 0.7,
            },
            "expected_action": "relationship_context",
            "expected_legacy_action": "implicit",
        },
        {
            "case_name": "sensitive_explicit_recall_uses_redacted_gist",
            "explicit_recall": True,
            "memory": {
                "summary": "The user shared a sensitive family concern.",
                "sensitivity": "high",
                "disclosure_permission": "gist_only",
            },
            "expected_action": "redacted_gist",
            "expected_legacy_action": "gist",
        },
        {
            "case_name": "quote_request_with_permission_uses_quote_memory",
            "query_text": "Please quote what I said about concise continuity.",
            "explicit_recall": True,
            "memory": {"disclosure_permission": "quote_ok"},
            "expected_action": "quote_memory",
            "expected_legacy_action": "gist",
        },
        {
            "case_name": "negative_feedback_suppresses_proactive_surface",
            "explicit_recall": False,
            "memory": {"surface_feedback": {"score": -0.9}},
            "expected_action": "none",
            "expected_legacy_action": "none",
            "expected_selected": False,
        },
    ]


def _memory_payload(overrides: JsonDict) -> JsonDict:
    payload: JsonDict = {
        "id": "mem_surface_eval",
        "type": "preference",
        "summary": "The user prefers concise continuity.",
        "confidence": 0.9,
        "importance": 0.7,
        "sensitivity": "low",
        "evidence_ids": ["ev_surface_eval"],
        "use_permission": "allow_implicit",
        "disclosure_permission": "gist_only",
        "surface_permission": "auto",
        "surface_cooldown_until": None,
        "relationship_impact": 0.0,
    }
    payload.update(overrides)
    return payload


def _loads(value: str | None) -> JsonDict:
    import json

    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}
