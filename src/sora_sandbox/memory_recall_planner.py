from __future__ import annotations

import re
from typing import Protocol

from .memory_index_fts import search_tokens
from .memory_schema import JsonDict, RecallPlan


EXPLICIT_RECALL_TERMS = (
    "remember",
    "recall",
    "previous",
    "last time",
    "before",
    "记得",
    "之前",
    "上次",
    "回忆",
    "记忆",
)
GRAPH_QUERY_TERMS = (
    "relationship",
    "relation",
    "who",
    "name",
    "entity",
    "together",
    "shared",
    "episode",
    "world",
    "person",
    "关系",
    "名字",
    "谁",
    "一起",
    "共同",
)
QUOTED_TERM_RE = re.compile(r"[\"'“”‘’](?P<term>[^\"'“”‘’]{1,40})[\"'“”‘’]")
TOPIC_PATTERNS = (
    re.compile(r"about\s+(?P<term>[A-Za-z0-9_. -]{1,40})", re.IGNORECASE),
    re.compile(r"regarding\s+(?P<term>[A-Za-z0-9_. -]{1,40})", re.IGNORECASE),
    re.compile(r"关于(?P<term>[^，。！？?]{1,40})"),
    re.compile(r"遇到(?P<term>[^，。！？?]{1,24})"),
    re.compile(r"推进(?P<term>[^，。！？?]{1,24})"),
    re.compile(r"我的(?P<term>[^，。！？?]{1,16})"),
)


class RecallPlanner(Protocol):
    def plan(
        self,
        query_text: str,
        *,
        turn_frame: JsonDict | None,
        context_use_policy: JsonDict | None,
        top_k: int,
    ) -> RecallPlan:
        ...


class RuleRecallPlanner:
    def plan(
        self,
        query_text: str,
        *,
        turn_frame: JsonDict | None,
        context_use_policy: JsonDict | None,
        top_k: int,
    ) -> RecallPlan:
        policy = context_use_policy or {}
        blocked = set(policy.get("blocked_context_sources", []))
        if "memory_recollection" in blocked:
            return RecallPlan(
                query_text=query_text,
                top_k=top_k,
                skipped_reason="memory_recollection_blocked_by_context_policy",
                backends=(),
            )
        if not query_text.strip():
            return RecallPlan(
                query_text=query_text,
                top_k=top_k,
                skipped_reason="empty_or_proactive_query",
                backends=(),
            )
        explicit = any(term.lower() in query_text.lower() for term in EXPLICIT_RECALL_TERMS)
        salient_terms = extract_salient_terms(query_text)
        token_source = " ".join(salient_terms) if salient_terms else query_text
        budget = 1200 if explicit else 500
        return RecallPlan(
            query_text=query_text,
            top_k=top_k,
            tokens=search_tokens(token_source),
            salient_terms=salient_terms,
            explicit_recall=explicit,
            backends=choose_backends(query_text, explicit_recall=explicit),
            service_budget_ms=budget,
            planner_mode="rules",
        )


def choose_backends(query_text: str, *, explicit_recall: bool) -> tuple[str, ...]:
    """Select logical retrieval backends for the current query.

    The vector backend is intentionally logical ("vector") so the runtime can
    route it to either local vectors or a production external vector DB.
    """

    lowered = query_text.lower()
    backends: list[str] = ["fts", "vector", "temporal", "graph"]
    if explicit_recall or any(term in lowered for term in GRAPH_QUERY_TERMS):
        return tuple(backends)
    return tuple(backends)


def extract_salient_terms(query_text: str) -> list[str]:
    terms: list[str] = []
    for match in QUOTED_TERM_RE.finditer(query_text):
        terms.append(match.group("term"))
    if terms:
        return _dedupe_terms(terms)

    for pattern in TOPIC_PATTERNS:
        match = pattern.search(query_text)
        if match:
            terms.append(_clean_term(match.group("term")))
    return _dedupe_terms(term for term in terms if term)


def _clean_term(value: str) -> str:
    cleaned = value.strip(" ，,。.!！？?\"'“”‘’")
    cleanup_prefixes = ("which ", "what ", "the ", "哪个", "哪一个", "哪种", "几")
    cleanup_suffixes = ("是什么", "是谁", "多少", "哪个", "哪种", "品牌")
    for prefix in cleanup_prefixes:
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    for suffix in cleanup_suffixes:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
    return cleaned.strip(" ，,。.!！？?\"'“”‘’")


def _dedupe_terms(values) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for value in values:
        cleaned = _clean_term(str(value))
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        terms.append(cleaned)
    return terms
