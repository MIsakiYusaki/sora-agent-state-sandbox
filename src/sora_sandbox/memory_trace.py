from __future__ import annotations

import sqlite3

from .memory_schema import JsonDict, new_id, utc_now


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
) -> str:
    trace_id = new_id("recall")
    conn.execute(
        """
        INSERT INTO recall_trace (
            id, query_text, backend, surface_action, decision_reason,
            degraded, latency_ms, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trace_id,
            query_text,
            backend,
            surface_action,
            reason,
            int(degraded),
            latency_ms,
            utc_now(),
        ),
    )
    for rank, (score, memory) in enumerate(selected, start=1):
        conn.execute(
            """
            INSERT INTO recall_trace_item (
                trace_id, memory_id, rank, score, selected, filter_reason
            ) VALUES (?, ?, ?, ?, 1, NULL)
            """,
            (trace_id, memory["id"], rank, score),
        )
    conn.commit()
    return trace_id
