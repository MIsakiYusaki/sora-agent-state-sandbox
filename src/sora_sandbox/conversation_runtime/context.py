from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import (
    DISCLOSURE_RANK,
    JsonDict,
    UnifiedDisclosureLevel,
    most_restrictive_disclosure,
)


GENERATION_CONTEXT_MARKER = "_conversation_runtime_generation_context"


class ContextBudgetExceeded(RuntimeError):
    """Raised when a prompt cannot be made safe within the configured budget."""


@dataclass(frozen=True)
class ContextBudget:
    total_tokens: int = 12_000
    response_reserve: int = 1_200
    system_reserve: int = 1_500
    recent_turns: int = 3_200
    memory: int = 2_400
    domain_frames: int = 2_600
    control_frames: int = 2_200

    def __post_init__(self) -> None:
        values = {
            "total_tokens": self.total_tokens,
            "response_reserve": self.response_reserve,
            "system_reserve": self.system_reserve,
            "recent_turns": self.recent_turns,
            "memory": self.memory,
            "domain_frames": self.domain_frames,
            "control_frames": self.control_frames,
        }
        invalid = [name for name, value in values.items() if value < 0]
        if invalid:
            raise ValueError(f"context budgets must be non-negative: {', '.join(invalid)}")
        if self.total_tokens <= self.response_reserve + self.system_reserve:
            raise ValueError("total_tokens must exceed response and system reserves")

    @property
    def injectable_tokens(self) -> int:
        return max(0, self.total_tokens - self.response_reserve - self.system_reserve)


@dataclass(frozen=True)
class PackedContext:
    context: JsonDict
    token_estimate: int
    pools: JsonDict
    disclosure: JsonDict
    dropped: tuple[str, ...]
    reply_budget_tokens: int

    def to_dict(self) -> JsonDict:
        return {
            "schema_version": "PackedConversationContext.v1",
            "context": self.context,
            "token_estimate": self.token_estimate,
            "pools": self.pools,
            "disclosure": self.disclosure,
            "dropped": list(self.dropped),
            "reply_budget_tokens": self.reply_budget_tokens,
        }


class ConversationContextPacker:
    DOMAIN_KEYS = (
        "persona_frame",
        "affect_frame",
        "relationship_frame",
        "worldops_frame",
        "origin_frame",
    )
    CONTROL_KEYS = (
        "turn_frame",
        "turn_interpretation",
        "context_use_policy",
        "dialogue_control_frame",
        "interruptibility_frame",
        "arbitration_result",
    )

    def __init__(self, budget: ContextBudget | None = None) -> None:
        self.budget = budget or ContextBudget()

    def pack_for_generation(self, context_pack: Mapping[str, Any]) -> JsonDict:
        """Returns the only context shape that may be passed to response generation."""
        packed = self.pack(context_pack)
        context = copy.deepcopy(packed.context)
        context_hash = generation_context_hash(context)
        context[GENERATION_CONTEXT_MARKER] = {
            "schema_version": "GenerationContextGate.v1",
            "token_estimate": packed.token_estimate,
            "reply_budget_tokens": packed.reply_budget_tokens,
            "disclosure": packed.disclosure,
            "dropped": list(packed.dropped),
            "context_hash": context_hash,
        }
        return context

    def pack(self, context_pack: Mapping[str, Any]) -> PackedContext:
        source = copy.deepcopy(dict(context_pack))
        source.pop(GENERATION_CONTEXT_MARKER, None)
        source = self._project_prompt_views(source)
        disclosure = self._disclosure_matrix(source)
        self._apply_disclosure(source, disclosure)
        pools: JsonDict = {}
        dropped: list[str] = []

        session = dict(source.get("session_context") or {})
        recent = list(session.get("recent_turns") or [])
        recent = self._fit_items(recent, self.budget.recent_turns)
        session["recent_turns"] = recent
        session["history_items"] = len(recent)
        session["history_rounds"] = len(recent) // 2
        source["session_context"] = session
        pools["recent_turns"] = estimate_tokens(recent)

        recollection = dict(source.get("recollection_frame") or {})
        memory_items = [
            _select(
                dict(item),
                "memory_id",
                "id",
                "memory_type",
                "type",
                "summary",
                "gist",
                "surface_action",
                "confidence",
                "reason",
            )
            for item in list(recollection.get("items") or [])
            if isinstance(item, Mapping)
        ]
        memory_items = self._fit_items(memory_items, self.budget.memory)
        recollection["items"] = memory_items
        remaining_memory_budget = max(0, self.budget.memory - estimate_tokens(memory_items))
        memory_summaries = [
            (
                _select(dict(item), "memory_id", "id", "memory_type", "type", "summary", "gist")
                if isinstance(item, Mapping)
                else str(item)
            )
            for item in list(recollection.get("memory_summaries") or [])
        ]
        recollection["memory_summaries"] = self._fit_items(
            memory_summaries, remaining_memory_budget
        )
        source["recollection_frame"] = recollection
        pools["memory"] = estimate_tokens(recollection)

        self._fit_domain_frames(source, dropped)
        control_payload = {key: source.get(key) for key in self.CONTROL_KEYS if key in source}
        control_tokens = estimate_tokens(control_payload)
        if control_tokens > self.budget.control_frames:
            raise ContextBudgetExceeded(
                f"control frames require {control_tokens} tokens; budget is {self.budget.control_frames}"
            )

        while estimate_tokens(source) > self.budget.injectable_tokens:
            if source.get("signals"):
                source["signals"] = list(source["signals"])[:-1]
                dropped.append("lowest_priority_signal")
                continue
            memories = dict(source.get("recollection_frame") or {})
            if memories.get("memory_summaries"):
                memories["memory_summaries"] = list(memories["memory_summaries"])[:-1]
                source["recollection_frame"] = memories
                dropped.append("lowest_ranked_memory")
                continue
            if memories.get("items"):
                memories["items"] = list(memories["items"])[:-1]
                source["recollection_frame"] = memories
                dropped.append("lowest_ranked_memory_item")
                continue
            turns = dict(source.get("session_context") or {})
            if len(turns.get("recent_turns") or []) > 2:
                turns["recent_turns"] = list(turns["recent_turns"])[2:]
                source["session_context"] = turns
                dropped.append("oldest_conversation_round")
                continue
            events = dict(source.get("event_bus") or {})
            if events.get("events"):
                events["events"] = list(events["events"])[:-1]
                source["event_bus"] = events
                dropped.append("oldest_event_bus_item")
                continue
            raise ContextBudgetExceeded(
                "context cannot fit the injectable token budget without dropping control data"
            )

        domain_payload = {key: source.get(key) for key in self.DOMAIN_KEYS if key in source}
        control_payload = {key: source.get(key) for key in self.CONTROL_KEYS if key in source}
        pools["recent_turns"] = estimate_tokens(
            dict(source.get("session_context") or {}).get("recent_turns") or []
        )
        pools["memory"] = estimate_tokens(source.get("recollection_frame") or {})
        pools["domain_frames"] = estimate_tokens(domain_payload)
        pools["control_frames"] = estimate_tokens(control_payload)
        pools["total_injectable"] = estimate_tokens(source)

        return PackedContext(
            context=source,
            token_estimate=estimate_tokens(source),
            pools=pools,
            disclosure={key: value.value for key, value in disclosure.items()},
            dropped=tuple(dropped),
            reply_budget_tokens=self.budget.response_reserve,
        )

    def _fit_domain_frames(self, source: JsonDict, dropped: list[str]) -> None:
        def domain_tokens() -> int:
            return estimate_tokens(
                {key: source.get(key) for key in self.DOMAIN_KEYS if key in source}
            )

        list_paths = (
            ("worldops_frame", "action_options"),
            ("persona_frame", "self_interests"),
            ("worldops_frame", "allowed_facts"),
            ("relationship_frame", "expression_hints"),
            ("persona_frame", "judgment_directives"),
            ("worldops_frame", "blocked_facts"),
            ("worldops_frame", "forbidden_behaviors"),
        )
        verbose_paths = (
            ("worldops_frame", "influence_summary"),
            ("persona_frame", "narrative_gist"),
            ("relationship_frame", "surface_summary"),
            ("origin_frame", "influence_summary"),
        )
        while domain_tokens() > self.budget.domain_frames:
            changed = False
            for frame_key, field in list_paths:
                frame = dict(source.get(frame_key) or {})
                items = list(frame.get(field) or [])
                if items:
                    frame[field] = items[:-1]
                    source[frame_key] = frame
                    dropped.append(f"{frame_key}.{field}.tail")
                    changed = True
                    break
            if changed:
                continue
            for frame_key, field in verbose_paths:
                frame = dict(source.get(frame_key) or {})
                if field in frame:
                    frame.pop(field, None)
                    source[frame_key] = frame
                    dropped.append(f"{frame_key}.{field}")
                    changed = True
                    break
            if changed:
                continue
            candidates = [key for key in self.DOMAIN_KEYS if source.get(key)]
            if not candidates:
                break
            largest = max(candidates, key=lambda key: estimate_tokens(source[key]))
            compact = _minimal_domain_frame(largest, dict(source.get(largest) or {}))
            if compact == source.get(largest):
                break
            source[largest] = compact
            dropped.append(f"{largest}.safe_fallback")
        if domain_tokens() > self.budget.domain_frames:
            raise ContextBudgetExceeded(
                "domain frames cannot fit their budget after safe fallback projection"
            )

    def _project_prompt_views(self, source: JsonDict) -> JsonDict:
        session = dict(source.get("session_context") or {})
        session.pop("scene_snapshot", None)
        source["session_context"] = session

        recollection = dict(source.get("recollection_frame") or {})
        source["recollection_frame"] = _select(
            recollection,
            "surface_action",
            "surface_action_v2",
            "items",
            "retrieved_ids",
            "memory_summaries",
            "evidence_level",
            "forbidden_disclosures",
            "decision_reason",
            "degraded",
        )

        persona = dict(source.get("persona_frame") or {})
        source["persona_frame"] = _select(
            persona,
            "schema_version",
            "runtime_mode",
            "snapshot_id",
            "snapshot_version",
            "core",
            "trait_bands",
            "expression_bias",
            "judgment_directives",
            "self_interests",
            "narrative_gist",
            "self_reference_candidate",
            "influence_summary",
            "use_policy",
            "growth_paused",
            "degraded_reasons",
        )

        affect = dict(source.get("affect_frame") or {})
        source["affect_frame"] = _select(
            affect,
            "schema_version",
            "runtime_mode",
            "snapshot_id",
            "state_revision",
            "core_band",
            "mood_band",
            "dominant_emotion",
            "capacity_bands",
            "need_bands",
            "regulation",
            "expression_bias",
            "state_surface_permission",
            "use_policy",
            "prompt_view",
            "degraded_reasons",
        )

        relationship = dict(source.get("relationship_frame") or {})
        source["relationship_frame"] = _select(
            relationship,
            "schema_version",
            "runtime_mode",
            "state_snapshot_version",
            "bond_stage",
            "stable_bands",
            "comfort_rhythm",
            "repair",
            "agreement",
            "expression_hints",
            "surface_summary",
            "surface_summary_descriptor",
            "surface_hints",
            "use_policy",
            "degraded_reasons",
        )

        worldops = dict(source.get("worldops_frame") or {})
        source["worldops_frame"] = _select(
            worldops,
            "schema_version",
            "runtime_mode",
            "world_id",
            "canon_version",
            "timeline_revision",
            "selected_event_id",
            "selected_event",
            "allowed_facts",
            "blocked_facts",
            "action_options",
            "expression",
            "expression_level",
            "expression_hint",
            "memory_policy",
            "contribution_policy",
            "state_surface_permission",
            "forbidden_behaviors",
            "influence_summary",
            "degraded_reasons",
        )

        origin = dict(source.get("origin_frame") or {})
        source["origin_frame"] = _select(
            origin,
            "template_id",
            "name",
            "summary",
            "worldview_anchor",
            "speech_habits",
            "influence_summary",
        )

        arbitration = dict(source.get("arbitration_result") or {})
        source["arbitration_result"] = _select(
            arbitration,
            "primary_mode",
            "selected_signals",
            "background_signals",
            "suppressed_signals",
            "response_directives",
            "semantic_judgment",
            "audit_reason",
        )
        selected_ids = set(source["arbitration_result"].get("selected_signals") or [])
        selected_ids.update(source["arbitration_result"].get("background_signals") or [])
        source["signals"] = [
            _select(
                dict(signal),
                "id",
                "source",
                "type",
                "content",
                "suggested_use",
                "priority",
                "confidence",
                "visibility",
            )
            for signal in list(source.get("signals") or [])
            if signal.get("id") in selected_ids
        ]
        source.pop("subsystem_health", None)
        event_bus = dict(source.get("event_bus") or {})
        bounded_events = []
        for event in list(event_bus.get("events") or []):
            event_copy = dict(event)
            payload = dict(event_copy.get("payload") or {})
            payload.pop("text", None)
            event_copy["payload"] = payload
            bounded_events.append(event_copy)
        event_bus["events"] = bounded_events
        source["event_bus"] = event_bus
        initiative = dict(source.get("initiative_window") or {})
        source["initiative_window"] = _select(
            initiative,
            "allowed",
            "reason",
            "allowed_types",
            "max_candidates",
            "question_budget",
        )
        policy = dict(source.get("policy_frame") or {})
        source["policy_frame"] = _select(
            policy,
            "external_action_allowed",
            "memory_write_mode",
            "persona_update_mode",
            "subsystem_runtime",
            "state_write_contract",
        )
        return source

    def _disclosure_matrix(self, context: JsonDict) -> dict[str, UnifiedDisclosureLevel]:
        interrupt = dict(context.get("interruptibility_frame") or {})
        surface = dict(interrupt.get("state_surface_permission") or {})
        memory_action = str(dict(context.get("recollection_frame") or {}).get("surface_action") or "implicit")
        return {
            "memory": most_restrictive_disclosure(
                _legacy_disclosure(surface.get("memory")), _memory_action_disclosure(memory_action)
            ),
            "persona": _legacy_disclosure(surface.get("persona")),
            "affect": _legacy_disclosure(surface.get("affect")),
            "relationship": _legacy_disclosure(surface.get("relationship")),
            "worldops": _legacy_disclosure(surface.get("worldops")),
        }

    @staticmethod
    def _apply_disclosure(
        context: JsonDict, disclosure: Mapping[str, UnifiedDisclosureLevel]
    ) -> None:
        memory_level = disclosure["memory"]
        recollection = dict(context.get("recollection_frame") or {})
        if DISCLOSURE_RANK[memory_level] <= DISCLOSURE_RANK[UnifiedDisclosureLevel.SILENT]:
            recollection["memory_summaries"] = []
            recollection["retrieved_ids"] = []
            recollection["surface_action"] = "none"
        elif memory_level == UnifiedDisclosureLevel.IMPLICIT:
            recollection["forbidden_disclosures"] = list(
                set(recollection.get("forbidden_disclosures") or []) | {"explicit_memory_reference", "raw_quote"}
            )
        elif memory_level == UnifiedDisclosureLevel.EXPLICIT_GIST:
            recollection["forbidden_disclosures"] = list(
                set(recollection.get("forbidden_disclosures") or []) | {"raw_quote"}
            )
        context["recollection_frame"] = recollection

        for domain in ("persona", "affect", "relationship", "worldops"):
            level = disclosure[domain]
            frame_key = f"{domain}_frame"
            frame = dict(context.get(frame_key) or {})
            frame["unified_disclosure_level"] = level.value
            if DISCLOSURE_RANK[level] <= DISCLOSURE_RANK[UnifiedDisclosureLevel.SILENT]:
                frame.pop("narrative_gist", None)
                frame.pop("self_reference_candidate", None)
                frame["surface_suppressed"] = True
            context[frame_key] = frame

    @staticmethod
    def _fit_items(items: list[Any], token_budget: int) -> list[Any]:
        selected: list[Any] = []
        used = 0
        for item in reversed(items):
            cost = estimate_tokens(item)
            if selected and used + cost > token_budget:
                continue
            if cost > token_budget and not selected:
                continue
            selected.append(item)
            used += cost
        selected.reverse()
        return selected


def estimate_tokens(value: Any) -> int:
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    ascii_count = sum(1 for character in serialized if ord(character) < 128)
    non_ascii_count = len(serialized) - ascii_count
    return max(1, (ascii_count + 3) // 4 + non_ascii_count)


def generation_context_hash(value: Mapping[str, Any]) -> str:
    context = copy.deepcopy(dict(value))
    context.pop(GENERATION_CONTEXT_MARKER, None)
    canonical = json.dumps(
        context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _minimal_domain_frame(frame_key: str, frame: JsonDict) -> JsonDict:
    common = _select(frame, "schema_version", "runtime_mode", "use_policy")
    common["prompt_truncated"] = True
    if frame_key == "persona_frame":
        common.update(
            _select(frame, "core", "trait_bands", "expression_bias", "growth_paused")
        )
    elif frame_key == "affect_frame":
        common.update(
            _select(
                frame,
                "core_band",
                "mood_band",
                "regulation",
                "prompt_view",
                "state_surface_permission",
            )
        )
    elif frame_key == "relationship_frame":
        common.update(_select(frame, "bond_stage", "repair", "agreement"))
    elif frame_key == "worldops_frame":
        common.update(
            _select(frame, "world_id", "state_surface_permission", "memory_policy")
        )
        common.update(
            {
                "expression_level": "L0",
                "surface_suppressed": True,
                "safe_fallback_reason": "worldops_frame_exceeded_prompt_budget",
            }
        )
    elif frame_key == "origin_frame":
        common.update(_select(frame, "template_id", "name"))
    return common


def _legacy_disclosure(value: Any) -> UnifiedDisclosureLevel:
    mapping = {
        None: UnifiedDisclosureLevel.IMPLICIT,
        "none": UnifiedDisclosureLevel.NONE,
        "deny": UnifiedDisclosureLevel.NONE,
        "silent": UnifiedDisclosureLevel.SILENT,
        "silent_only": UnifiedDisclosureLevel.SILENT,
        "implicit": UnifiedDisclosureLevel.IMPLICIT,
        "allow": UnifiedDisclosureLevel.EXPLICIT_GIST,
        "explicit": UnifiedDisclosureLevel.EXPLICIT_GIST,
        "explicit_gist": UnifiedDisclosureLevel.EXPLICIT_GIST,
        "explicit_quote": UnifiedDisclosureLevel.EXPLICIT_QUOTE,
    }
    return mapping.get(value, UnifiedDisclosureLevel.IMPLICIT)


def _memory_action_disclosure(action: str) -> UnifiedDisclosureLevel:
    mapping = {
        "none": UnifiedDisclosureLevel.NONE,
        "suppress": UnifiedDisclosureLevel.SILENT,
        "silent_use": UnifiedDisclosureLevel.SILENT,
        "implicit_continuity": UnifiedDisclosureLevel.IMPLICIT,
        "answer_memory_question": UnifiedDisclosureLevel.EXPLICIT_GIST,
        "explicit_recall": UnifiedDisclosureLevel.EXPLICIT_GIST,
        "quote": UnifiedDisclosureLevel.EXPLICIT_QUOTE,
    }
    return mapping.get(action, UnifiedDisclosureLevel.IMPLICIT)


def _select(source: Mapping[str, Any], *keys: str) -> JsonDict:
    return {key: source[key] for key in keys if key in source}
