from __future__ import annotations

import re
from dataclasses import dataclass


EXPLICIT_MEMORY_PATTERNS = (
    re.compile(r"(?:请|帮我)?记住[：:，,\s]*(?P<content>.+)", re.IGNORECASE),
    re.compile(r"remember(?:\s+that)?[：:，,\s]*(?P<content>.+)", re.IGNORECASE),
)

PREFERENCE_PATTERNS = (
    re.compile(r"我(?:更)?(?:喜欢|偏好|希望|不喜欢|讨厌)(?P<content>[^。！？!?]+)"),
    re.compile(r"我(?:想要|需要|期待)(?P<content>[^。！？!?]+)"),
)

GOAL_PATTERNS = (
    re.compile(r"我(?:打算|准备|计划|接下来要)(?P<content>[^。！？!?]+)"),
    re.compile(r"我们的(?:目标|计划|下一步)是?(?P<content>[^。！？!?]+)"),
)

PROJECT_PATTERNS = (
    re.compile(r"这个项目(?:应该|要|需要)(?P<content>[^。！？!?]+)"),
    re.compile(r"我们(?:应该|要|需要)(?P<content>[^。！？!?]*(?:架构|设计|实现|治理|召回|记忆)[^。！？!?]*)"),
)


@dataclass(frozen=True)
class MemoryExtraction:
    content: str
    memory_type: str
    tags: tuple[str, ...] = ()
    confidence: float = 0.75
    reason: str = "rule_extraction"
    explicit_user_request: bool = False


def extract_explicit_memory(user_input: str) -> str | None:
    for pattern in EXPLICIT_MEMORY_PATTERNS:
        match = pattern.search(user_input)
        if match:
            return match.group("content").strip("。.!！ ")
    return None


def extract_memory_candidates(user_input: str, reply: str = "") -> list[MemoryExtraction]:
    text = user_input.strip()
    if not text:
        return []

    explicit_content = extract_explicit_memory(text)
    if explicit_content:
        return [
            MemoryExtraction(
                content=explicit_content,
                memory_type="auto",
                confidence=1.0,
                reason="user_explicitly_requested_memory",
                explicit_user_request=True,
            )
        ]

    candidates: list[MemoryExtraction] = []
    candidates.extend(
        _extract_by_patterns(
            text,
            PREFERENCE_PATTERNS,
            memory_type="preference",
            tags=("implicit_user_statement",),
            confidence=0.82,
            reason="implicit_preference_statement",
        )
    )
    candidates.extend(
        _extract_by_patterns(
            text,
            GOAL_PATTERNS,
            memory_type="goal",
            tags=("implicit_user_statement",),
            confidence=0.84,
            reason="implicit_goal_statement",
        )
    )
    candidates.extend(
        _extract_by_patterns(
            text,
            PROJECT_PATTERNS,
            memory_type="project_principle",
            tags=("project_sora", "implicit_user_statement"),
            confidence=0.86,
            reason="implicit_project_principle_statement",
        )
    )
    return _dedupe_extractions(candidates)


def _extract_by_patterns(
    text: str,
    patterns: tuple[re.Pattern[str], ...],
    *,
    memory_type: str,
    tags: tuple[str, ...],
    confidence: float,
    reason: str,
) -> list[MemoryExtraction]:
    candidates: list[MemoryExtraction] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            content = _clean_candidate(match.group(0))
            if not _is_worth_remembering(content):
                continue
            candidates.append(
                MemoryExtraction(
                    content=content,
                    memory_type=memory_type,
                    tags=tags,
                    confidence=confidence,
                    reason=reason,
                    explicit_user_request=False,
                )
            )
    return candidates


def _clean_candidate(value: str) -> str:
    return value.strip("。.!！ ，,")


def _is_worth_remembering(value: str) -> bool:
    if len(value) < 6:
        return False
    throwaway_terms = ("哈哈", "随便", "算了", "没事", "不用了")
    return not any(term == value or value.endswith(term) for term in throwaway_terms)


def _dedupe_extractions(items: list[MemoryExtraction]) -> list[MemoryExtraction]:
    seen: set[tuple[str, str]] = set()
    deduped: list[MemoryExtraction] = []
    for item in items:
        key = (item.memory_type, item.content)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped
