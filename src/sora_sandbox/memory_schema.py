from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


JsonDict = dict[str, Any]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class MemorySourceType(StrEnum):
    CONVERSATION = "conversation"
    WORLD_EVENT = "world_event"
    RESPONSE_PLAN = "response_plan"
    MANUAL = "manual"
    LEGACY_IMPORT = "legacy_state_import"
    SEED = "seed"


class MemoryActor(StrEnum):
    USER = "user"
    SORA = "sora"
    SYSTEM = "system"


class EpisodeStatus(StrEnum):
    OPEN = "open"
    SEALED = "sealed"
    EXTRACTED = "extracted"
    ARCHIVED = "archived"
    DELETED = "deleted"


class ExtractionJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EvidenceVisibility(StrEnum):
    PRIVATE = "private"
    LOCAL_ONLY = "local_only"
    REDACTED = "redacted"


@dataclass(frozen=True)
class PreFilterDecision:
    should_extract: bool
    reason: str
    priority: int = 50
    archived: bool = False


@dataclass(frozen=True)
class MemoryEventRecord:
    event_id: str
    source_type: str
    actor: str
    session_id: str
    turn_id: str | None
    episode_id: str | None
    payload: JsonDict
    raw_ref: str | None
    visibility: str
    retention_policy: str
    sensitivity_hint: str
    event_hash: str
    created_at: str
    deleted_at: str | None = None
    delete_reason: str | None = None


@dataclass(frozen=True)
class EpisodeRecord:
    episode_id: str
    session_id: str
    status: str
    topic_hint: str | None
    gist: str
    event_ids: list[str]
    sealed_reason: str | None
    created_at: str
    sealed_at: str | None
    updated_at: str
    deleted_at: str | None = None
    delete_reason: str | None = None


@dataclass(frozen=True)
class ExtractionJobRecord:
    job_id: str
    episode_id: str
    status: str
    reason: str
    attempts: int
    input_summary: str
    event_ids: list[str]
    priority: int
    created_at: str
    updated_at: str
    available_at: str | None = None
    claimed_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    result_summary: str | None = None


@dataclass(frozen=True)
class RecallSubquery:
    query_id: str
    text: str
    tokens: list[str] = field(default_factory=list)
    salient_terms: list[str] = field(default_factory=list)
    memory_types: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    time_range: str = "any"
    backends: tuple[str, ...] = ("fts", "vector", "temporal", "graph")
    filters: JsonDict = field(default_factory=dict)
    top_k: int = 5
    min_confidence: float = 0.0
    weight: float = 1.0
    reason: str = ""


@dataclass(frozen=True)
class RecallBudget:
    token_budget: int = 420
    service_budget_ms: int = 500
    rerank_budget_ms: int = 150
    llm_planner_budget_tokens: int = 0


@dataclass(frozen=True)
class RecallPlan:
    query_text: str
    top_k: int
    tokens: list[str] = field(default_factory=list)
    salient_terms: list[str] = field(default_factory=list)
    explicit_recall: bool = False
    skipped_reason: str | None = None
    backends: tuple[str, ...] = ("fts", "vector", "temporal", "graph")
    service_budget_ms: int = 500
    planner_mode: str = "rules"
    plan_id: str = field(default_factory=lambda: new_id("recall_plan"))
    planner_version: str = "recall_planner_v2"
    decision: str = "light_recall"
    query_intent: str = "open_chat"
    scene: str = "unknown"
    subqueries: list[RecallSubquery] = field(default_factory=list)
    budget: RecallBudget = field(default_factory=RecallBudget)
    surface_hint: str = "allow_implicit"
    fallbacks: list[str] = field(default_factory=lambda: ["rules_fast_path", "fts_vector_only", "skip_with_trace"])
    decision_reasons: list[str] = field(default_factory=list)
    llm_usage: JsonDict = field(default_factory=dict)
    planner_trace: JsonDict = field(default_factory=dict)
    prefetch: JsonDict = field(default_factory=dict)
    trace: bool = True


@dataclass(frozen=True)
class RetrievalHit:
    memory_id: str
    backend: str
    rank: int
    score: float
    row: Any


@dataclass(frozen=True)
class FusedMemoryHit:
    memory_id: str
    score: float
    row: Any
    matched_backends: tuple[str, ...]
