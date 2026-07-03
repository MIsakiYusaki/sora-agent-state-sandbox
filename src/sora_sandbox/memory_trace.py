from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .memory_schema import JsonDict, json_dumps, new_id, utc_now


def record_recall_trace(
    conn: sqlite3.Connection,
    query_text: str,
    *,
    backend: str,
    surface_action: str,
    reason: str,
    selected: list[tuple[float, JsonDict]],
    latency_ms: float,
    degraded: bool = False,
    plan: JsonDict | None = None,
    trace_items: list[JsonDict] | None = None,
    token_budget: int = 0,
    token_used: int = 0,
    cost: JsonDict | None = None,
    ttl_days: int = 30,
) -> str:
    trace_id = new_id("recall")
    created_at = utc_now()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=ttl_days)
    ).isoformat() if ttl_days > 0 else created_at
    items = trace_items or [
        {
            "memory_id": memory["id"],
            "rank": rank,
            "score": score,
            "selected": True,
            "backend": "hybrid_rrf",
            "backend_rank": rank,
            "backend_score": score,
            "rrf_score": score,
            "rerank_score": score,
            "matched_backends": [],
            "filter_reason": None,
        }
        for rank, (score, memory) in enumerate(selected, start=1)
    ]
    conn.execute(
        """
        INSERT INTO recall_trace (
            id, query_text, backend, surface_action, decision_reason,
            degraded, latency_ms, plan_json, token_budget, token_used,
            final_count, item_count, cost_json, expires_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trace_id,
            query_text,
            backend,
            surface_action,
            reason,
            int(degraded),
            latency_ms,
            json_dumps(plan or {}),
            int(token_budget),
            int(token_used),
            len(selected),
            len(items),
            json_dumps(cost or {}),
            expires_at,
            created_at,
        ),
    )
    for item in items:
        conn.execute(
            """
            INSERT INTO recall_trace_item (
                trace_id, memory_id, rank, score, selected, backend,
                backend_rank, backend_score, rrf_score, rerank_score,
                matched_backends_json, filter_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace_id,
                item["memory_id"],
                int(item.get("rank") or 0),
                float(item.get("score") or 0.0),
                int(bool(item.get("selected"))),
                item.get("backend"),
                item.get("backend_rank"),
                item.get("backend_score"),
                item.get("rrf_score"),
                item.get("rerank_score"),
                json_dumps(item.get("matched_backends", [])),
                item.get("filter_reason"),
            ),
        )
    conn.commit()
    return trace_id


def record_recall_plan_trace(
    conn: sqlite3.Connection,
    plan: JsonDict,
    *,
    recall_trace_id: str | None = None,
    turn_id: str | None = None,
    session_id: str | None = None,
) -> None:
    plan_id = str(plan.get("plan_id") or new_id("recall_plan"))
    created_at = utc_now()
    conn.execute(
        """
        INSERT INTO recall_plan_trace (
            plan_id, recall_trace_id, turn_id, session_id, planner_version,
            planner_mode, decision, query_intent, scene, explicit_recall,
            skipped_reason, subqueries_json, budget_json, decision_reasons_json,
            llm_usage_json, planner_trace_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(plan_id) DO UPDATE SET
            recall_trace_id = excluded.recall_trace_id,
            turn_id = excluded.turn_id,
            session_id = excluded.session_id,
            planner_version = excluded.planner_version,
            planner_mode = excluded.planner_mode,
            decision = excluded.decision,
            query_intent = excluded.query_intent,
            scene = excluded.scene,
            explicit_recall = excluded.explicit_recall,
            skipped_reason = excluded.skipped_reason,
            subqueries_json = excluded.subqueries_json,
            budget_json = excluded.budget_json,
            decision_reasons_json = excluded.decision_reasons_json,
            llm_usage_json = excluded.llm_usage_json,
            planner_trace_json = excluded.planner_trace_json
        """,
        (
            plan_id,
            recall_trace_id,
            turn_id,
            session_id,
            str(plan.get("planner_version") or "unknown"),
            str(plan.get("mode") or plan.get("planner_mode") or "unknown"),
            str(plan.get("decision") or "unknown"),
            str(plan.get("query_intent") or "unknown"),
            str(plan.get("scene") or "unknown"),
            int(bool(plan.get("explicit_recall"))),
            plan.get("skip_reason") or plan.get("skipped_reason"),
            json_dumps(plan.get("subqueries", [])),
            json_dumps(plan.get("budget", {})),
            json_dumps(plan.get("decision_reasons", [])),
            json_dumps(plan.get("llm_usage", {})),
            json_dumps(plan.get("planner_trace", {})),
            created_at,
        ),
    )
    conn.commit()


def record_surface_policy_trace(
    conn: sqlite3.Connection,
    *,
    recall_trace_id: str | None,
    policy_version: str,
    policy_profile: str,
    provider: str,
    surface_action: str,
    legacy_surface_action: str,
    decision_reason: str,
    forbidden_disclosures: list[str],
    consent_request: JsonDict | None,
    items: list[JsonDict],
    token_budget: int,
    token_used: int,
    provider_usage: list[JsonDict] | None = None,
    turn_id: str | None = None,
    session_id: str | None = None,
) -> str:
    trace_id = new_id("surface")
    created_at = utc_now()
    conn.execute(
        """
        INSERT INTO memory_surface_policy_trace (
            id, recall_trace_id, turn_id, session_id, policy_version,
            policy_profile, provider, surface_action, legacy_surface_action,
            decision_reason, forbidden_disclosures_json, consent_request_json,
            provider_usage_json, token_budget, token_used, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trace_id,
            recall_trace_id,
            turn_id,
            session_id,
            policy_version,
            policy_profile,
            provider,
            surface_action,
            legacy_surface_action,
            decision_reason,
            json_dumps(forbidden_disclosures),
            json_dumps(consent_request or {}),
            json_dumps(provider_usage or []),
            int(token_budget),
            int(token_used),
            created_at,
        ),
    )
    for item in items:
        conn.execute(
            """
            INSERT INTO memory_surface_policy_item (
                trace_id, memory_id, rank, score, priority, action,
                legacy_action, selected, reason, suppression_reason, policy_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace_id,
                item["memory_id"],
                int(item.get("rank") or 0),
                float(item.get("score") or 0.0),
                float(item.get("priority") or 0.0),
                str(item.get("action") or "none"),
                str(item.get("legacy_action") or "none"),
                int(bool(item.get("selected"))),
                str(item.get("reason") or ""),
                item.get("suppression_reason"),
                json_dumps(item.get("policy") or {}),
            ),
        )
    conn.commit()
    return trace_id


def cleanup_recall_traces(
    conn: sqlite3.Connection,
    *,
    older_than_days: int = 30,
    keep_pinned: bool = True,
) -> JsonDict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    pinned_filter = "AND pinned = 0" if keep_pinned else ""
    rows = conn.execute(
        f"""
        SELECT id FROM recall_trace
        WHERE (expires_at IS NOT NULL AND expires_at <= ? OR created_at <= ?)
        {pinned_filter}
        """,
        (utc_now(), cutoff),
    ).fetchall()
    ids = [row["id"] for row in rows]
    if not ids:
        return {"status": "ok", "deleted_count": 0, "deleted_ids": []}
    placeholders = ",".join("?" for _ in ids)
    surface_rows = conn.execute(
        f"SELECT id FROM memory_surface_policy_trace WHERE recall_trace_id IN ({placeholders})",
        ids,
    ).fetchall()
    surface_ids = [row["id"] for row in surface_rows]
    if surface_ids:
        surface_placeholders = ",".join("?" for _ in surface_ids)
        conn.execute(
            f"DELETE FROM memory_surface_policy_item WHERE trace_id IN ({surface_placeholders})",
            surface_ids,
        )
        conn.execute(
            f"DELETE FROM memory_surface_policy_trace WHERE id IN ({surface_placeholders})",
            surface_ids,
        )
    conn.execute(f"DELETE FROM recall_trace_item WHERE trace_id IN ({placeholders})", ids)
    conn.execute(f"DELETE FROM recall_trace WHERE id IN ({placeholders})", ids)
    conn.commit()
    return {"status": "ok", "deleted_count": len(ids), "deleted_ids": ids}
