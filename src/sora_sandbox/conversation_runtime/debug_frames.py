"""Bounded L0 frame values, never raw evidence, prompt text or model rationale.

Paths and enum values are source allowlists. Unknown fields/types fail closed.
Generation capture and historical delivery snapshots are deliberately distinct.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

FrameId = Literal["turn_frame", "turn_interpretation", "context_use_policy",
                  "interruptibility_frame", "dialogue_control_frame", "recollection_frame",
                  "persona_frame", "affect_frame", "relationship_frame", "worldops_frame",
                  "origin_frame", "session_frame", "arbitration_result"]
FrameSource = Literal["generation_sealed", "delivery_snapshot", "control_snapshot", "unobserved"]


class FrameContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class FrameFieldView(FrameContract):
    path: str = Field(max_length=96)
    state: Literal["observed", "missing", "protected", "invalid"]
    kind: Literal["scalar", "count", "reference_hash"]
    value: bool | int | float | str | None

    @model_validator(mode="after")
    def validate_value(self):
        if self.state != "observed" and self.value is not None:
            raise ValueError("unobserved fields cannot carry values")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("nonfinite frame value")
        return self


class DebugFrameProjection(FrameContract):
    schema_version: Literal["DebugFrameProjection.v1"] = "DebugFrameProjection.v1"
    frame_id: FrameId
    owner: Literal["conversation", "interruptibility", "memory", "persona", "affect", "relationship", "worldops"]
    source: FrameSource
    captured_at_ms: int | None = Field(ge=0)
    frame_schema_version: str | None
    field_count: int = Field(ge=0)
    protected_field_count: int = Field(ge=0)
    projection_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    fields: list[FrameFieldView] = Field(max_length=96)

    @model_validator(mode="after")
    def enforce_policy(self):
        rules = {**COMMON, **RULES[self.frame_id]}
        if self.owner != OWNERS.get(self.frame_id, "conversation"):
            raise ValueError("invalid frame owner")
        if self.frame_schema_version is not None and self.frame_schema_version not in SCHEMAS:
            raise ValueError("unapproved frame schema")
        seen = set()
        for row in self.fields:
            if row.path in seen or row.path not in rules and row.path not in REFERENCES:
                raise ValueError("unapproved or repeated field")
            seen.add(row.path)
            if row.path in REFERENCES:
                if row.kind != "reference_hash" or row.state == "observed" and not (isinstance(row.value, str) and re.fullmatch(r"[0-9a-f]{16}", row.value)):
                    raise ValueError("invalid reference projection")
            elif row.state == "observed" and row.value is not None:
                rule = rules[row.path]
                if row.kind != ("count" if rule == "count" else "scalar"):
                    raise ValueError("invalid field kind")
                valid = (isinstance(rule, set) and row.value in rule if isinstance(row.value, str) else False)
                valid = valid or rule == "bool" and type(row.value) is bool
                valid = valid or rule == "number" and type(row.value) in (int, float) and 0 <= row.value <= 1_000_000_000
                valid = valid or rule == "count" and type(row.value) is int and 0 <= row.value <= 1_000_000_000
                if not valid:
                    raise ValueError("unapproved field value")
        return self


class DesktopDebugFrameRequest(FrameContract):
    conversation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    turn_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    frame_id: FrameId


class DesktopFrameObservationRequest(FrameContract):
    enabled: bool


class DesktopFrameObservation(FrameContract):
    active: bool
    expires_at_ms: int | None = Field(ge=0)


class DesktopDebugFrameDetail(FrameContract):
    schema_version: Literal["DesktopDebugFrameDetail.v1"] = "DesktopDebugFrameDetail.v1"
    profile_id_hash: str = Field(pattern=r"^[0-9a-f]{16}$")
    conversation_id: str
    turn_id: str
    correlation_id: str
    collected_at_ms: int = Field(ge=0)
    projection: DebugFrameProjection
    budget: dict[str, int | None] = Field(default_factory=dict)
    observation: DesktopFrameObservation


SURFACE = {"silent", "implicit", "explicit", "explicit_gist", "explicit_raw", "none", "gist", "hint", "mention", "ask_consent", "suppress"}
MODES = {"active", "shadow", "disabled", "canonical_persona_system", "base_definition_fallback",
         "canonical_affect_system", "canonical_relationship_system", "canonical_worldops_system", "safe_fallback",
         "llm", "rule", "rules", "deterministic", "deterministic_fast_path", "llm_degraded", "fallback", "skipped", "text_first", "neutral_safe_fallback"}
BANDS = {"very_low", "low", "medium", "moderate", "high", "very_high", "neutral", "unknown", "balanced", "floor", "available"}
OWNERS = {"persona_frame": "persona", "affect_frame": "affect", "relationship_frame": "relationship",
          "worldops_frame": "worldops", "recollection_frame": "memory", "interruptibility_frame": "interruptibility",
          "dialogue_control_frame": "interruptibility", "context_use_policy": "interruptibility"}

# A rule is a source-constant enum, a strict boolean, a bounded nonnegative
# number, or a collection count. No generic string/dict recursive renderer.
COMMON = {"runtime_mode": MODES, "mode": MODES, "surface_suppressed": "bool",
          "unified_disclosure_level": SURFACE, "degraded_reasons": "count"}
RULES: dict[str, dict[str, Any]] = {
    "turn_frame": {"turn_kind": {"reactive", "proactive"}, "blocked_inferences": "count"},
    "turn_interpretation": {"mode": MODES, "context_reference.references_previous_context": "bool",
                            "context_reference.resolved_targets": "count", "context_reference.unresolved_references": "count"},
    "context_use_policy": {"memory": SURFACE, "persona": SURFACE, "affect": SURFACE, "relationship": SURFACE, "worldops": SURFACE},
    "interruptibility_frame": {
        "availability": {"unknown", "available", "busy", "focused", "away"},
        "response_obligation": {"required", "optional", "forbidden"},
        "expansion_permission": {"none", "minimal", "bounded", "allowed"},
        "follow_up_permission": {"none", "one", "bounded", "allowed"},
        "topic_shift_permission": {"none", "user_led", "bounded", "allowed"},
        "initiative_permission": {"none", "invited", "bounded", "allowed"},
        "state_surface_permission.memory": SURFACE, "state_surface_permission.persona": SURFACE,
        "state_surface_permission.affect": SURFACE, "state_surface_permission.relationship": SURFACE,
        "state_surface_permission.worldops": SURFACE,
        "response_shape": {"silent", "ack", "brief", "natural", "thorough", "normal", "concise"},
        "decision_path": {"deterministic", "rules", "model_assisted", "rule", "fallback", "hard_override", "bounded_model"},
        "model_assist.invoked": "bool", "model_assist.fallback_used": "bool",
        **{f"budgets.{key}": "number" for key in ("max_sentences", "question_budget", "topic_shift_budget", "state_surface_budget")},
        "expires_after_turn": "bool",
    },
    "dialogue_control_frame": {"question_budget": "number", "max_sentences": "number", "topic_shift_budget": "number",
                               "state_surface_budget": "number", "follow_up_allowed": "bool"},
    "recollection_frame": {"surface_action": SURFACE, "surface_action_v2": SURFACE, "degraded": "bool",
                           "items": "count", "memory_summaries": "count", "retrieved_ids": "count"},
    "persona_frame": {"snapshot_version": "number", "growth_paused": "bool", "snapshot_applied": "bool",
                      **{f"trait_bands.{key}": BANDS for key in ("warmth", "expressiveness", "playfulness", "curiosity", "caution", "autonomy")},
                      **{f"use_policy.{key}": "bool" for key in ("include_core", "include_traits", "include_expression_bias", "include_narrative_gist", "include_self_reference")}},
    "affect_frame": {"state_revision": "number", "state_surface_permission": SURFACE,
                     "core_band": {"bright_energized", "calm_content", "tense_watchful", "quiet_low", "neutral_alert", "neutral_calm"},
                     "mood_band": {"bright_energized", "calm_content", "tense_watchful", "quiet_low", "neutral_alert", "neutral_calm"},
                     **{f"{group}.{key}": BANDS for group, keys in (
                         ("capacity_bands", ("energy", "social", "cognitive", "patience", "social_capacity", "cognitive_capacity", "emotional_bandwidth")),
                         ("need_bands", ("rest", "connection", "stimulation", "autonomy"))) for key in keys}},
    "relationship_frame": {"state_snapshot_version": "number", "bond_stage": {"nascent", "familiar", "close", "intimate", "bonded", "committed"},
                           "repair.state": {"stable", "rupture_detected", "acknowledged", "repairing", "repaired", "unresolved", "closed"},
                           "use_policy.surface": SURFACE, "use_policy.explicit_attribution": "bool",
                           "use_policy.max_hint_count": "number",
                           **{f"stable_bands.{key}": BANDS for key in ("trust", "familiarity", "psychological_safety", "respect", "shared_history_density", "comfort_rhythm", "reciprocity_style", "boundary_preference")}},
    "worldops_frame": {"timeline_revision": "number", "expression_level": {"L0", "L1", "L2", "L3", "none", "silent", "implicit", "explicit"},
                       "expression.max_level": {"L0", "L1", "L2", "L3"}, "expression.initiative_allowed": "bool",
                       "state_surface_permission": SURFACE, "memory_policy.write_only_if_user_participates": "bool",
                       "allowed_facts": "count", "blocked_facts": "count", "action_options": "count"},
    "origin_frame": {}, "session_frame": {"history_items": "number", "history_rounds": "number"},
    "arbitration_result": {"primary_mode": {"reactive", "proactive", "silent", "respond", "answer", "companion"},
                           "selected_signals": "count", "background_signals": "count", "suppressed_signals": "count"},
}
SCHEMAS = {"PersonaFrame.v1", "AffectFrame.v2", "RelationshipFrame.v1", "WorldOpsFrame.v1",
           "interruptibility_frame.v1", "TurnInterpretation.v1", "turn_interpretation.v1", "TurnFrame.v1",
           "turn_frame.v1", "ContextUsePolicy.v1", "RecollectionFrame.v1", "SessionFrame.v1"}
REFERENCES = ("snapshot_id", "trace_id", "world_id", "selected_event_id", "template_id", "pair_ref")


def lookup(frame: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    value: Any = frame
    for key in path.split("."):
        if not isinstance(value, Mapping) or key not in value:
            return False, None
        value = value[key]
    return True, value


def project_frame(frame_id: str, frame: Mapping[str, Any], *, source: FrameSource,
                  captured_at_ms: int | None = None) -> DebugFrameProjection:
    if frame_id not in RULES:
        raise ValueError("unsupported frame")
    rules = {**COMMON, **RULES[frame_id]}
    rows: list[FrameFieldView] = []
    for path, rule in rules.items():
        present, value = lookup(frame, path)
        kind = "count" if rule == "count" else "scalar"
        if not present:
            state, safe = "missing", None
        elif value is None:
            state, safe = "observed", None  # explicit null is not an absent field
        elif rule == "count" and isinstance(value, (list, tuple, dict)):
            state, safe = "observed", len(value)
        elif rule == "bool" and type(value) is bool:
            state, safe = "observed", value
        elif rule == "number" and type(value) in (int, float) and 0 <= value <= 1_000_000_000 and math.isfinite(value):
            state, safe = "observed", value
        elif isinstance(rule, set) and isinstance(value, str) and value in rule:
            state, safe = "observed", value
        else:
            state, safe = "invalid", None
        rows.append(FrameFieldView(path=path, state=state, value=safe, kind=kind))
    for path in REFERENCES:
        present, value = lookup(frame, path)
        valid = isinstance(value, str) and 0 < len(value) <= 256
        rows.append(FrameFieldView(path=path, state="observed" if valid else "missing" if not present else "invalid",
                                   value=hashlib.sha256(value.encode()).hexdigest()[:16] if valid else None,
                                   kind="reference_hash"))
    allowed_roots = {path.split(".")[0] for path in rules} | set(REFERENCES) | {"schema_version"}
    protected = len(set(frame) - allowed_roots)
    # Only counts are exported for unapproved keys; keys themselves may contain
    # private text. Nested dictionaries are never exported as arbitrary JSON.
    schema = frame.get("schema_version")
    schema = schema if isinstance(schema, str) and schema in SCHEMAS else None
    basis = {"frame_id": frame_id, "source": source, "frame_schema_version": schema,
             "fields": [row.model_dump() for row in rows], "field_count": len(frame), "protected_field_count": protected}
    digest = hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return DebugFrameProjection(frame_id=frame_id, owner=OWNERS.get(frame_id, "conversation"), source=source,
                                captured_at_ms=captured_at_ms, frame_schema_version=schema, field_count=len(frame),
                                protected_field_count=protected, projection_revision=digest, fields=rows)


def project_generation(context: Mapping[str, Any], *, captured_at_ms: int) -> dict[str, Any]:
    frames = [project_frame(key, value, source="generation_sealed", captured_at_ms=captured_at_ms).model_dump()
              for key, value in context.items() if key in RULES and isinstance(value, Mapping)]
    gate = context.get("_conversation_runtime_generation_context") or {}
    budget = {key: value if type(value := gate.get(key)) is int and value >= 0 else None
              for key in ("token_estimate", "reply_budget_tokens")}
    return {"frames": frames, "budget": budget}
