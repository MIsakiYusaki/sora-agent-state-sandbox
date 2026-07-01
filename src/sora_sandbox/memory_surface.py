from __future__ import annotations

from .memory_schema import FusedMemoryHit, JsonDict, RecallPlan


def choose_surface_action(plan: RecallPlan, selected: list[tuple[float, JsonDict, tuple[str, ...]]]) -> str:
    if not selected:
        return "none"
    if plan.explicit_recall:
        return "gist"
    return "implicit"


def decision_reason(surface_action: str, has_items: bool) -> str:
    if surface_action == "gist":
        return "explicit recall request with governed gist"
    if has_items:
        return "relevant canonical memories used implicitly"
    return "no sufficiently relevant canonical memory"


def build_recollection_items(
    selected: list[tuple[float, JsonDict, tuple[str, ...]]],
) -> list[JsonDict]:
    return [
        {
            "memory_id": memory["id"],
            "type": memory["type"],
            "gist": memory["summary"],
            "confidence": memory["confidence"],
            "evidence_strength": "strong" if memory["evidence_ids"] else "weak",
            "use_permission": memory["use_permission"],
            "disclosure_permission": memory["disclosure_permission"],
            "matched_backends": list(matched_backends),
            "score": round(score, 4),
        }
        for score, memory, matched_backends in selected
    ]
