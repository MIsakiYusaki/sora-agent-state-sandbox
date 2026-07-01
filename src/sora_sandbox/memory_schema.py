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
class RecallPlan:
    query_text: str
    top_k: int
    tokens: list[str] = field(default_factory=list)
    salient_terms: list[str] = field(default_factory=list)
    explicit_recall: bool = False
    skipped_reason: str | None = None
    backends: tuple[str, ...] = ("fts", "vector_local", "temporal")
    service_budget_ms: int = 500
    planner_mode: str = "rules"


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
