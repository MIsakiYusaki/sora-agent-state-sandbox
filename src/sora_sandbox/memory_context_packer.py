from __future__ import annotations

import re
from dataclasses import dataclass

from .memory_schema import JsonDict
from .memory_surface_policy import (
    DEFAULT_SURFACE_PROFILE,
    DeterministicSurfacePolicyProvider,
    SurfacePolicyProvider,
)


@dataclass(frozen=True)
class PackedMemory:
    score: float
    memory: JsonDict
    matched_backends: tuple[str, ...]
    why_recalled: str


class MemoryContextPacker:
    def __init__(self, provider: SurfacePolicyProvider | None = None) -> None:
        self.provider = provider or DeterministicSurfacePolicyProvider()

    def pack(
        self,
        candidates: list[PackedMemory],
        *,
        token_budget: int,
        explicit_recall: bool,
        surface_action: str | None = None,
        legacy_surface_action: str | None = None,
        policy_by_memory_id: dict[str, JsonDict] | None = None,
        policy_profile: str = DEFAULT_SURFACE_PROFILE,
        forbidden_disclosures: list[str] | None = None,
        pre_suppressed: list[JsonDict] | None = None,
    ) -> JsonDict:
        budget = max(int(token_budget), 0)
        selected: list[JsonDict] = []
        suppressed: list[JsonDict] = list(pre_suppressed or [])
        provider_usage: list[JsonDict] = []
        used = 0
        pools = _budget_pools(budget)
        pool_used = {
            "memory_gist": 0,
            "why_recalled": 0,
            "policy_overhead": 0,
        }
        seen: set[str] = set()
        policies = policy_by_memory_id or {}
        for candidate in candidates:
            memory = candidate.memory
            dedupe_key = _dedupe_key(memory)
            if dedupe_key in seen:
                suppressed.append({"memory_id": memory["id"], "reason": "dedupe"})
                continue
            item = self._build_item(
                candidate,
                explicit_recall=explicit_recall,
                policy=policies.get(str(memory["id"])),
            )
            if item["surface_action"] == "relationship_context":
                relationship_result = self._relationship_gist(candidate, item)
                if relationship_result.get("text"):
                    item["gist"] = relationship_result["text"]
                if relationship_result.get("provider_trace"):
                    item.setdefault("provider_traces", []).append(relationship_result["provider_trace"])
                    provider_usage.append(relationship_result["provider_trace"])
            why_tokens = _estimate_tokens(item["why_recalled"])
            if budget and pool_used["why_recalled"] + why_tokens > pools["why_recalled"]:
                remaining_why = max(pools["why_recalled"] - pool_used["why_recalled"], 1)
                item["why_recalled"] = _truncate_to_budget(item["why_recalled"], remaining_why)
                why_tokens = _estimate_tokens(item["why_recalled"])

            gist_tokens = _estimate_tokens(item["gist"])
            remaining_gist = max(pools["memory_gist"] - pool_used["memory_gist"], 0)
            if budget and gist_tokens > remaining_gist:
                if selected and remaining_gist <= 0:
                    suppressed.append({"memory_id": memory["id"], "reason": "memory_gist_budget"})
                    continue
                reserved_overhead = max(
                    min(12, max(pools["policy_overhead"] - pool_used["policy_overhead"], 0)),
                    1,
                )
                available_for_gist = max(
                    min(remaining_gist or pools["memory_gist"], budget - why_tokens - reserved_overhead),
                    1,
                )
                compression = self._compress_gist(
                    item["gist"],
                    available_for_gist,
                    memory_id=str(memory["id"]),
                    reason="memory_gist_budget",
                )
                item["gist"] = compression["text"]
                if compression.get("provider_trace"):
                    item.setdefault("provider_traces", []).append(compression["provider_trace"])
                    provider_usage.append(compression["provider_trace"])
                gist_tokens = _estimate_tokens(item["gist"])

            overhead_tokens = min(12, max(pools["policy_overhead"] - pool_used["policy_overhead"], 0) or 12)
            item_tokens = gist_tokens + why_tokens + overhead_tokens
            if budget and used + item_tokens > budget and selected:
                suppressed.append({"memory_id": memory["id"], "reason": "token_budget"})
                continue
            if budget and item_tokens > budget:
                why_tokens = _estimate_tokens(item["why_recalled"])
                available_for_gist = max(budget - why_tokens - 12, 1)
                compression = self._compress_gist(
                    item["gist"],
                    available_for_gist,
                    memory_id=str(memory["id"]),
                    reason="token_budget",
                )
                item["gist"] = compression["text"]
                if compression.get("provider_trace"):
                    item.setdefault("provider_traces", []).append(compression["provider_trace"])
                    provider_usage.append(compression["provider_trace"])
                item_tokens = _estimate_tokens(item["gist"]) + _estimate_tokens(item["why_recalled"]) + 12
                if item_tokens > budget:
                    item["why_recalled"] = "matched current turn"
                    available_for_gist = max(budget - _estimate_tokens(item["why_recalled"]) - 12, 1)
                    compression = self._compress_gist(
                        item["gist"],
                        available_for_gist,
                        memory_id=str(memory["id"]),
                        reason="token_budget_fallback",
                    )
                    item["gist"] = compression["text"]
                    if compression.get("provider_trace"):
                        item.setdefault("provider_traces", []).append(compression["provider_trace"])
                        provider_usage.append(compression["provider_trace"])
                    item_tokens = min(
                        budget,
                        _estimate_tokens(item["gist"]) + _estimate_tokens(item["why_recalled"]) + 12,
                    )
            item["token_estimate"] = item_tokens
            item["budget_pool_usage"] = {
                "memory_gist": gist_tokens,
                "why_recalled": why_tokens,
                "policy_overhead": overhead_tokens,
            }
            selected.append(item)
            seen.add(dedupe_key)
            used += item_tokens
            pool_used["memory_gist"] += gist_tokens
            pool_used["why_recalled"] += why_tokens
            pool_used["policy_overhead"] += overhead_tokens
        action = surface_action or ("answer_memory_question" if explicit_recall and selected else "implicit_continuity" if selected else "none")
        legacy_action = legacy_surface_action or ("gist" if explicit_recall and selected else "implicit" if selected else "none")
        forbidden = sorted(set(forbidden_disclosures or (["raw_evidence"] if selected else [])))
        context_frame = {
            "version": "memory_context_frame_v2",
            "policy_profile": policy_profile,
            "surface_action": action,
            "legacy_surface_action": legacy_action,
            "items": selected,
            "forbidden_disclosures": forbidden,
            "budget": {
                "token_budget": budget,
                "token_used": used,
                "pools": pools,
                "pool_used": pool_used,
                "pool_enforcement": "hard_gist_soft_why_response_reserved",
            },
            "provider_usage": provider_usage,
            "packing_strategy": "template_budgeted_v2",
        }
        return {
            "items": selected,
            "suppressed": suppressed,
            "token_budget": budget,
            "token_used": used,
            "packing_strategy": "template_budgeted_v2",
            "context_frame": context_frame,
            "forbidden_disclosures": forbidden,
            "provider_usage": provider_usage,
        }

    def _build_item(
        self,
        candidate: PackedMemory,
        *,
        explicit_recall: bool,
        policy: JsonDict | None = None,
    ) -> JsonDict:
        memory = candidate.memory
        policy_payload = policy or {
            "action": "answer_memory_question" if explicit_recall else "implicit_continuity",
            "legacy_action": "gist" if explicit_recall else "implicit",
            "may_quote": False,
            "may_disclose_gist": True,
            "forbidden_disclosures": ["raw_evidence"],
            "reason": "legacy_default_surface_policy",
        }
        action = str(policy_payload.get("action") or "implicit_continuity")
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
            "surface_policy": action,
            "surface_action": action,
            "legacy_surface_action": policy_payload.get("legacy_action"),
            "surface_instruction": _surface_instruction(action),
            "may_quote": bool(policy_payload.get("may_quote")),
            "may_disclose_gist": bool(policy_payload.get("may_disclose_gist", True)),
            "forbidden_disclosures": list(policy_payload.get("forbidden_disclosures", ["raw_evidence"])),
            "policy_reason": policy_payload.get("reason"),
        }

    def _relationship_gist(self, candidate: PackedMemory, item: JsonDict) -> JsonDict:
        try:
            result = self.provider.relationship_explain(
                {
                    "memory_id": item["memory_id"],
                    "summary": item["gist"],
                    "text": item["gist"],
                    "relationship_impact": candidate.memory.get("relationship_impact"),
                    "sensitivity": candidate.memory.get("sensitivity"),
                }
            )
            return {
                "text": str(result.get("text") or ""),
                "provider_trace": _provider_trace("relationship_explain", item["memory_id"], result),
            }
        except Exception as exc:
            return {
                "text": item["gist"],
                "provider_trace": {
                    "kind": "relationship_explain",
                    "memory_id": item["memory_id"],
                    "provider": getattr(self.provider, "name", self.provider.__class__.__name__),
                    "degraded": True,
                    "error": str(exc),
                },
            }

    def _compress_gist(self, text: str, token_budget: int, *, memory_id: str, reason: str) -> JsonDict:
        try:
            result = self.provider.compress(
                {
                    "memory_id": memory_id,
                    "text": text,
                    "token_budget": token_budget,
                    "reason": reason,
                }
            )
            compressed = str(result.get("text") or "")
            if compressed and _estimate_tokens(compressed) > token_budget:
                compressed = _truncate_to_budget(compressed, token_budget)
            return {
                "text": compressed if compressed else _truncate_to_budget(text, token_budget),
                "provider_trace": _provider_trace("compress", memory_id, result),
            }
        except Exception:
            return {
                "text": _truncate_to_budget(text, token_budget),
                "provider_trace": {
                    "kind": "compress",
                    "memory_id": memory_id,
                    "provider": getattr(self.provider, "name", self.provider.__class__.__name__),
                    "degraded": True,
                    "reason": reason,
                },
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


def _surface_instruction(action: str) -> str:
    if action == "silent_use":
        return "use_for_internal_continuity_only"
    if action == "implicit_continuity":
        return "use_naturally_without_over_disclosing_memory"
    if action == "relationship_context":
        return "use_for_relationship_continuity_without_overclaiming"
    if action == "redacted_gist":
        return "answer_with_redacted_gist_only"
    if action == "quote_memory":
        return "may_quote_only_if_item_allows_quote"
    if action == "answer_memory_question":
        return "answer_from_governed_gist"
    return "do_not_surface"


def _budget_pools(token_budget: int) -> JsonDict:
    if token_budget <= 0:
        return {
            "memory_gist": 0,
            "why_recalled": 0,
            "policy_overhead": 0,
            "response_reserve_hint": 0,
        }
    policy_overhead = min(80, max(2, token_budget // 8))
    why_recalled = min(90, max(4, token_budget // 5))
    if policy_overhead + why_recalled >= token_budget:
        policy_overhead = max(1, token_budget // 8)
        why_recalled = max(1, token_budget // 6)
    memory_gist = max(1, token_budget - policy_overhead - why_recalled)
    return {
        "memory_gist": memory_gist,
        "why_recalled": why_recalled,
        "policy_overhead": policy_overhead,
        "response_reserve_hint": max(64, min(240, token_budget // 3)),
    }


def _provider_trace(kind: str, memory_id: str, result: JsonDict) -> JsonDict:
    return {
        "kind": kind,
        "memory_id": memory_id,
        "provider": result.get("provider"),
        "model": result.get("model"),
        "prompt_version": result.get("prompt_version"),
        "degraded": bool(result.get("degraded")),
        "usage": result.get("usage", {}),
        "cost": result.get("cost", {}),
        "latency_ms": result.get("latency_ms", 0),
        "json_repair_pass": result.get("json_repair_pass", {"used": False}),
        "json_mode_retry": result.get("json_mode_retry", {"used": False}),
        "fidelity_notes": result.get("fidelity_notes", []),
        "boundary_notes": result.get("boundary_notes", []),
        "risk_flags": result.get("risk_flags", []),
        "error": result.get("error"),
    }
