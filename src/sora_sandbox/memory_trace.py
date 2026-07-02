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
    conn.execute(f"DELETE FROM recall_trace_item WHERE trace_id IN ({placeholders})", ids)
    conn.execute(f"DELETE FROM recall_trace WHERE id IN ({placeholders})", ids)
    conn.commit()
    return {"status": "ok", "deleted_count": len(ids), "deleted_ids": ids}
