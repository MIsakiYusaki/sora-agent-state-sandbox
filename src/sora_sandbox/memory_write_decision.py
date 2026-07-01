from __future__ import annotations

from dataclasses import replace

from .memory_extractor_schema import (
    MemoryCandidateDraft,
    WriteOperation,
)
from .memory_index_fts import search_tokens
from .memory_schema import JsonDict


CONFLICT_CUES = (
    "no longer",
    "instead",
    "rather than",
    "not minimal",
    "不再",
    "改成",
    "不是",
    "而是",
    "修正",
)
DELETE_CUES = ("forget", "delete", "remove", "不要记", "忘记", "删除", "不算了")


def calibrate_write_decisions(
    candidates: list[MemoryCandidateDraft],
    related_memories: list[JsonDict],
) -> list[MemoryCandidateDraft]:
    return [calibrate_candidate(candidate, related_memories) for candidate in candidates]


def calibrate_candidate(
    candidate: MemoryCandidateDraft,
    related_memories: list[JsonDict],
) -> MemoryCandidateDraft:
    if candidate.operation in {WriteOperation.NOOP, WriteOperation.REVIEW}:
        return candidate
    best_memory, best_similarity = most_similar_memory(candidate.summary, related_memories)
    scores = candidate.scores
    novelty = min(scores.novelty, max(0.0, 1.0 - best_similarity))
    conflict_score = max(scores.conflict_score, _deterministic_conflict(candidate.summary, best_similarity))
    operation = candidate.operation
    target_memory_id = candidate.target_memory_id

    if operation == WriteOperation.ADD and best_memory and best_similarity >= 0.78:
        operation = WriteOperation.MERGE
        target_memory_id = str(best_memory["id"])
    elif operation == WriteOperation.ADD and best_memory and conflict_score >= 0.75:
        operation = WriteOperation.UPDATE
        target_memory_id = str(best_memory["id"])
    elif operation == WriteOperation.DELETE and not target_memory_id and best_memory:
        target_memory_id = str(best_memory["id"])

    if operation in {WriteOperation.UPDATE, WriteOperation.MERGE, WriteOperation.DELETE} and not target_memory_id:
        operation = WriteOperation.REVIEW

    return replace(
        candidate,
        operation=operation,
        target_memory_id=target_memory_id,
        scores=replace(scores, novelty=novelty, conflict_score=conflict_score),
    )


def most_similar_memory(summary: str, related_memories: list[JsonDict]) -> tuple[JsonDict | None, float]:
    query_tokens = set(search_tokens(summary))
    if not query_tokens:
        return None, 0.0
    best: JsonDict | None = None
    best_score = 0.0
    for memory in related_memories:
        memory_text = " ".join(
            [
                str(memory.get("summary") or ""),
                str(memory.get("type") or ""),
                " ".join(str(tag) for tag in memory.get("tags", [])),
            ]
        )
        memory_tokens = set(search_tokens(memory_text))
        if not memory_tokens:
            continue
        overlap = len(query_tokens & memory_tokens) / max(len(query_tokens | memory_tokens), 1)
        containment = len(query_tokens & memory_tokens) / max(min(len(query_tokens), len(memory_tokens)), 1)
        tag_bonus = 0.04 if set(memory.get("tags", [])) & query_tokens else 0.0
        score = min(1.0, (overlap * 0.7) + (containment * 0.3) + tag_bonus)
        if score > best_score:
            best = memory
            best_score = score
    return best, best_score


def _deterministic_conflict(summary: str, similarity: float) -> float:
    lowered = summary.lower()
    has_conflict_cue = any(cue in lowered or cue in summary for cue in CONFLICT_CUES)
    has_delete_cue = any(cue in lowered or cue in summary for cue in DELETE_CUES)
    if has_delete_cue and similarity > 0.15:
        return 0.85
    if has_conflict_cue and similarity > 0.15:
        return 0.78
    return 0.0
