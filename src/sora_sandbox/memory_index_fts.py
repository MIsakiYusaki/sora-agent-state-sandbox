from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from typing import Iterable

from .memory_schema import FusedMemoryHit, RecallPlan, RetrievalHit


ASCII_WORD_RE = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*")
CJK_RE = re.compile(r"[\u3400-\u9fff]")
SEARCH_STOP_TOKENS = {
    "the",
    "this",
    "that",
    "的是",
    "多少",
    "方式",
    "欢的",
    "记忆",
    "名字",
    "哪一",
    "哪支",
    "哪种",
    "几点",
    "前关",
    "是否",
    "什么",
    "是谁",
    "时候",
    "通常",
    "怎么",
    "关于",
    "我的",
    "我最",
    "最喜",
    "用户",
    "喜欢",
    "我们",
    "这个",
    "那个",
    "请记",
    "记住",
    "之前",
    "上次",
    "回忆",
}


def search_tokens(text: str) -> list[str]:
    normalized = text.lower()
    tokens = {match.group(0) for match in ASCII_WORD_RE.finditer(normalized)}
    cjk = "".join(CJK_RE.findall(normalized))
    if len(cjk) == 1:
        tokens.add(cjk)
    else:
        tokens.update(cjk[index : index + 2] for index in range(len(cjk) - 1))
    return sorted(token for token in tokens if token and token not in SEARCH_STOP_TOKENS)


def index_text(summary: str, memory_type: str, tags: Iterable[str]) -> str:
    source = " ".join([summary, memory_type, *tags])
    return " ".join(search_tokens(source))


def hybrid_search(
    conn: sqlite3.Connection,
    *,
    owner_user_id: str,
    agent_id: str,
    plan: RecallPlan,
) -> list[FusedMemoryHit]:
    """Deprecated compatibility path.

    Module 05 production recall uses MemoryRetrievalService, which performs
    FTS/vector/temporal/graph candidate generation, RRF fusion, rerank,
    governance, packing, and trace recording. Keep this helper only for old
    tests or callers that still need the pre-Module-05 FTS/token-overlap path.
    """

    groups: list[list[RetrievalHit]] = []
    candidate_ids: set[str] = set()
    if "fts" in plan.backends and plan.tokens:
        hits = fts_search(conn, owner_user_id, agent_id, plan.tokens, max(plan.top_k * 4, 12))
        groups.append(hits)
        candidate_ids.update(hit.memory_id for hit in hits)
    if ("vector" in plan.backends or "vector_local" in plan.backends) and plan.tokens:
        hits = token_overlap_search(conn, owner_user_id, agent_id, plan.tokens, max(plan.top_k * 4, 12))
        groups.append(hits)
        candidate_ids.update(hit.memory_id for hit in hits)
    if "temporal" in plan.backends and candidate_ids:
        groups.append(
            temporal_search(
                conn,
                owner_user_id,
                agent_id,
                max(plan.top_k * 2, 6),
                candidate_ids=candidate_ids,
            )
        )
    return rerank_fused_hits(reciprocal_rank_fusion(groups), plan)


def fts_search(
    conn: sqlite3.Connection,
    owner_user_id: str,
    agent_id: str,
    tokens: list[str],
    limit: int,
) -> list[RetrievalHit]:
    expression = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:24])
    if not expression:
        return []
    rows = conn.execute(
        """
        SELECT m.*, bm25(memory_fts) AS lexical_rank
        FROM memory_fts
        JOIN memory_record m ON m.id = memory_fts.memory_id
        WHERE memory_fts MATCH ?
          AND m.owner_user_id = ?
          AND m.agent_id = ?
          AND m.status = 'active'
          AND m.retrieval_permission = 'allow'
        ORDER BY lexical_rank ASC
        LIMIT ?
        """,
        (expression, owner_user_id, agent_id, limit),
    ).fetchall()
    return [
        RetrievalHit(
            memory_id=row["id"],
            backend="fts",
            rank=rank,
            score=float(row["lexical_rank"]),
            row=row,
        )
        for rank, row in enumerate(rows, start=1)
    ]


def token_overlap_search(
    conn: sqlite3.Connection,
    owner_user_id: str,
    agent_id: str,
    tokens: list[str],
    limit: int,
) -> list[RetrievalHit]:
    query_tokens = set(tokens)
    rows = conn.execute(
        """
        SELECT *
        FROM memory_record
        WHERE owner_user_id = ? AND agent_id = ? AND status = 'active'
          AND retrieval_permission = 'allow'
        """,
        (owner_user_id, agent_id),
    ).fetchall()
    scored: list[tuple[float, sqlite3.Row]] = []
    for row in rows:
        memory_tokens = set(search_tokens(" ".join([row["summary"], row["type"], row["tags_json"]])))
        overlap = len(query_tokens & memory_tokens) / max(len(query_tokens), 1)
        if overlap <= 0:
            continue
        score = 0.65 * overlap + 0.2 * float(row["importance"]) + 0.15 * float(row["confidence"])
        if row["sensitivity"] == "sensitive":
            score -= 0.1
        scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        RetrievalHit(
            memory_id=row["id"],
            backend="vector_local",
            rank=rank,
            score=score,
            row=row,
        )
        for rank, (score, row) in enumerate(scored[:limit], start=1)
    ]


def temporal_search(
    conn: sqlite3.Connection,
    owner_user_id: str,
    agent_id: str,
    limit: int,
    *,
    candidate_ids: set[str] | None = None,
) -> list[RetrievalHit]:
    id_filter = ""
    params: list[object] = [owner_user_id, agent_id]
    if candidate_ids is not None:
        if not candidate_ids:
            return []
        placeholders = ",".join("?" for _ in candidate_ids)
        id_filter = f" AND id IN ({placeholders})"
        params.extend(sorted(candidate_ids))
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT *
        FROM memory_record
        WHERE owner_user_id = ? AND agent_id = ? AND status = 'active'
          AND retrieval_permission = 'allow'
          {id_filter}
        ORDER BY COALESCE(last_accessed_at, updated_at) DESC,
                 access_count DESC,
                 importance DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [
        RetrievalHit(
            memory_id=row["id"],
            backend="temporal",
            rank=rank,
            score=float(row["importance"]),
            row=row,
        )
        for rank, row in enumerate(rows, start=1)
    ]


def reciprocal_rank_fusion(
    groups: list[list[RetrievalHit]],
    *,
    k: int = 60,
    weights: dict[str, float] | None = None,
) -> list[FusedMemoryHit]:
    backend_weights = weights or {"fts": 1.0, "vector_local": 0.9, "temporal": 0.12}
    scores: dict[str, float] = defaultdict(float)
    rows: dict[str, sqlite3.Row] = {}
    backends: dict[str, set[str]] = defaultdict(set)
    for group in groups:
        for hit in group:
            scores[hit.memory_id] += backend_weights.get(hit.backend, 1.0) / (k + hit.rank)
            rows.setdefault(hit.memory_id, hit.row)
            backends[hit.memory_id].add(hit.backend)
    fused = [
        FusedMemoryHit(
            memory_id=memory_id,
            score=score,
            row=rows[memory_id],
            matched_backends=tuple(sorted(backends[memory_id])),
        )
        for memory_id, score in scores.items()
    ]
    fused.sort(key=lambda item: item.score, reverse=True)
    return fused


def rerank_fused_hits(hits: list[FusedMemoryHit], plan: RecallPlan) -> list[FusedMemoryHit]:
    query = plan.query_text.lower()
    reranked: list[FusedMemoryHit] = []
    for hit in hits:
        row = hit.row
        memory_type = str(row["type"])
        haystack = " ".join([str(row["summary"]), str(row["type"]), str(row["tags_json"])]).lower()
        score = hit.score
        exact_salience = sum(
            1 for term in plan.salient_terms if term and term.lower() in haystack
        )
        if exact_salience:
            score += 0.004 * exact_salience

        asks_preference = any(term in query for term in ("习惯", "偏好", "通常", "喜欢"))
        asks_principle = any(term in query for term in ("约定", "原则", "治理", "边界", "架构"))
        asks_experience = any(term in query for term in ("经历", "一起", "共同"))
        if asks_preference:
            score += _type_bias(memory_type, preference=0.006, profile=0.002, episode=-0.004)
        elif asks_principle:
            score += _type_bias(
                memory_type,
                principle=0.006,
                sensitive_boundary=0.006,
                preference=0.002,
                goal=0.002,
                episode=-0.003,
                profile=-0.003,
            )
        elif not asks_experience:
            score += _type_bias(
                memory_type,
                preference=0.004,
                principle=0.004,
                sensitive_boundary=0.003,
                goal=0.001,
                episode=-0.002,
                profile=-0.002,
            )

        reranked.append(
            FusedMemoryHit(
                memory_id=hit.memory_id,
                score=score,
                row=hit.row,
                matched_backends=hit.matched_backends,
            )
        )
    reranked.sort(key=lambda item: item.score, reverse=True)
    return reranked


def _type_bias(
    memory_type: str,
    *,
    preference: float = 0.0,
    principle: float = 0.0,
    sensitive_boundary: float = 0.0,
    goal: float = 0.0,
    episode: float = 0.0,
    profile: float = 0.0,
) -> float:
    if memory_type == "preference":
        return preference
    if memory_type == "project_principle":
        return principle
    if memory_type == "sensitive_boundary":
        return sensitive_boundary
    if memory_type == "goal":
        return goal
    if memory_type in {"episode", "worldops_episode"}:
        return episode
    if memory_type == "profile":
        return profile
    return 0.0
