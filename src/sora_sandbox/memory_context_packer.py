from __future__ import annotations

import re
from dataclasses import dataclass

from .memory_schema import JsonDict


@dataclass(frozen=True)
class PackedMemory:
    score: float
    memory: JsonDict
    matched_backends: tuple[str, ...]
    why_recalled: str


class MemoryContextPacker:
    def pack(
        self,
        candidates: list[PackedMemory],
        *,
        token_budget: int,
        explicit_recall: bool,
    ) -> JsonDict:
        budget = max(int(token_budget), 0)
        selected: list[JsonDict] = []
        suppressed: list[JsonDict] = []
        used = 0
        seen: set[str] = set()
        for candidate in candidates:
            memory = candidate.memory
            dedupe_key = _dedupe_key(memory)
            if dedupe_key in seen:
                suppressed.append({"memory_id": memory["id"], "reason": "dedupe"})
                continue
            item = self._build_item(candidate, explicit_recall=explicit_recall)
            item_tokens = _estimate_tokens(item["gist"]) + _estimate_tokens(item["why_recalled"]) + 12
            if budget and used + item_tokens > budget and selected:
                suppressed.append({"memory_id": memory["id"], "reason": "token_budget"})
                continue
            if budget and item_tokens > budget:
                why_tokens = _estimate_tokens(item["why_recalled"])
                available_for_gist = max(budget - why_tokens - 12, 1)
                item["gist"] = _truncate_to_budget(item["gist"], available_for_gist)
                item_tokens = _estimate_tokens(item["gist"]) + _estimate_tokens(item["why_recalled"]) + 12
                if item_tokens > budget:
                    item["why_recalled"] = "matched current turn"
                    available_for_gist = max(budget - _estimate_tokens(item["why_recalled"]) - 12, 1)
                    item["gist"] = _truncate_to_budget(item["gist"], available_for_gist)
                    item_tokens = min(
                        budget,
                        _estimate_tokens(item["gist"]) + _estimate_tokens(item["why_recalled"]) + 12,
                    )
            selected.append(item)
            seen.add(dedupe_key)
            used += item_tokens
        return {
            "items": selected,
            "suppressed": suppressed,
            "token_budget": budget,
            "token_used": used,
            "packing_strategy": "template_deterministic_v1",
        }

    def _build_item(self, candidate: PackedMemory, *, explicit_recall: bool) -> JsonDict:
        memory = candidate.memory
        return {
            "memory_id": memory["id"],
            "type": memory["type"],
            "gist": str(memory["summary"]),
            "confidence": memory["confidence"],
            "importance": memory["importance"],
            "evidence_strength": "strong" if memory.get("evidence_ids") else "weak",
            "use_permission": memory["use_permission"],
            "disclosure_permission": memory["disclosure_permission"],
            "matched_backends": list(candidate.matched_backends),
            "score": round(candidate.score, 4),
            "why_recalled": candidate.why_recalled,
            "surface_policy": "allow_gist" if explicit_recall else "allow_implicit",
        }


def _dedupe_key(memory: JsonDict) -> str:
    summary = re.sub(r"\s+", " ", str(memory.get("summary") or "").lower()).strip()
    return f"{memory.get('type')}:{summary[:96]}"


def _estimate_tokens(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    ascii_chars = sum(1 for char in stripped if ord(char) < 128)
    non_ascii_chars = len(stripped) - ascii_chars
    return max(1, ascii_chars // 4 + non_ascii_chars // 2)


def _truncate_to_budget(text: str, budget: int) -> str:
    if _estimate_tokens(text) <= budget:
        return text
    limit = max(16, budget * 2)
    return text[:limit].rstrip() + "..."
