from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .memory_schema import FusedMemoryHit, JsonDict, json_loads


CONTEXT_FEATURE_CONTRACT = "RecallContextFeatures.v1"


@dataclass(frozen=True)
class BoundedContextualRecallPolicy:
    """Apply optional typed context without allowing it to replace relevance."""

    policy_version: str = "bounded_contextual_recall.v1"
    hard_max_relative_boost: float = 0.10
    max_features: int = 8

    def apply(
        self,
        hits: list[FusedMemoryHit],
        *,
        context_features: Mapping[str, Any] | None,
        top_k: int,
    ) -> tuple[list[FusedMemoryHit], JsonDict]:
        context = dict(context_features or {})
        if not context or context.get("schema_version") != CONTEXT_FEATURE_CONTRACT:
            return list(hits), self._metadata(applied=False, reason="context_features_absent")

        configured_cap = _bounded(float(context.get("max_relative_boost", self.hard_max_relative_boost)))
        max_boost = min(self.hard_max_relative_boost, configured_cap)
        features = list(context.get("features") or [])[: self.max_features]
        rescored: list[FusedMemoryHit] = []
        score_details: list[JsonDict] = []
        for hit in hits:
            tags = _tags(hit.row)
            memory_type = str(hit.row["type"])
            applied_features: list[str] = []
            total_boost = 0.0
            for raw_feature in features:
                feature = dict(raw_feature or {})
                if not _matches(feature, tags=tags, memory_type=memory_type):
                    continue
                weight = min(max_boost, _bounded(float(feature.get("relative_boost", 0.0))))
                if weight <= 0.0:
                    continue
                total_boost = min(max_boost, total_boost + weight)
                applied_features.append(str(feature.get("name") or "unnamed_context_feature"))
            score = float(hit.score) * (1.0 + total_boost)
            rescored.append(
                FusedMemoryHit(
                    memory_id=hit.memory_id,
                    score=score,
                    row=hit.row,
                    matched_backends=hit.matched_backends,
                )
            )
            score_details.append(
                {
                    "memory_id": hit.memory_id,
                    "base_score": hit.score,
                    "contextual_score": score,
                    "relative_boost": total_boost,
                    "applied_features": applied_features,
                }
            )
        rescored.sort(key=lambda item: item.score, reverse=True)
        diversified, diversity = _apply_diversity_guard(
            rescored,
            top_k=top_k,
            config=dict(context.get("diversity") or {}),
        )
        return diversified, self._metadata(
            applied=True,
            reason="bounded_features_applied",
            score_details=score_details,
            diversity=diversity,
            max_relative_boost=max_boost,
            source=str(context.get("source") or "typed_context"),
            source_revision=context.get("source_revision"),
        )

    def _metadata(self, *, applied: bool, reason: str, **extra: Any) -> JsonDict:
        return {
            "policy_version": self.policy_version,
            "applied": applied,
            "reason": reason,
            **extra,
        }


def _apply_diversity_guard(
    hits: list[FusedMemoryHit],
    *,
    top_k: int,
    config: JsonDict,
) -> tuple[list[FusedMemoryHit], JsonDict]:
    threshold = max(1, int(config.get("when_top_k_at_least", 4)))
    limit = min(max(0, top_k), len(hits))
    if not config.get("enabled", True) or limit < threshold or len(hits) <= limit:
        return hits, {"applied": False, "reason": "below_threshold_or_no_alternative"}

    selected = list(hits[:limit])
    remainder = list(hits[limit:])
    swaps: list[JsonDict] = []
    guard_tag = str(config.get("guard_tag") or "")
    max_tag_share = max(0.0, min(1.0, float(config.get("max_tag_share", 0.75))))
    if guard_tag:
        max_tagged = max(1, int(limit * max_tag_share))
        while sum(guard_tag in _tags(item.row) for item in selected) > max_tagged:
            outgoing_index = next(
                (index for index in range(len(selected) - 1, -1, -1) if guard_tag in _tags(selected[index].row)),
                None,
            )
            incoming_index = next(
                (index for index, item in enumerate(remainder) if guard_tag not in _tags(item.row)),
                None,
            )
            if outgoing_index is None or incoming_index is None:
                break
            outgoing = selected[outgoing_index]
            incoming = remainder.pop(incoming_index)
            selected[outgoing_index] = incoming
            remainder.append(outgoing)
            swaps.append(
                {"reason": "guard_tag_share", "out": outgoing.memory_id, "in": incoming.memory_id}
            )

    min_types = max(1, int(config.get("min_distinct_types", 2)))
    selected_types = {str(item.row["type"]) for item in selected}
    while len(selected_types) < min_types:
        incoming_index = next(
            (index for index, item in enumerate(remainder) if str(item.row["type"]) not in selected_types),
            None,
        )
        if incoming_index is None:
            break
        duplicate_counts = {
            memory_type: sum(str(item.row["type"]) == memory_type for item in selected)
            for memory_type in selected_types
        }
        outgoing_index = next(
            (
                index
                for index in range(len(selected) - 1, -1, -1)
                if duplicate_counts[str(selected[index].row["type"])] > 1
            ),
            None,
        )
        if outgoing_index is None:
            break
        outgoing = selected[outgoing_index]
        incoming = remainder.pop(incoming_index)
        selected[outgoing_index] = incoming
        remainder.append(outgoing)
        selected_types = {str(item.row["type"]) for item in selected}
        swaps.append({"reason": "type_grounding", "out": outgoing.memory_id, "in": incoming.memory_id})

    selected_ids = {item.memory_id for item in selected}
    ordered_remainder = [item for item in hits if item.memory_id not in selected_ids]
    return selected + ordered_remainder, {
        "applied": bool(swaps),
        "reason": "grounded" if swaps else "already_diverse",
        "top_k": limit,
        "swaps": swaps,
    }


def _matches(feature: JsonDict, *, tags: set[str], memory_type: str) -> bool:
    match_tags = {str(item) for item in feature.get("match_tags") or []}
    match_types = {str(item) for item in feature.get("match_types") or []}
    if not match_tags and not match_types:
        return False
    return bool(tags.intersection(match_tags) or memory_type in match_types)


def _tags(row: Any) -> set[str]:
    return {str(item) for item in json_loads(str(row["tags_json"] or "[]"), [])}


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, value))
