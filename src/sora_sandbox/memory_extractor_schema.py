from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .memory_schema import JsonDict


EXTRACTOR_RESULT_SCHEMA_VERSION = "extractor_result.v1"


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class WriteOperation(StrEnum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    MERGE = "MERGE"
    DELETE = "DELETE"
    NOOP = "NOOP"
    REVIEW = "REVIEW"


class ExtractedMemoryType(StrEnum):
    PREFERENCE = "preference"
    GOAL = "goal"
    SHARED_EPISODE = "shared_episode"
    PERSONA_DELTA = "persona_delta"
    RELATIONSHIP_SIGNAL = "relationship_signal"
    DOMAIN_PRINCIPLE = "domain_principle"
    PROJECT_PRINCIPLE = "project_principle"
    WORLD_STATE = "world_state"
    WORLD_OPS = "world_ops"
    PROCEDURAL_HINT = "procedural_hint"
    EPISODE = "episode"
    WORLDOPS_EPISODE = "worldops_episode"


class ExtractorValidationError(ValueError):
    pass


@dataclass(frozen=True)
class EvidenceRef:
    event_id: str
    episode_id: str
    speaker: str | None = None
    span: tuple[int, int] | None = None


@dataclass(frozen=True)
class ExtractorScores:
    confidence: float
    importance: float = 0.5
    stability: float = 0.5
    specificity: float = 0.5
    novelty: float = 1.0
    conflict_score: float = 0.0


@dataclass(frozen=True)
class MemoryCandidateDraft:
    candidate_id: str
    operation: WriteOperation
    memory_type: str
    summary: str
    raw_quote: str | None
    evidence_refs: list[EvidenceRef]
    target_memory_id: str | None = None
    scores: ExtractorScores = field(default_factory=lambda: ExtractorScores(confidence=0.0))
    sensitivity: str = "low"
    visibility: str = "internal"
    temporal_scope: JsonDict = field(default_factory=dict)
    entities: list[JsonDict] = field(default_factory=list)
    reason: str = "llm_extractor"
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExtractorResult:
    schema_version: str
    job_id: str
    episode_id: str
    model: JsonDict
    candidates: list[MemoryCandidateDraft]
    contradictions: list[JsonDict] = field(default_factory=list)
    trace: JsonDict = field(default_factory=dict)


def validate_extractor_result(payload: JsonDict, input_package: JsonDict) -> ExtractorResult:
    if payload.get("schema_version") != EXTRACTOR_RESULT_SCHEMA_VERSION:
        raise ExtractorValidationError("unsupported extractor result schema_version")

    job_id = _required_str(payload, "job_id")
    episode_id = _required_str(payload, "episode_id")
    expected_job_id = str(input_package.get("job", {}).get("job_id") or input_package.get("job_id") or "")
    expected_episode_id = str(
        input_package.get("episode", {}).get("episode_id") or input_package.get("episode_id") or ""
    )
    if expected_job_id and job_id != expected_job_id:
        raise ExtractorValidationError(f"job_id mismatch: {job_id} != {expected_job_id}")
    if expected_episode_id and episode_id != expected_episode_id:
        raise ExtractorValidationError(f"episode_id mismatch: {episode_id} != {expected_episode_id}")

    event_text_by_id = {
        str(event.get("event_id") or event.get("id")): _event_text(event)
        for event in input_package.get("events", [])
        if event.get("event_id") or event.get("id")
    }
    event_ids = set(event_text_by_id)
    related_memory_ids = {
        str(memory.get("id"))
        for memory in input_package.get("related_memories", [])
        if memory.get("id")
    }

    candidates = [
        _validate_candidate(item, episode_id, event_ids, event_text_by_id, related_memory_ids)
        for item in _required_list(payload, "candidates")
    ]
    contradictions = _validate_contradictions(
        list(payload.get("contradictions") or []),
        {candidate.candidate_id for candidate in candidates},
        related_memory_ids,
    )
    return ExtractorResult(
        schema_version=payload["schema_version"],
        job_id=job_id,
        episode_id=episode_id,
        model=dict(payload.get("model") or {}),
        candidates=candidates,
        contradictions=contradictions,
        trace=dict(payload.get("trace") or {}),
    )


def extractor_result_to_dict(result: ExtractorResult) -> JsonDict:
    return {
        "schema_version": result.schema_version,
        "job_id": result.job_id,
        "episode_id": result.episode_id,
        "model": result.model,
        "candidates": [candidate_to_dict(candidate) for candidate in result.candidates],
        "contradictions": result.contradictions,
        "trace": result.trace,
    }


def candidate_to_dict(candidate: MemoryCandidateDraft) -> JsonDict:
    return {
        "candidate_id": candidate.candidate_id,
        "operation": candidate.operation.value,
        "memory_type": candidate.memory_type,
        "summary": candidate.summary,
        "raw_quote": candidate.raw_quote,
        "evidence_refs": [
            {
                "event_id": ref.event_id,
                "episode_id": ref.episode_id,
                "speaker": ref.speaker,
                "span": list(ref.span) if ref.span else None,
            }
            for ref in candidate.evidence_refs
        ],
        "target_memory_id": candidate.target_memory_id,
        "confidence": candidate.scores.confidence,
        "importance": candidate.scores.importance,
        "stability": candidate.scores.stability,
        "specificity": candidate.scores.specificity,
        "novelty": candidate.scores.novelty,
        "conflict_score": candidate.scores.conflict_score,
        "sensitivity": candidate.sensitivity,
        "visibility": candidate.visibility,
        "temporal_scope": candidate.temporal_scope,
        "entities": candidate.entities,
        "reason": candidate.reason,
        "tags": candidate.tags,
    }


def _validate_candidate(
    payload: JsonDict,
    episode_id: str,
    event_ids: set[str],
    event_text_by_id: dict[str, str],
    related_memory_ids: set[str],
) -> MemoryCandidateDraft:
    operation = _enum_value(WriteOperation, _required_str(payload, "operation"), "operation")
    memory_type = _enum_value(ExtractedMemoryType, _required_str(payload, "memory_type"), "memory_type")
    candidate_id = str(payload.get("candidate_id") or "")
    if not candidate_id:
        candidate_id = "draft"
    summary = _required_str(payload, "summary").strip()
    if operation not in {WriteOperation.NOOP, WriteOperation.REVIEW} and not summary:
        raise ExtractorValidationError("write candidate summary is required")
    if len(summary) > 800:
        raise ExtractorValidationError("candidate summary is too long")

    evidence_refs = [
        _validate_evidence_ref(item, episode_id, event_ids, event_text_by_id)
        for item in list(payload.get("evidence_refs") or [])
    ]
    if operation not in {WriteOperation.NOOP} and not evidence_refs:
        raise ExtractorValidationError("candidate requires at least one evidence_ref")

    target_memory_id = payload.get("target_memory_id")
    if target_memory_id is not None:
        target_memory_id = str(target_memory_id)
    if operation in {WriteOperation.UPDATE, WriteOperation.MERGE, WriteOperation.DELETE}:
        if not target_memory_id:
            raise ExtractorValidationError(f"{operation.value} requires target_memory_id")
        if related_memory_ids and target_memory_id not in related_memory_ids:
            raise ExtractorValidationError("target_memory_id was not provided as related memory")

    return MemoryCandidateDraft(
        candidate_id=candidate_id,
        operation=operation,
        memory_type=memory_type.value,
        summary=summary,
        raw_quote=_optional_str(payload.get("raw_quote")),
        evidence_refs=evidence_refs,
        target_memory_id=target_memory_id,
        scores=ExtractorScores(
            confidence=_score(payload, "confidence", 0.0),
            importance=_score(payload, "importance", 0.5),
            stability=_score(payload, "stability", 0.5),
            specificity=_score(payload, "specificity", 0.5),
            novelty=_score(payload, "novelty", 1.0),
            conflict_score=_score(payload, "conflict_score", 0.0),
        ),
        sensitivity=str(payload.get("sensitivity") or "low"),
        visibility=str(payload.get("visibility") or "internal"),
        temporal_scope=dict(payload.get("temporal_scope") or {}),
        entities=list(payload.get("entities") or []),
        reason=str(payload.get("reason") or "llm_extractor"),
        tags=[str(tag) for tag in list(payload.get("tags") or [])],
    )


def _validate_evidence_ref(
    payload: JsonDict | str,
    episode_id: str,
    event_ids: set[str],
    event_text_by_id: dict[str, str],
) -> EvidenceRef:
    if isinstance(payload, str):
        payload = {"event_id": payload, "episode_id": episode_id}
    if not isinstance(payload, dict):
        raise ExtractorValidationError("evidence_ref must be an object or event_id string")
    event_id = _required_str(payload, "event_id")
    if event_ids and event_id not in event_ids:
        raise ExtractorValidationError(f"unknown evidence event_id: {event_id}")
    ref_episode_id = str(payload.get("episode_id") or episode_id)
    if ref_episode_id != episode_id:
        raise ExtractorValidationError("evidence_ref episode_id mismatch")
    span = payload.get("span")
    parsed_span: tuple[int, int] | None = None
    if span is not None:
        if (
            not isinstance(span, (list, tuple))
            or len(span) != 2
            or not all(isinstance(item, int) for item in span)
            or span[0] < 0
            or span[1] < span[0]
        ):
            raise ExtractorValidationError("span must be [start, end]")
        event_text = event_text_by_id.get(event_id, "")
        if span[1] > len(event_text):
            raise ExtractorValidationError("span end exceeds referenced event content length")
        parsed_span = (int(span[0]), int(span[1]))
    return EvidenceRef(
        event_id=event_id,
        episode_id=ref_episode_id,
        speaker=_optional_str(payload.get("speaker")),
        span=parsed_span,
    )


def _validate_contradictions(
    payloads: list[Any],
    candidate_ids: set[str],
    related_memory_ids: set[str],
) -> list[JsonDict]:
    validated: list[JsonDict] = []
    allowed_resolutions = {"UPDATE", "REVIEW", "NOOP", "DELETE"}
    for item in payloads:
        if not isinstance(item, dict):
            raise ExtractorValidationError("contradiction must be an object")
        new_candidate_id = _required_str(item, "new_candidate_id")
        existing_memory_id = _required_str(item, "existing_memory_id")
        resolution = _required_str(item, "resolution").upper()
        if candidate_ids and new_candidate_id not in candidate_ids:
            raise ExtractorValidationError("contradiction references unknown new_candidate_id")
        if related_memory_ids and existing_memory_id not in related_memory_ids:
            raise ExtractorValidationError("contradiction references memory outside related_memories")
        if resolution not in allowed_resolutions:
            raise ExtractorValidationError("invalid contradiction resolution")
        validated.append(
            {
                **item,
                "new_candidate_id": new_candidate_id,
                "existing_memory_id": existing_memory_id,
                "resolution": resolution,
            }
        )
    return validated


def _event_text(event: JsonDict) -> str:
    payload = event.get("payload", {})
    if isinstance(payload, dict):
        return str(payload.get("content") or payload.get("text") or "")
    return str(payload)


def _required_str(payload: JsonDict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ExtractorValidationError(f"{key} is required")
    return value


def _required_list(payload: JsonDict, key: str) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ExtractorValidationError(f"{key} must be a list")
    return value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _enum_value(enum: type[StrEnum], value: str, field_name: str) -> StrEnum:
    try:
        return enum(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum)
        raise ExtractorValidationError(f"invalid {field_name}: {value}; allowed: {allowed}") from exc


def _score(payload: JsonDict, key: str, default: float) -> float:
    raw = payload.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ExtractorValidationError(f"{key} must be a number") from exc
    if value < 0 or value > 1:
        raise ExtractorValidationError(f"{key} must be between 0 and 1")
    return value
