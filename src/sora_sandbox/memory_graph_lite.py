from __future__ import annotations

import sqlite3

from .memory_index_fts import search_tokens
from .memory_schema import JsonDict, RetrievalHit, json_dumps, new_id, utc_now


def upsert_graph_edges(
    conn: sqlite3.Connection,
    document: JsonDict,
    *,
    index_generation: str = "active",
) -> JsonDict:
    memory_id = str(document["memory_id"])
    metadata = document.get("metadata", {})
    entities = [str(item).lower() for item in metadata.get("entities", []) if str(item).strip()]
    tags = [str(item).lower() for item in metadata.get("tags", []) if str(item).strip()]
    memory_type = str(metadata.get("memory_type") or "memory")
    evidence_ids = list(metadata.get("evidence_ids", []))
    conn.execute(
        "DELETE FROM memory_graph_edge WHERE memory_id = ? AND index_generation = ?",
        (memory_id, index_generation),
    )
    edge_count = 0
    now = utc_now()
    for entity in _dedupe([*entities[:24], *tags[:12], memory_type]):
        conn.execute(
            """
            INSERT INTO memory_graph_edge (
                id, memory_id, src_type, src_id, edge_type, dst_type, dst_id,
                weight, evidence_ids_json, index_generation, created_at
            ) VALUES (?, ?, 'memory', ?, 'mentions', 'entity', ?, ?, ?, ?, ?)
            """,
            (
                new_id("edge"),
                memory_id,
                memory_id,
                entity,
                1.0 if entity in entities else 0.72,
                json_dumps(evidence_ids),
                index_generation,
                now,
            ),
        )
        edge_count += 1
    return {
        "backend": "graph",
        "action": "upsert",
        "applied": True,
        "edge_count": edge_count,
        "index_generation": index_generation,
    }


def delete_graph_edges(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    index_generation: str | None = None,
) -> JsonDict:
    if index_generation:
        conn.execute(
            "DELETE FROM memory_graph_edge WHERE memory_id = ? AND index_generation = ?",
            (memory_id, index_generation),
        )
    else:
        conn.execute("DELETE FROM memory_graph_edge WHERE memory_id = ?", (memory_id,))
    return {
        "backend": "graph",
        "action": "delete",
        "applied": True,
        "index_generation": index_generation or "*",
    }


def graph_search(
    conn: sqlite3.Connection,
    *,
    owner_user_id: str,
    agent_id: str,
    query_text: str,
    limit: int,
    index_generation: str = "active",
) -> list[RetrievalHit]:
    tokens = set(search_tokens(query_text))
    if not tokens:
        return []
    rows = conn.execute(
        """
        SELECT m.*, MAX(e.weight) AS graph_weight, COUNT(e.id) AS edge_hits
        FROM memory_graph_edge e
        JOIN memory_record m ON m.id = e.memory_id
        WHERE e.index_generation = ?
          AND e.dst_id IN (%s)
          AND m.owner_user_id = ?
          AND m.agent_id = ?
          AND m.status = 'active'
          AND m.retrieval_permission = 'allow'
        GROUP BY m.id
        ORDER BY graph_weight DESC, edge_hits DESC, m.importance DESC
        LIMIT ?
        """ % ",".join("?" for _ in tokens),
        [index_generation, *sorted(tokens), owner_user_id, agent_id, limit],
    ).fetchall()
    return [
        RetrievalHit(
            memory_id=row["id"],
            backend="graph",
            rank=rank,
            score=float(row["graph_weight"] or 0.0) + float(row["edge_hits"] or 0.0) * 0.05,
            row=row,
        )
        for rank, row in enumerate(rows, start=1)
    ]


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = value.strip().lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result
