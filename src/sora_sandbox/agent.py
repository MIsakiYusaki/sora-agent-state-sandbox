from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .contracts import TurnContext
from .llm import LLMClient
from .prompt_loader import render_system_prompt, render_user_prompt
from .state import append_recent_turn, find_world_event, now_ms, persist_response_candidates
from .subsystems import FunctionSubsystemAdapter, SubsystemRegistry


JsonDict = dict[str, Any]


_PAREN_BLOCK_RE = re.compile(r"[（(].*?[)）]")
_PAREN_ASIDE_HINTS = ("笑", "叹", "眨", "思考", "小声", "轻轻", "动作", "表情", "旁白", "内心", "OS", "os")


def _timer() -> float:
    return time.perf_counter() * 1000


def _elapsed_since(start_ms: float) -> float:
    return round(_timer() - start_ms, 2)


class ArbitrationJudger(Protocol):
    def score(self, context: JsonDict, signals: list[JsonDict]) -> JsonDict:
        ...


class TurnInterpreter(Protocol):
    def interpret(self, context: JsonDict) -> JsonDict:
        ...


@dataclass
class LLMTurnInterpreter:
    llm: LLMClient

    def interpret(self, context: JsonDict) -> JsonDict:
        if context.get("proactive_trigger"):
            return rule_turn_interpretation(
                context.get("user_input", ""),
                context.get("recent_turns", []),
                proactive_trigger=context.get("proactive_trigger"),
            )
        system = (
            "You are Project Sora's turn interpreter. "
            "Read the current user input with recent conversation context. "
            "Resolve implicit references, infer conversation move, intent, domain, risk, "
            "and whether world/persona framing is literal or stylistic. "
            "Return strict JSON only; do not answer the user."
        )
        user = json.dumps(
            {
                "task": "interpret_turn",
                "schema": turn_interpretation_schema(),
                "context": context,
            },
            ensure_ascii=False,
            indent=2,
        )
        result = self.llm.chat(system=system, user=user, temperature=0.0, max_tokens=900)
        content = result.get("content", "")
        try:
            payload = extract_json_object(content)
        except ValueError as exc:
            fallback = rule_turn_interpretation(
                context.get("user_input", ""),
                context.get("recent_turns", []),
                proactive_trigger=context.get("proactive_trigger"),
            )
            fallback["mode"] = "llm_degraded"
            fallback["degraded_reason"] = f"invalid_json: {exc}"
            fallback["raw"] = content[:500]
            return fallback
        return normalize_turn_interpretation(payload)


@dataclass
class LLMArbitrationJudger:
    llm: LLMClient

    def score(self, context: JsonDict, signals: list[JsonDict]) -> JsonDict:
        if not signals:
            return {"mode": "skipped", "scores": {}, "reason": "no_gray_signals"}
        system = (
            "You are Project Sora's semantic arbitration judge. "
            "Hard policy has already removed forbidden actions. "
            "Score only whether candidate signals are semantically natural, relevant, "
            "and low-risk for this turn. Return strict JSON only."
        )
        user = json.dumps(
            {
                "task": "score_candidate_signals",
                "output_schema": {
                    "signal_scores": [
                        {
                            "signal_id": "string",
                            "semantic_relevance": "0..1",
                            "naturalness": "0..1",
                            "interruption_risk": "0..1",
                            "boundary_risk": "0..1",
                            "recommended_visibility": "active|background|suppress",
                            "reason": "short string",
                        }
                    ],
                    "overall": {
                        "should_expand_topic": "boolean",
                        "max_length": "short|medium",
                        "question_budget": "integer",
                    },
                },
                "context": context,
                "candidate_signals": signals,
            },
            ensure_ascii=False,
            indent=2,
        )
        result = self.llm.chat(system=system, user=user, temperature=0.0, max_tokens=700)
        content = result.get("content", "")
        try:
            payload = extract_json_object(content)
        except ValueError as exc:
            return {
                "mode": "llm_degraded",
                "scores": {},
                "reason": f"invalid_json: {exc}",
                "raw": content[:500],
            }
        return normalize_arbitration_judgment(payload)


@dataclass
class AgentRunner:
    llm: LLMClient
    subsystem_registry: SubsystemRegistry | None = None
    memory_runtime: Any | None = None
    arbitration_judger: ArbitrationJudger | None = None
    turn_interpreter: TurnInterpreter | None = None

    def run(self, state: JsonDict, user_input: str) -> JsonDict:
        return self._run(state, user_input=user_input, proactive_trigger=None)

    def run_proactive(self, state: JsonDict, trigger: JsonDict) -> JsonDict:
        return self._run(state, user_input="", proactive_trigger=trigger)

    def _run(
        self,
        state: JsonDict,
        *,
        user_input: str,
        proactive_trigger: JsonDict | None,
    ) -> JsonDict:
        start = _timer()
        trace: list[JsonDict] = []
        timing: dict[str, float] = {}

        def step(name: str, payload: JsonDict | None = None) -> None:
            trace.append({"seq": len(trace) + 1, "kind": name, "at": now_ms(), "payload": payload or {}})

        state["session"]["turn_id"] += 1
        state["session"]["context_version"] += 1
        if proactive_trigger:
            step("proactive.trigger.received", proactive_trigger)
        else:
            step("user.input.received", {"input_type": "text"})

        context_pack = build_context_pack(
            state,
            user_input,
            step,
            timing,
            subsystem_registry=self.subsystem_registry,
            proactive_trigger=proactive_trigger,
            memory_runtime=self.memory_runtime,
            arbitration_judger=self.arbitration_judger or LLMArbitrationJudger(self.llm),
            turn_interpreter=self.turn_interpreter or LLMTurnInterpreter(self.llm),
        )
        mark_behavior_observed(state)

        if proactive_trigger and context_pack["arbitration_result"]["primary_mode"] == "proactive_silent":
            return finalize_silent_proactive_result(
                state,
                context_pack,
                trace,
                timing,
                proactive_trigger,
                start,
            )

        t0 = _timer()
        response_plan = self._plan_response(context_pack, user_input, step)
        timing["response_planner"] = _elapsed_since(t0)

        t0 = _timer()
        expression_event = route_expression(response_plan, context_pack, step)
        timing["expression_router"] = _elapsed_since(t0)

        t0 = _timer()
        reply = response_plan["output_text"]
        if not proactive_trigger:
            append_recent_turn(state, "user", user_input)
        append_recent_turn(state, "sora", reply)
        update_session(state, user_input or "[proactive idle turn]", reply)
        persist_response_candidates(state, response_plan)
        memory_updates: list[JsonDict] = []
        if self.memory_runtime is not None:
            if not proactive_trigger:
                explicit_update = self.memory_runtime.observe_turn(user_input, reply)
                if explicit_update:
                    memory_updates.append(explicit_update)
            hook_update = self.memory_runtime.observe_response_plan(response_plan)
            if hook_update:
                memory_updates.append(hook_update)
            self.memory_runtime.sync_state_cache(state)
            if memory_updates:
                step(
                    "memory.candidates.observed",
                    {
                        "count": len(memory_updates),
                        "statuses": [item.get("status") for item in memory_updates],
                    },
                )
        if proactive_trigger:
            record_proactive_attempt(
                state,
                proactive_trigger,
                emitted=True,
                reply=reply,
                topic_id=selected_proactive_topic_id(context_pack),
            )
        timing["state_commit"] = _elapsed_since(t0)

        state_diff = build_state_diff(context_pack, response_plan, expression_event)
        timing["_total"] = _elapsed_since(start)

        result = {
            "user_input": user_input,
            "turn_kind": "proactive" if proactive_trigger else "reactive",
            "proactive_trigger": proactive_trigger,
            "emitted": True,
            "reply": reply,
            "context_pack": context_pack,
            "context_pack_summary": summarize_context_pack(context_pack),
            "response_plan": response_plan,
            "expression_event": expression_event,
            "state_diff": state_diff,
            "trace": trace,
            "timing": timing,
            "token_usage": response_plan.get("token_usage", {}),
            "memory_updates": memory_updates,
        }

        state["observability"]["last_trace"] = trace
        state["observability"]["last_state_diff"] = state_diff
        state["observability"]["last_response_plan"] = response_plan
        state["observability"]["last_context_pack"] = context_pack
        state["observability"]["last_run_result"] = result
        return result

    def _plan_response(self, context_pack: JsonDict, user_input: str, step) -> JsonDict:
        system = build_system_prompt(context_pack)
        user = build_user_prompt(context_pack, user_input)
        llm_result = self.llm.chat(system=system, user=user, temperature=0.7, max_tokens=800)
        step("llm.response.received", {"model": llm_result.get("model"), "usage": llm_result.get("usage", {})})

        output_text = clean_response(llm_result["content"].strip())
        output_text = enforce_turn_frame(output_text, context_pack["turn_frame"])
        output_text = enforce_question_budget(output_text, context_pack["dialogue_control_frame"])
        usage = normalize_token_usage(llm_result.get("usage", {}))
        growth_candidate = maybe_growth_candidate(context_pack, user_input, output_text)
        memory_hook_candidate = maybe_memory_hook_candidate(context_pack, user_input, output_text)
        return {
            "intent": (
                "proactive_companion_opening"
                if context_pack["turn_frame"]["speech_act"] == "proactive_opening"
                else infer_intent(user_input)
            ),
            "turn_frame": context_pack["turn_frame"],
            "context_use_policy": context_pack["context_use_policy"],
            "event_bus": context_pack["event_bus"],
            "arbitration_result": context_pack["arbitration_result"],
            "output_text": output_text,
            "used_signal_ids": context_pack["arbitration_result"]["selected_signals"],
            "background_signal_ids": context_pack["arbitration_result"]["background_signals"],
            "used_memory_ids": context_pack["recollection_frame"]["retrieved_ids"],
            "persona_influences": context_pack["persona_frame"]["influence_summary"],
            "worldops_influences": context_pack["worldops_frame"]["influence_summary"],
            "expression_hint": context_pack["arbitration_result"]["response_directives"].get("expression_hint") or context_pack["worldops_frame"].get("expression_hint") or "text",
            "growth_event_candidate": growth_candidate,
            "memory_hook_candidate": memory_hook_candidate,
            "session_update": {
                "active_topic": "Sora Agent State Sandbox 工程验证",
                "last_user_input": user_input,
            },
            "audit_reason": "Agent Runtime 只生成回应计划和候选事件，不直接写长期记忆或 Persona。",
            "token_usage": {"response_planner": usage},
            "model": llm_result.get("model"),
        }


def build_context_pack(
    state: JsonDict,
    user_input: str,
    step,
    timing: dict[str, float],
    subsystem_registry: SubsystemRegistry | None = None,
    proactive_trigger: JsonDict | None = None,
    memory_runtime: Any | None = None,
    arbitration_judger: ArbitrationJudger | None = None,
    turn_interpreter: TurnInterpreter | None = None,
) -> JsonDict:
    context_start = _timer()
    behavior_frame = dict(state["behavior"]["current"])
    behavior_frame["changed"] = bool(behavior_frame.get("changed", True))
    step("behavior.frame.provided", {"scene_type": behavior_frame["scene_type"], "changed": behavior_frame["changed"]})

    t0 = _timer()
    turn_interpretation = interpret_turn(
        state,
        user_input,
        proactive_trigger=proactive_trigger,
        interpreter=turn_interpreter,
    )
    timing["turn_interpretation"] = _elapsed_since(t0)
    step(
        "turn.interpretation.provided",
        {
            "mode": turn_interpretation.get("mode"),
            "move": turn_interpretation.get("conversation_move"),
            "intent": turn_interpretation.get("intent", {}).get("primary"),
            "references_previous_context": turn_interpretation.get("context_reference", {}).get("references_previous_context"),
        },
    )

    turn_frame = build_turn_frame(
        state,
        user_input,
        proactive_trigger=proactive_trigger,
        interpretation=turn_interpretation,
    )
    step("turn.frame.built", {"speech_act": turn_frame["speech_act"], "context_relevance": turn_frame["context_relevance"]})

    context_use_policy = build_context_use_policy(turn_frame)
    step("context.use_policy.built", {"allowed": context_use_policy["allowed_context_sources"], "blocked": context_use_policy["blocked_context_sources"]})

    session_context = build_session_context(state, behavior_frame, context_use_policy)
    step("session.context.updated", {"turn_id": session_context["turn_id"]})

    origin_frame = build_origin_frame(state)
    step("origin.frame.provided", {"template_id": origin_frame.get("template_id"), "name": origin_frame.get("name")})

    if memory_runtime is None:
        recollection_frame = retrieve_memories(state, user_input)
    else:
        recollection_frame = memory_runtime.recall(
            user_input,
            turn_frame=turn_frame,
            context_use_policy=context_use_policy,
        )
        timing["memory_recall"] = recollection_frame.get("latency_ms", 0.0)
    step("memory.recollection.provided", {"ids": recollection_frame["retrieved_ids"], "surface_action": recollection_frame["surface_action"]})

    persona_frame = build_persona_frame(state)
    step("persona.frame.provided", {"debug_overrides": len(state["persona"].get("debug_overrides", []))})

    worldops_frame = build_worldops_frame(state, behavior_frame, persona_frame)
    step("worldops.frame.provided", {"active_event_id": worldops_frame.get("selected_event_id"), "level": worldops_frame.get("expression_level")})

    dialogue_control_frame = build_dialogue_control_frame(state, user_input)
    if proactive_trigger:
        dialogue_control_frame.update({
            "input_mode": "proactive_idle",
            "should_ask_question": False,
            "question_budget": 0,
            "preferred_closing": "brief_statement",
        })
    else:
        apply_turn_interpretation_dialogue_control(dialogue_control_frame, turn_interpretation, turn_frame)
    timing["context_frames"] = _elapsed_since(context_start)

    t0 = _timer()
    event_bus = build_event_bus(
        state,
        user_input,
        behavior_frame,
        turn_frame,
        proactive_trigger=proactive_trigger,
    )
    timing["event_bus"] = _elapsed_since(t0)

    t0 = _timer()
    base_signals, subsystem_health = collect_subsystem_signals(
        state,
        event_bus,
        session_context,
        behavior_frame,
        recollection_frame,
        persona_frame,
        worldops_frame,
        origin_frame,
        turn_frame,
        context_use_policy,
        dialogue_control_frame=dialogue_control_frame,
        timing=timing,
        registry=subsystem_registry,
    )
    timing["subsystem_signals"] = _elapsed_since(t0)

    t0 = _timer()
    initiative_window = build_initiative_window(state, event_bus, behavior_frame, turn_frame, base_signals)
    timing["initiative_window"] = _elapsed_since(t0)

    t0 = _timer()
    initiative_signals = build_initiative_signals(state, event_bus, initiative_window, base_signals, worldops_frame)
    timing["sig_initiative"] = _elapsed_since(t0)
    timing["subsystem_signals"] = round(timing["subsystem_signals"] + timing["sig_initiative"], 2)

    signals = base_signals + initiative_signals
    t0 = _timer()
    arbitration_result = arbitrate_signals(
        signals,
        initiative_window,
        turn_frame,
        behavior_frame,
        semantic_judger=arbitration_judger,
    )
    timing["arbitration"] = _elapsed_since(t0)
    step(
        "subsystems.signals.collected",
        {
            "count": len(signals),
            "sources": sorted({signal["source"] for signal in signals}),
            "health": subsystem_health,
        },
    )
    step("arbiter.result.provided", {"primary_mode": arbitration_result["primary_mode"], "selected": len(arbitration_result["selected_signals"])})

    return {
        "session_context": session_context,
        "behavior_frame": behavior_frame,
        "recollection_frame": recollection_frame,
        "persona_frame": persona_frame,
        "worldops_frame": worldops_frame,
        "origin_frame": origin_frame,
        "turn_frame": turn_frame,
        "turn_interpretation": turn_interpretation,
        "context_use_policy": context_use_policy,
        "dialogue_control_frame": dialogue_control_frame,
        "event_bus": event_bus,
        "signals": signals,
        "subsystem_health": subsystem_health,
        "initiative_window": initiative_window,
        "arbitration_result": arbitration_result,
        "policy_frame": {
            "external_action_allowed": False,
            "memory_write_mode": "candidate_only",
            "persona_update_mode": "candidate_only_or_manual_apply",
            "subsystem_runtime": "pluggable_parallel_v0.6",
            "state_write_contract": "candidate_then_commit",
        },
    }


def build_session_context(state: JsonDict, behavior_frame: JsonDict, context_use_policy: JsonDict) -> JsonDict:
    session = state["session"]
    all_recent_turns = session.get("recent_turns", [])[-40:]
    mode = context_use_policy["recent_turns_mode"]
    if mode == "full":
        recent_turns = all_recent_turns
    elif mode == "proactive_tail":
        recent_turns = all_recent_turns[-20:]
    elif mode == "greeting_tail":
        recent_turns = all_recent_turns[-4:]
    else:
        recent_turns = []
    return {
        "session_id": session["session_id"],
        "turn_id": session["turn_id"],
        "context_version": session["context_version"],
        "recent_turns": recent_turns,
        "history_items": len(recent_turns),
        "history_rounds": len(recent_turns) // 2,
        "recent_turns_available": len(all_recent_turns),
        "recent_turns_mode": context_use_policy["recent_turns_mode"],
        "recent_turns_omitted_reason": context_use_policy.get("recent_turns_omitted_reason"),
        "rolling_summary": session.get("rolling_summary", ""),
        "dialogue_state": session["dialogue_state"],
        "scene_snapshot": behavior_frame,
    }


def build_origin_frame(state: JsonDict) -> JsonDict:
    origin = state.get("origin", {})
    if not origin:
        return {
            "template_id": None,
            "name": "未选择",
            "summary": "",
            "speech_habits": [],
            "initiative_bias": {},
            "influence_summary": ["当前没有 Origin Template。"],
        }
    return {
        "template_id": origin.get("template_id"),
        "name": origin.get("name"),
        "summary": origin.get("summary"),
        "worldview_anchor": origin.get("worldview_anchor"),
        "speech_habits": origin.get("speech_habits", []),
        "trait_offsets": origin.get("trait_offsets", {}),
        "initiative_bias": origin.get("initiative_bias", {}),
        "active_event_id": origin.get("active_event_id"),
        "selection_mode": origin.get("selection_mode"),
        "influence_summary": [
            f"Origin={origin.get('name')}: {origin.get('summary')}",
            "表达习惯：" + "；".join(origin.get("speech_habits", [])),
        ],
    }


def interpret_turn(
    state: JsonDict,
    user_input: str,
    *,
    proactive_trigger: JsonDict | None,
    interpreter: TurnInterpreter | None,
) -> JsonDict:
    recent_turns = state.get("session", {}).get("recent_turns", [])[-8:]
    context = {
        "user_input": user_input,
        "recent_turns": recent_turns,
        "session_dialogue_state": state.get("session", {}).get("dialogue_state", {}),
        "persona_core": state.get("persona", {}).get("persona_core", {}),
        "origin": state.get("origin", {}),
        "behavior": state.get("behavior", {}).get("current", {}),
        "proactive_trigger": proactive_trigger,
    }
    if interpreter is None:
        interpretation = rule_turn_interpretation(user_input, recent_turns, proactive_trigger=proactive_trigger)
        return repair_turn_interpretation(interpretation, user_input, recent_turns, state.get("worldops", {}))
    try:
        interpretation = interpreter.interpret(context)
        return repair_turn_interpretation(interpretation, user_input, recent_turns, state.get("worldops", {}))
    except Exception as exc:
        fallback = rule_turn_interpretation(user_input, recent_turns, proactive_trigger=proactive_trigger)
        fallback["mode"] = "interpreter_degraded"
        fallback["degraded_reason"] = f"{type(exc).__name__}: {exc}"
        return repair_turn_interpretation(fallback, user_input, recent_turns, state.get("worldops", {}))


def repair_turn_interpretation(interpretation: JsonDict, user_input: str, recent_turns: list[JsonDict], worldops: JsonDict | None = None) -> JsonDict:
    repaired = normalize_turn_interpretation(interpretation)
    repairs: list[str] = []
    move = repaired.get("conversation_move")
    reference = repaired.get("context_reference", {})
    targets = reference.get("resolved_targets", [])
    needs_context = move in {"continue_previous_topic", "repair_or_correction", "clarification_request"} or has_deictic_reference(user_input)

    if needs_context and recent_turns and (not reference.get("references_previous_context") or not targets):
        inferred = infer_recent_reference_target(user_input, recent_turns)
        if inferred:
            reference["references_previous_context"] = True
            reference["reference_type"] = "implicit_deictic" if has_deictic_reference(user_input) else "topic_resume"
            reference["resolved_targets"] = [inferred]
            reference["unresolved_references"] = []
            repairs.append("filled_resolved_target_from_recent_turns")

            topic = repaired.setdefault("topic", {})
            if not topic.get("current_topic"):
                topic["current_topic"] = inferred["summary"]
            if not topic.get("user_visible_topic_label"):
                topic["user_visible_topic_label"] = inferred["summary"][:120]
            topic["topic_shift"] = "none"
            topic["topic_continuity"] = "strong"
            if inferred.get("source_turn") == "last_sora_turn":
                domain = repaired.setdefault("domain", {})
                if domain.get("primary") == "general":
                    domain["primary"] = "world_lore"
                    repairs.append("marked_world_lore_domain_from_sora_context")
                framing = repaired.setdefault("framing", {})
                if framing.get("frame_role") == "none":
                    framing["requested_frame"] = "sora_world"
                    framing["frame_role"] = "literal_topic"
                    framing["should_answer_in_frame"] = True
                    framing["frame_must_not_override_reality"] = True
                    repairs.append("marked_sora_world_literal_topic_from_sora_context")

    active_event_target = infer_active_worldops_event_target(user_input, worldops or {})
    if active_event_target:
        reference["references_previous_context"] = True
        reference["reference_type"] = "explicit"
        existing_targets = reference.get("resolved_targets", [])
        if not any(target.get("event_id") == active_event_target.get("event_id") for target in existing_targets):
            reference["resolved_targets"] = [active_event_target] + existing_targets[:4]
            repairs.append("linked_active_worldops_event")
        reference["unresolved_references"] = []

        topic = repaired.setdefault("topic", {})
        topic["current_topic"] = active_event_target["summary"]
        topic["user_visible_topic_label"] = active_event_target["summary"][:120]
        topic["topic_shift"] = "none"
        topic["topic_continuity"] = "strong"

        domain = repaired.setdefault("domain", {})
        if domain.get("primary") == "general":
            domain["primary"] = "world_lore"
            repairs.append("marked_world_lore_domain_from_active_event")
        framing = repaired.setdefault("framing", {})
        if framing.get("frame_role") == "none":
            framing["requested_frame"] = "sora_world"
            framing["frame_role"] = "literal_topic"
            framing["should_answer_in_frame"] = True
            framing["frame_must_not_override_reality"] = True
            repairs.append("marked_sora_world_literal_topic_from_active_event")

    if reference.get("references_previous_context") and not reference.get("resolved_targets"):
        reference["unresolved_references"] = list(dict.fromkeys(reference.get("unresolved_references", []) + ["previous_context_target"]))
        repairs.append("marked_unresolved_context_reference")

    if is_sora_world_question(user_input):
        framing = repaired.setdefault("framing", {})
        if framing.get("frame_role") == "none":
            framing["requested_frame"] = "sora_world"
            framing["frame_role"] = "literal_topic"
            framing["should_answer_in_frame"] = True
            framing["frame_must_not_override_reality"] = True
            repairs.append("marked_sora_world_literal_topic")
        domain = repaired.setdefault("domain", {})
        if domain.get("primary") == "general":
            domain["primary"] = "world_lore"
            repairs.append("marked_world_lore_domain")

    repaired["consistency"] = {
        "status": "repaired" if repairs else "ok",
        "repairs": repairs,
    }
    return repaired


def infer_active_worldops_event_target(user_input: str, worldops: JsonDict) -> JsonDict | None:
    event = find_world_event(worldops, worldops.get("active_event_id"))
    if not event or not text_mentions_worldops_event(user_input, event):
        return None
    return {
        "target_type": "active_worldops_event",
        "source_turn": "worldops_active_event",
        "event_id": event.get("event_id"),
        "summary": event.get("title") or event.get("one_sentence") or event.get("event_id", ""),
        "confidence": 0.9,
    }


def text_mentions_worldops_event(text: str, event: JsonDict) -> bool:
    lowered = text.lower()
    return any(alias and alias.lower() in lowered for alias in worldops_event_aliases(event))


def worldops_event_aliases(event: JsonDict) -> list[str]:
    aliases: set[str] = set()
    for field in ["event_id", "title", "one_sentence"]:
        value = str(event.get(field, "") or "").strip()
        if value:
            aliases.add(value)
    for field in ["related_factions", "affected_personas"]:
        for item in event.get(field, []) or []:
            if item:
                aliases.add(str(item).strip())

    for suffix in ["前夜", "后续", "事件"]:
        title = str(event.get("title", "") or "")
        if title.endswith(suffix) and len(title) > len(suffix) + 1:
            aliases.add(title[: -len(suffix)])

    source_text = " ".join(
        str(event.get(field, "") or "")
        for field in ["title", "one_sentence"]
    )
    for chunk in re.findall(r"[\u4e00-\u9fffA-Za-z0-9_]{2,}", source_text):
        max_len = min(6, len(chunk))
        for size in range(2, max_len + 1):
            for idx in range(0, len(chunk) - size + 1):
                aliases.add(chunk[idx : idx + size])

    stopwords = {
        "Sora", "sora", "开始", "准备", "想起", "自己", "第一次", "这里",
        "一个", "一小", "小段", "表现", "平时", "更安", "安静",
        "星港", "同盟", "白塔", "档案", "第七", "观测", "回声", "集市",
    }
    return sorted(
        alias
        for alias in aliases
        if alias and alias not in stopwords and not alias.isdigit()
    )


def has_deictic_reference(text: str) -> bool:
    return any(term in text for term in [
        "这", "这个", "这样", "这种", "这些",
        "那", "那个", "那里", "那边", "那种", "那些",
        "之类", "刚才", "上面", "前面", "继续",
        "it", "that", "this", "those", "there",
    ])


def is_sora_world_question(text: str) -> bool:
    return any(term in text for term in [
        "你们那边", "你那边", "你们那个世界", "你的世界",
        "星港", "灯塔", "信标", "夜航", "你那里",
    ])


def infer_recent_reference_target(user_input: str, recent_turns: list[JsonDict]) -> JsonDict | None:
    last_user = next((turn for turn in reversed(recent_turns) if turn.get("role") == "user"), None)
    last_sora = next((turn for turn in reversed(recent_turns) if turn.get("role") == "sora"), None)
    advice_like = any(term in user_input for term in ["怎么办", "怎么做", "办法", "建议", "如何"])
    source = last_user if advice_like and last_user else (last_sora or last_user)
    if not source:
        return None
    role = source.get("role", "turn")
    return {
        "target_type": "previous_user_problem" if role == "user" and advice_like else "previous_conversation_topic",
        "source_turn": "last_user_turn" if role == "user" else "last_sora_turn",
        "summary": str(source.get("content", ""))[:240],
        "confidence": 0.68,
    }


def build_turn_frame(
    state: JsonDict,
    user_input: str,
    proactive_trigger: JsonDict | None = None,
    interpretation: JsonDict | None = None,
) -> JsonDict:
    if proactive_trigger:
        return {
            "input_mode": "proactive_idle",
            "speech_act": "proactive_opening",
            "context_relevance": "medium",
            "current_turn_priority": "initiative_trigger",
            "blocked_inferences": ["user_intent", "relationship_claim", "urgent_interruption"],
            "reply_shape": {
                "max_sentences": 3,
                "can_ask_question": False,
                "can_reference_recent_topic": True,
                "can_reference_worldops": True,
                "style": "brief_low_interruption_opening",
            },
            "decision_reason": "用户持续空闲，系统发起主动回合；只允许短、可忽略、无需用户回应的话题入口。",
            "proactive_trigger": proactive_trigger,
            "interpretation": interpretation or {},
        }
    input_mode = classify_input_mode(user_input)
    interpretation = interpretation or rule_turn_interpretation(user_input, state["session"].get("recent_turns", []))
    if should_use_interpreted_followup(interpretation):
        domain = interpretation.get("domain", {})
        context_reference = interpretation.get("context_reference", {})
        resolved = context_reference.get("resolved_targets", [])
        has_resolved_context = bool(resolved)
        style = "resolved_context_reply" if has_resolved_context else "contextual_followup_unresolved"
        return {
            "input_mode": input_mode,
            "speech_act": interpreted_speech_act(interpretation),
            "context_relevance": "high" if context_reference.get("references_previous_context") else "medium",
            "current_turn_priority": "current_input_with_resolved_context" if has_resolved_context else "current_input_with_unresolved_context",
            "blocked_inferences": interpreted_blocked_inferences(interpretation),
            "reply_shape": {
                "max_sentences": 7 if domain.get("risk_level") in {"medium", "high"} else 6,
                "can_ask_question": bool(interpretation.get("response_requirements", {}).get("needs_clarification", False)),
                "can_reference_recent_topic": True,
                "can_reference_worldops": interpretation.get("framing", {}).get("frame_role") in {"literal_topic", "metaphor_or_style", "roleplay"},
                "style": style,
            },
            "decision_reason": (
                "LLM Turn Interpreter 判断本轮输入依赖上文；"
                f"resolved_targets={len(resolved)}，domain={domain.get('primary')}，risk={domain.get('risk_level')}。"
            ),
            "interpretation": interpretation,
        }
    if input_mode == "greeting":
        return {
            "input_mode": input_mode,
            "speech_act": "greeting_ack",
            "context_relevance": "low_commitment",
            "current_turn_priority": "current_input_with_light_continuity",
            "blocked_inferences": ["user_preference", "topic_choice", "relationship_claim", "topic_expansion", "explicit_memory_disclosure"],
            "reply_shape": {
                "max_sentences": 2,
                "can_ask_question": False,
                "can_reference_recent_topic": True,
                "can_reference_worldops": False,
                "style": "short_warm_ack_with_optional_continuity",
            },
            "decision_reason": "用户本轮只是轻量寒暄；可以轻轻承接最近上下文的存在感，但不要推进旧话题、显式召回长期记忆或要求用户回应。",
            "interpretation": interpretation,
        }

    if any(term in user_input for term in ["和我聊", "讲讲", "新鲜事", "你的生活", "你那边", "你最近"]):
        return {
            "input_mode": input_mode,
            "speech_act": "sora_life_share",
            "context_relevance": "medium",
            "current_turn_priority": "current_input_with_recent_topic",
            "blocked_inferences": ["relationship_claim", "user_preference_overclaim"],
            "reply_shape": {
                "max_sentences": 6,
                "can_ask_question": False,
                "can_reference_recent_topic": True,
                "can_reference_worldops": True,
                "style": "specific_observation",
            },
            "decision_reason": "用户主动询问 Sora 的生活或近况，可以使用近期话题和 WorldOps 细节，但不要反问续命。",
            "interpretation": interpretation,
        }

    return {
        "input_mode": input_mode,
        "speech_act": "general_reply",
        "context_relevance": "medium",
        "current_turn_priority": "current_input_first",
        "blocked_inferences": ["relationship_claim"],
        "reply_shape": {
            "max_sentences": 6,
            "can_ask_question": True,
            "can_reference_recent_topic": True,
            "can_reference_worldops": True,
            "style": "natural_companion_reply",
        },
        "decision_reason": "普通陪伴输入；可以参考短期上下文，但必须以本轮输入为最高优先级。",
        "interpretation": interpretation,
    }


def should_use_interpreted_followup(interpretation: JsonDict) -> bool:
    if interpretation.get("mode") in {"llm_degraded", "interpreter_degraded"} and interpretation.get("confidence", 0) < 0.6:
        return False
    move = interpretation.get("conversation_move")
    references = interpretation.get("context_reference", {}).get("references_previous_context", False)
    intent = interpretation.get("intent", {}).get("primary")
    return bool(
        references
        or move in {"continue_previous_topic", "repair_or_correction", "clarification_request"}
        or intent in {"correct_assumption", "answer_to_assistant_question"}
    )


def interpreted_speech_act(interpretation: JsonDict) -> str:
    domain = interpretation.get("domain", {})
    intent = interpretation.get("intent", {}).get("primary")
    move = interpretation.get("conversation_move")
    if move == "repair_or_correction":
        return "topic_repair"
    if domain.get("primary") == "health" and intent in {"ask_for_advice", "ask_for_information"}:
        return "health_advice_followup"
    if intent == "ask_for_advice":
        return "advice_followup"
    if intent == "ask_for_explanation":
        return "explanation_followup"
    return "contextual_followup"


def interpreted_blocked_inferences(interpretation: JsonDict) -> list[str]:
    blocked = ["relationship_claim"]
    domain = interpretation.get("domain", {})
    framing = interpretation.get("framing", {})
    if domain.get("risk_level") in {"medium", "high"}:
        blocked.extend(["overconfident_advice", "diagnosis_or_professional_claim"])
    if framing.get("frame_role") in {"metaphor_or_style", "roleplay"}:
        blocked.append("world_framing_overrides_reality")
    for item in interpretation.get("response_requirements", {}).get("should_avoid", []):
        key = str(item).strip().replace(" ", "_")
        if key:
            blocked.append(key)
    return list(dict.fromkeys(blocked))


def build_context_use_policy(turn_frame: JsonDict) -> JsonDict:
    allowed = ["persona_tone"]
    blocked = []
    recent_turns_mode = "full"
    omitted_reason = None
    interpretation = turn_frame.get("interpretation", {})

    if turn_frame["speech_act"] == "proactive_opening":
        recent_turns_mode = "proactive_tail"
        omitted_reason = "主动搭话只注入最近 10 轮，兼顾关系连续性与上下文成本。"
        allowed.extend(["recent_turns_tail", "worldops_event"])
        blocked.extend(["full_recent_turns", "memory_recollection"])
    elif interpretation.get("context_reference", {}).get("references_previous_context"):
        recent_turns_mode = "full"
        omitted_reason = "Turn Interpreter 判断本轮依赖上文，完整注入短期上下文用于指代消解。"
        allowed.extend(["recent_turns", "resolved_previous_context", "memory_recollection"])
        if interpretation.get("framing", {}).get("frame_role") in {"literal_topic", "metaphor_or_style", "roleplay"}:
            allowed.append("worldops_event")
        else:
            blocked.append("worldops_event")
    elif turn_frame["speech_act"] == "greeting_ack":
        recent_turns_mode = "greeting_tail"
        omitted_reason = "轻量寒暄只注入最近 2 轮用于连续感；禁止借此推进旧话题或展开新信息。"
        allowed.extend(["recent_turns_light_continuity"])
        blocked.extend(["memory_recollection", "worldops_event", "topic_expansion"])
    elif turn_frame["context_relevance"] == "low":
        recent_turns_mode = "metadata_only"
        omitted_reason = "当前输入为低上下文输入，recent_turns 只用于统计，不进入生成上下文。"
        blocked.extend(["recent_turns_content", "memory_recollection", "worldops_event"])
    else:
        allowed.extend(["recent_turns", "memory_recollection", "worldops_event"])

    return {
        "allowed_context_sources": allowed,
        "blocked_context_sources": blocked,
        "recent_turns_mode": recent_turns_mode,
        "recent_turns_omitted_reason": omitted_reason,
        "inference_policy": {
            "blocked_inferences": turn_frame["blocked_inferences"],
            "must_not_say": ["看来你更喜欢", "你是不是想", "你应该是想", "你看来想要"],
        },
    }


def build_dialogue_control_frame(state: JsonDict, user_input: str) -> JsonDict:
    recent_turns = state["session"].get("recent_turns", [])
    sora_question_streak = count_recent_sora_question_streak(recent_turns)
    input_mode = classify_input_mode(user_input)
    user_explicitly_invites_question = any(term in user_input for term in ["你问", "问我", "你想知道", "你有什么想问"])
    user_needs_clarification = any(term in user_input for term in ["怎么", "如何", "为什么", "哪一个", "帮我判断", "不确定"])
    user_invites_sora_story = any(term in user_input for term in ["你的生活", "和我聊", "讲讲", "新鲜事", "你那边", "你最近"])
    should_ask_question = user_explicitly_invites_question or (user_needs_clarification and sora_question_streak == 0)
    if input_mode == "greeting":
        should_ask_question = False
    if user_invites_sora_story and not user_explicitly_invites_question:
        should_ask_question = False
    if sora_question_streak >= 2:
        should_ask_question = False

    return {
        "input_mode": input_mode,
        "recent_sora_question_streak": sora_question_streak,
        "should_ask_question": should_ask_question,
        "question_budget": 1 if should_ask_question else 0,
        "preferred_closing": preferred_closing(input_mode, should_ask_question),
        "anti_patterns": [
            "不要每轮都用问句结尾。",
            "不要机械使用“你呢”“你觉得呢”“要不要继续聊”。",
            "用户关心 Sora 生活时，优先多讲自己的观察和感受，而不是立刻把话题抛回用户。",
            "用户只是打招呼时，不要从历史推断“你更喜欢/你看来想要/你是不是”。",
        ],
    }


def apply_turn_interpretation_dialogue_control(
    dialogue_control_frame: JsonDict,
    interpretation: JsonDict,
    turn_frame: JsonDict,
) -> None:
    requirements = interpretation.get("response_requirements", {})
    if not should_use_interpreted_followup(interpretation):
        return

    question_budget = int(requirements.get("question_budget", dialogue_control_frame.get("question_budget", 0)) or 0)
    needs_clarification = bool(requirements.get("needs_clarification", False))
    if not needs_clarification:
        question_budget = 0
    question_budget = max(0, min(question_budget, 1))

    dialogue_control_frame.update({
        "interpreted_intent": interpretation.get("intent", {}).get("primary"),
        "conversation_move": interpretation.get("conversation_move"),
        "should_ask_question": bool(question_budget),
        "question_budget": question_budget,
        "preferred_closing": "brief_statement" if question_budget == 0 else dialogue_control_frame.get("preferred_closing", "statement_or_light_offer"),
    })
    turn_frame["reply_shape"]["can_ask_question"] = bool(question_budget)


def retrieve_memories(state: JsonDict, user_input: str) -> JsonDict:
    words = set(_keywords(user_input))
    scored: list[tuple[int, JsonDict]] = []
    for item in state["memory"]["items"]:
        if item.get("status") != "active":
            continue
        haystack = " ".join([item.get("content", ""), " ".join(item.get("tags", []))]).lower()
        score = sum(1 for word in words if word and word.lower() in haystack)
        if score:
            scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    memories = [item for _, item in scored[:3]]
    explicit = any(term in user_input for term in ["记得", "之前", "上次", "回忆"])
    surface_action = "gist" if explicit and memories else ("implicit" if memories else "none")
    return {
        "surface_action": surface_action,
        "retrieved_ids": [item["id"] for item in memories],
        "memory_summaries": [item["content"] for item in memories],
        "evidence_level": "strong" if memories else "none",
        "decision_reason": "按关键词召回 seed/mock memory；仅返回治理后的摘要。",
        "forbidden_disclosures": ["不要把全部原始记忆逐条倾倒给用户。"],
    }


def build_persona_frame(state: JsonDict) -> JsonDict:
    persona = state["persona"]
    traits = {key: value["value"] for key, value in persona["persona_traits"].items()}
    top_interests = sorted(
        persona["interest_graph"].items(),
        key=lambda pair: pair[1].get("sora_interest", 0),
        reverse=True,
    )[:3]
    influence = [
        f"caution={traits['caution']:.2f}: 默认低打扰和边界敏感",
        f"playfulness={traits['playfulness']:.2f}: 控制吐槽强度",
        f"expressiveness={traits['expressiveness']:.2f}: 控制主动表达意愿",
    ]
    if persona.get("debug_overrides"):
        influence.append("当前含 debug_overrides，必须在 state_diff 中标记")
    return {
        "core": persona["persona_core"],
        "traits": traits,
        "short_term_state": persona["short_term_state"],
        "interest_graph_top": [{"topic": topic, **item} for topic, item in top_interests],
        "judgment_policy": persona["judgment_policy"],
        "relationship_state": persona["relationship_state"],
        "expression_policy": persona["expression_policy"],
        "debug_overrides": persona.get("debug_overrides", []),
        "influence_summary": influence,
    }


def build_worldops_frame(state: JsonDict, behavior_frame: JsonDict, persona_frame: JsonDict) -> JsonDict:
    worldops = state["worldops"]
    event = find_world_event(worldops, worldops.get("active_event_id"))
    if not event:
        return {
            "candidate_events": [],
            "selected_event_id": None,
            "expression_level": "L0",
            "expression_hint": "text",
            "interruption_risk": 0.0,
            "micro_quest": None,
            "memory_policy": {"write_only_if_user_participates": True},
            "forbidden_behaviors": ["不要凭空引入 WorldOps 事件。"],
            "influence_summary": ["当前没有激活的 WorldOps 事件。"],
        }

    caution = persona_frame["traits"].get("caution", 0.7)
    busy_scene = behavior_frame["scene_type"] in {"focus_work", "watch_companion"}
    interruption_risk = min(1.0, caution + (0.2 if busy_scene else 0.0))
    level = event.get("expression_default", "L2")
    if interruption_risk >= 0.85:
        level = "L0"
    elif interruption_risk >= 0.72:
        level = "L1"

    micro_quest = active_micro_quest(worldops, event["event_id"]) or None
    hint = "micro_quest_invite" if level == "L3" else ("bubble" if level in {"L1", "L2"} else "text")
    return {
        "candidate_events": [event],
        "selected_event_id": event["event_id"],
        "selected_event": event,
        "expression_level": level,
        "expression_hint": hint,
        "interruption_risk": round(interruption_risk, 3),
        "micro_quest": micro_quest,
        "memory_policy": event.get("memory_policy", {"write_only_if_user_participates": True}),
        "forbidden_behaviors": [
            "不要把世界事件讲成长篇设定。",
            "用户忽略或拒绝时，不制造愧疚感。",
            "未参与事件不得写入共同经历。",
        ],
        "influence_summary": [f"WorldOps {event['event_id']} at {level}: {event['one_sentence']}"],
    }


def build_event_bus(
    state: JsonDict,
    user_input: str,
    behavior_frame: JsonDict,
    turn_frame: JsonDict,
    proactive_trigger: JsonDict | None = None,
) -> JsonDict:
    events = []
    if proactive_trigger:
        events.append({
            "event_id": f"evt_idle_trigger_{state['session']['turn_id']}",
            "type": "proactive_trigger",
            "payload": proactive_trigger,
        })
    else:
        events.append(
            {
                "event_id": f"evt_user_input_{state['session']['turn_id']}",
                "type": "user_input",
                "payload": {
                    "text": user_input,
                    "speech_act": turn_frame["speech_act"],
                    "input_mode": turn_frame["input_mode"],
                },
            }
        )
    events.append(
        {
            "event_id": f"evt_behavior_{state['session']['turn_id']}",
            "type": "behavior_snapshot",
            "payload": {
                "scene_type": behavior_frame.get("scene_type"),
                "changed": behavior_frame.get("changed", False),
                "afk": behavior_frame.get("afk", False),
                "interruptibility": behavior_frame.get("interruptibility", "unknown"),
                "interruptibility_confidence": behavior_frame.get("interruptibility_confidence", 0.0),
                "user_present": behavior_frame.get("user_present", False),
                "focus_level": behavior_frame.get("focus_level", "unknown"),
            },
        }
    )
    return {
        "turn_id": state["session"]["turn_id"],
        "turn_kind": "proactive" if proactive_trigger else "reactive",
        "events": events,
        "runtime": "pluggable_parallel_v0.6",
    }


def collect_subsystem_signals(
    state: JsonDict,
    event_bus: JsonDict,
    session_context: JsonDict,
    behavior_frame: JsonDict,
    recollection_frame: JsonDict,
    persona_frame: JsonDict,
    worldops_frame: JsonDict,
    origin_frame: JsonDict,
    turn_frame: JsonDict,
    context_use_policy: JsonDict,
    dialogue_control_frame: JsonDict,
    timing: dict[str, float] | None = None,
    registry: SubsystemRegistry | None = None,
) -> tuple[list[JsonDict], list[JsonDict]]:
    context = TurnContext(
        state=state,
        event_bus=event_bus,
        session_context=session_context,
        turn_frame=turn_frame,
        context_use_policy=context_use_policy,
        frames={
            "behavior": behavior_frame,
            "recollection": recollection_frame,
            "persona": persona_frame,
            "worldops": worldops_frame,
            "origin": origin_frame,
            "dialogue_control": dialogue_control_frame,
        },
    )
    active_registry = registry or build_default_subsystem_registry()
    signals, health = active_registry.collect(context)
    if timing is not None:
        for item in health:
            timing[f"sig_{item['subsystem']}"] = item["latency_ms"]
    return signals, health


def build_default_subsystem_registry() -> SubsystemRegistry:
    def optional(signal: JsonDict | None) -> list[JsonDict]:
        return [signal] if signal else []

    return SubsystemRegistry(
        [
            FunctionSubsystemAdapter(
                "dialogue",
                lambda ctx: [dialogue_signal(ctx.event_bus, ctx.turn_frame, ctx.frame("dialogue_control"))],
            ),
            FunctionSubsystemAdapter(
                "behavior",
                lambda ctx: [behavior_signal(ctx.event_bus, ctx.frame("behavior"))],
            ),
            FunctionSubsystemAdapter(
                "memory",
                lambda ctx: optional(memory_signal(ctx.event_bus, ctx.frame("recollection"), ctx.turn_frame)),
            ),
            FunctionSubsystemAdapter(
                "persona",
                lambda ctx: [persona_signal(ctx.event_bus, ctx.frame("persona"))],
            ),
            FunctionSubsystemAdapter(
                "origin",
                lambda ctx: optional(origin_signal(ctx.event_bus, ctx.frame("origin"))),
            ),
            FunctionSubsystemAdapter(
                "worldops",
                lambda ctx: optional(
                    worldops_signal(
                        ctx.event_bus,
                        ctx.frame("worldops"),
                        ctx.frame("behavior"),
                        ctx.turn_frame,
                    )
                ),
            ),
            FunctionSubsystemAdapter(
                "relationship",
                lambda ctx: [
                    relationship_signal(
                        ctx.event_bus,
                        ctx.state,
                        ctx.session_context,
                        ctx.turn_frame,
                    )
                ],
            ),
        ]
    )


def timed_signal(name: str, build, timing: dict[str, float] | None) -> JsonDict | None:
    t0 = _timer()
    signal = build()
    if timing is not None:
        timing[name] = _elapsed_since(t0)
    return signal


def build_signal(event_bus: JsonDict, source: str, signal_type: str, **fields: Any) -> JsonDict:
    turn_id = event_bus["turn_id"]
    return {
        "id": f"sig_{source}_{signal_type}_{turn_id}",
        "source": source,
        "type": signal_type,
        "priority": fields.pop("priority", 0.5),
        "confidence": fields.pop("confidence", 0.7),
        "visibility": fields.pop("visibility", "candidate"),
        "interrupt_level": fields.pop("interrupt_level", "low"),
        "emotional_weight": fields.pop("emotional_weight", 0.0),
        "relation_weight": fields.pop("relation_weight", 0.0),
        "expires_in_turns": fields.pop("expires_in_turns", 1),
        **fields,
    }


def dialogue_signal(event_bus: JsonDict, turn_frame: JsonDict, dialogue_control_frame: JsonDict) -> JsonDict:
    interpretation = turn_frame.get("interpretation", {})
    return build_signal(
        event_bus,
        "dialogue",
        turn_frame["speech_act"],
        content=turn_frame["decision_reason"],
        intent="set_reply_mode",
        priority=0.95,
        confidence=0.9,
        visibility="active",
        interrupt_level="none",
        suggested_use=f"按 {turn_frame['reply_shape']['style']} 组织本轮回应。",
        constraints=turn_frame["blocked_inferences"],
        metadata={
            "question_budget": dialogue_control_frame["question_budget"],
            "context_relevance": turn_frame["context_relevance"],
            "interpreted_intent": interpretation.get("intent", {}).get("primary"),
            "conversation_move": interpretation.get("conversation_move"),
            "resolved_targets": interpretation.get("context_reference", {}).get("resolved_targets", []),
            "domain": interpretation.get("domain", {}),
            "framing": interpretation.get("framing", {}),
        },
    )


def behavior_signal(event_bus: JsonDict, behavior_frame: JsonDict) -> JsonDict:
    scene_type = behavior_frame.get("scene_type", "")
    work_like = scene_type in {"project_design", "focus_work"}
    return build_signal(
        event_bus,
        "behavior",
        "scene_context",
        content=behavior_frame.get("scene_summary", ""),
        intent="control_interruption",
        priority=0.78 if work_like else 0.55,
        confidence=0.82,
        visibility="background",
        interrupt_level="medium" if work_like else "low",
        suggested_use="决定主动性窗口和回复打扰程度，不要机械复述应用或窗口。",
        metadata={
            "scene_type": scene_type,
            "is_work_like": work_like,
            "changed": behavior_frame.get("changed", False),
            "interruptibility": behavior_frame.get("interruptibility", "unknown"),
            "interruptibility_confidence": behavior_frame.get("interruptibility_confidence", 0.0),
            "user_present": behavior_frame.get("user_present", False),
            "focus_level": behavior_frame.get("focus_level", "unknown"),
        },
    )


def memory_signal(event_bus: JsonDict, recollection_frame: JsonDict, turn_frame: JsonDict) -> JsonDict | None:
    ids = recollection_frame.get("retrieved_ids", [])
    if not ids:
        return None
    explicit = recollection_frame.get("surface_action") == "gist"
    return build_signal(
        event_bus,
        "memory",
        "relevant_memory",
        content="；".join(recollection_frame.get("memory_summaries", [])),
        intent="support_reply",
        priority=0.72 if explicit else 0.42,
        confidence=0.78,
        visibility="candidate" if turn_frame["context_relevance"] != "low" else "silent",
        interrupt_level="low",
        relation_weight=0.45,
        evidence={"memory_ids": ids},
        suggested_use="可影响理解和措辞；默认不要复读成“我记得你...”",
        constraints=recollection_frame.get("forbidden_disclosures", []),
    )


def persona_signal(event_bus: JsonDict, persona_frame: JsonDict) -> JsonDict:
    traits = persona_frame.get("traits", {})
    return build_signal(
        event_bus,
        "persona",
        "tone_bias",
        content="；".join(persona_frame.get("influence_summary", [])),
        intent="change_tone",
        priority=0.72,
        confidence=0.86,
        visibility="active",
        interrupt_level="none",
        emotional_weight=float(traits.get("warmth", 0.6)),
        suggested_use="影响语气、边界和表达密度；不要把 trait 数值说给用户。",
        metadata={
            "warmth": traits.get("warmth"),
            "playfulness": traits.get("playfulness"),
            "caution": traits.get("caution"),
        },
    )


def origin_signal(event_bus: JsonDict, origin_frame: JsonDict) -> JsonDict | None:
    if not origin_frame.get("template_id"):
        return None
    return build_signal(
        event_bus,
        "origin",
        "cold_start_texture",
        content=origin_frame.get("summary", ""),
        intent="shape_initial_personhood",
        priority=0.5,
        confidence=0.82,
        visibility="background",
        interrupt_level="none",
        emotional_weight=0.35,
        relation_weight=0.25,
        suggested_use="作为 Sora 的初始生活底色和表达纹理，不要自我介绍成模板。",
        constraints=["不要说“我的模板是...”", "不要把世界观设定长篇解释给用户"],
        metadata={
            "template_id": origin_frame.get("template_id"),
            "name": origin_frame.get("name"),
            "speech_habits": origin_frame.get("speech_habits", []),
        },
    )


def worldops_signal(event_bus: JsonDict, worldops_frame: JsonDict, behavior_frame: JsonDict, turn_frame: JsonDict) -> JsonDict | None:
    event = worldops_frame.get("selected_event")
    if not event:
        return build_signal(
            event_bus,
            "worldops",
            "no_active_event",
            content="当前没有激活的 WorldOps 事件。",
            intent="suppress",
            priority=0.1,
            confidence=0.9,
            visibility="silent",
            interrupt_level="none",
            suggested_use="本轮不要凭空发明 Sora 那边的新鲜事。",
        )
    user_invited = turn_frame["speech_act"] == "sora_life_share" or turn_asks_active_worldops_event(turn_frame, event)
    non_work = is_non_work_scene(behavior_frame)
    visibility = "active" if user_invited else ("candidate" if non_work else "silent")
    return build_signal(
        event_bus,
        "worldops",
        "world_event_share",
        content=event.get("one_sentence", ""),
        intent="suggest_topic",
        priority=0.82 if user_invited else (0.48 if non_work else 0.18),
        confidence=0.72,
        visibility=visibility,
        interrupt_level="low",
        emotional_weight=0.25,
        relation_weight=0.35,
        evidence={"event_ids": [event.get("event_id")]},
        suggested_use="用户问 Sora 生活时可主动讲；非工作空闲时可作为轻量主动话题候选。",
        constraints=worldops_frame.get("forbidden_behaviors", []),
        metadata={
            "expression_level": worldops_frame.get("expression_level"),
            "scene_type": behavior_frame.get("scene_type"),
            "user_invited": user_invited,
            "matched_active_event": turn_asks_active_worldops_event(turn_frame, event),
        },
    )


def turn_asks_active_worldops_event(turn_frame: JsonDict, event: JsonDict) -> bool:
    if not allows_sora_world_lore_answer(turn_frame):
        return False
    interpretation = turn_frame.get("interpretation", {})
    reference = interpretation.get("context_reference", {})
    for target in reference.get("resolved_targets", []) or []:
        if target.get("event_id") == event.get("event_id"):
            return True
        if target.get("target_type") == "active_worldops_event":
            return True
        if text_mentions_worldops_event(str(target.get("summary", "")), event):
            return True
    topic = interpretation.get("topic", {})
    return text_mentions_worldops_event(str(topic.get("current_topic", "")), event)


def relationship_signal(event_bus: JsonDict, state: JsonDict, session_context: JsonDict, turn_frame: JsonDict) -> JsonDict:
    relationship = state["persona"].get("relationship_state", {})
    user_cares = turn_frame["speech_act"] == "sora_life_share"
    return build_signal(
        event_bus,
        "relationship",
        "relationship_rhythm",
        content="维护当前熟悉度、共同话题和边界节奏。",
        intent="calibrate_closeness",
        priority=0.62 if user_cares else 0.45,
        confidence=0.7,
        visibility="background",
        interrupt_level="none",
        relation_weight=0.8 if user_cares else 0.45,
        suggested_use="可以让回复更接住关心，但不要突然升高亲密度。",
        metadata={
            "familiarity": relationship.get("familiarity"),
            "shared_topic_count": len(relationship.get("shared_topics", [])) if isinstance(relationship.get("shared_topics"), list) else 0,
            "history_items": session_context.get("recent_turns_available", 0),
        },
    )


def build_initiative_window(state: JsonDict, event_bus: JsonDict, behavior_frame: JsonDict, turn_frame: JsonDict, signals: list[JsonDict]) -> JsonDict:
    initiative = state.get("initiative", {})
    if not initiative.get("enabled", True):
        return initiative_window(False, "initiative disabled", behavior_frame)

    if turn_frame["speech_act"] == "proactive_opening":
        trigger = turn_frame.get("proactive_trigger", {})
        idle_seconds = int(trigger.get("idle_seconds", 0) or 0)
        threshold = int(initiative.get("idle_threshold_sec", 180) or 180)
        if idle_seconds < threshold:
            return initiative_window(
                False,
                f"空闲 {idle_seconds} 秒，尚未达到 {threshold} 秒阈值。",
                behavior_frame,
            )
        interruptibility = behavior_frame.get("interruptibility", "unknown")
        confidence = float(behavior_frame.get("interruptibility_confidence", 0.0) or 0.0)
        if not behavior_frame.get("user_present", False):
            return initiative_window(False, "感知系统判断用户当前不在设备前。", behavior_frame)
        if behavior_frame.get("afk", False):
            return initiative_window(False, "感知系统判断用户当前处于离开状态。", behavior_frame)
        if behavior_frame.get("privacy_level") not in {"low", "normal"}:
            return initiative_window(False, "当前场景隐私风险较高，不主动搭话。", behavior_frame)
        if interruptibility != "available":
            return initiative_window(
                False,
                f"感知系统判断当前不可主动打扰：interruptibility={interruptibility}。",
                behavior_frame,
            )
        if confidence < 0.7:
            return initiative_window(
                False,
                f"可打扰性置信度不足：{confidence:.2f} < 0.70。",
                behavior_frame,
            )
        if behavior_frame.get("focus_level") == "high":
            return initiative_window(False, "感知系统判断用户专注度较高。", behavior_frame)
        if proactive_cooldown_active(initiative):
            return initiative_window(False, "主动搭话仍在冷却时间内。", behavior_frame)
        return {
            "allowed": True,
            "reason": "达到最低空闲阈值，且感知系统判断用户可被低打扰搭话。",
            "max_interrupt_level": "low",
            "max_length": "short",
            "allowed_types": ["self_life_share"],
            "cooldowns": initiative.get("cooldowns", {}),
            "candidate_signal_ids": [
                signal["id"]
                for signal in signals
                if signal["source"] in {"worldops", "memory", "relationship"}
            ],
        }

    if turn_frame["speech_act"] == "greeting_ack":
        return initiative_window(False, "用户只是问候，本轮不主动展开新话题。", behavior_frame)

    if user_has_task_intent(event_bus):
        return initiative_window(False, "用户输入包含明确任务或工程推进意图。", behavior_frame)

    if behavior_frame.get("scene_type") == "focus_work":
        return initiative_window(False, "用户处于专注工作场景。", behavior_frame)

    if is_non_work_scene(behavior_frame):
        return {
            "allowed": True,
            "reason": "用户处于非工作陪伴场景，允许低打扰主动话题。",
            "max_interrupt_level": "low",
            "max_length": "short",
            "allowed_types": ["self_life_share", "gentle_check_in", "topic_resume", "quiet_presence"],
            "cooldowns": initiative.get("cooldowns", {}),
            "candidate_signal_ids": [signal["id"] for signal in signals if signal["source"] in {"worldops", "memory", "relationship"}],
        }

    return initiative_window(False, "当前场景默认不打开主动窗口。", behavior_frame)


def initiative_window(allowed: bool, reason: str, behavior_frame: JsonDict) -> JsonDict:
    return {
        "allowed": allowed,
        "reason": reason,
        "max_interrupt_level": "none" if not allowed else "low",
        "max_length": "none" if not allowed else "short",
        "allowed_types": [],
        "cooldowns": {},
        "scene_type": behavior_frame.get("scene_type"),
        "candidate_signal_ids": [],
    }


def build_initiative_signals(state: JsonDict, event_bus: JsonDict, initiative_window_frame: JsonDict, base_signals: list[JsonDict], worldops_frame: JsonDict) -> list[JsonDict]:
    if not initiative_window_frame.get("allowed"):
        return [
            build_signal(
                event_bus,
                "initiative",
                "initiative_suppressed",
                content=initiative_window_frame["reason"],
                intent="suppress",
                priority=0.2,
                confidence=0.82,
                visibility="silent",
                interrupt_level="none",
                suggested_use="不主动开新话题。",
            )
        ]

    world_signal = next((signal for signal in base_signals if signal["source"] == "worldops" and signal["type"] == "world_event_share"), None)
    if world_signal and state.get("initiative", {}).get("user_preference", {}).get("worldops_share_when_free", True):
        event_id = (world_signal.get("evidence") or {}).get("event_ids", [None])[0]
        # Topic history suppresses repeated unsolicited openings only. The
        # underlying WorldOps event remains available when the user asks.
        used_topics = {
            item.get("topic_id")
            for item in state.get("initiative", {}).get("proactive_topic_history", [])
        }
        if event_bus.get("turn_kind") == "proactive" and event_id in used_topics:
            world_signal = None
    if world_signal and state.get("initiative", {}).get("user_preference", {}).get("worldops_share_when_free", True):
        event_id = (world_signal.get("evidence") or {}).get("event_ids", [None])[0]
        bias = float(state.get("initiative", {}).get("origin_bias", {}).get("self_life_share", 0.0) or 0.0)
        return [
            build_signal(
                event_bus,
                "initiative",
                "self_life_share",
                topic_source="worldops",
                topic_id=event_id,
                content=f"Sora 可以轻轻分享：{world_signal.get('content', '')}",
                intent="suggest_topic",
                priority=round(min(0.9, 0.58 + bias), 3),
                confidence=0.72,
                visibility="candidate",
                interrupt_level="low",
                relation_weight=0.35,
                cooldown_key="worldops_self_life_share",
                suggested_opening="我这边倒有个小插曲。",
                suggested_use="只抛一个短入口，用户不接就自然收回。",
                constraints=["不要打断用户工作流", "不要讲太长", "用户没接就自然收回"],
            )
        ]

    return [
        build_signal(
            event_bus,
            "initiative",
            "quiet_presence",
            content="没有合适内容源时保持轻量陪伴。",
            intent="support_reply",
            priority=0.35,
            confidence=0.65,
            visibility="candidate",
            interrupt_level="none",
            suggested_use="不主动展开复杂话题。",
        )
    ]


def extract_json_object(text: str) -> JsonDict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object found")
    value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def normalize_arbitration_judgment(payload: JsonDict) -> JsonDict:
    scores: JsonDict = {}
    for raw in payload.get("signal_scores", []):
        if not isinstance(raw, dict) or not raw.get("signal_id"):
            continue
        relevance = clamp01(raw.get("semantic_relevance", 0.0))
        naturalness = clamp01(raw.get("naturalness", 0.0))
        interruption = clamp01(raw.get("interruption_risk", 0.0))
        boundary = clamp01(raw.get("boundary_risk", 0.0))
        final_score = round(
            0.45 * relevance
            + 0.35 * naturalness
            + 0.10 * (1.0 - interruption)
            + 0.10 * (1.0 - boundary),
            4,
        )
        visibility = str(raw.get("recommended_visibility", "background"))
        if visibility not in {"active", "background", "suppress"}:
            visibility = "background"
        scores[str(raw["signal_id"])] = {
            "semantic_relevance": relevance,
            "naturalness": naturalness,
            "interruption_risk": interruption,
            "boundary_risk": boundary,
            "recommended_visibility": visibility,
            "final_score": final_score,
            "reason": str(raw.get("reason", ""))[:240],
        }
    return {
        "mode": "llm",
        "scores": scores,
        "overall": payload.get("overall", {}),
    }


def turn_interpretation_schema() -> JsonDict:
    return {
        "version": "turn_interpretation_v1",
        "speech_act": [
            "greeting",
            "question",
            "request",
            "correction",
            "complaint",
            "preference_statement",
            "status_update",
            "joke",
            "roleplay_invite",
            "planning",
            "proactive_trigger",
        ],
        "conversation_move": [
            "new_topic",
            "continue_previous_topic",
            "repair_or_correction",
            "clarification_request",
            "answer_to_assistant_question",
            "greeting_reentry",
            "emotional_disclosure",
            "task_request",
            "meta_discussion",
        ],
        "intent_primary": [
            "ask_for_advice",
            "ask_for_information",
            "ask_for_explanation",
            "ask_for_identity",
            "ask_for_action",
            "share_state",
            "express_emotion",
            "set_preference",
            "correct_assumption",
            "continue_chat",
            "invite_assistant_self_disclosure",
        ],
        "reference_type": [
            "none",
            "explicit",
            "implicit_deictic",
            "ellipsis",
            "correction",
            "topic_resume",
        ],
        "domain_primary": [
            "general",
            "health",
            "legal",
            "finance",
            "coding",
            "project_design",
            "relationship",
            "emotional_support",
            "personal_memory",
            "world_lore",
            "daily_life",
        ],
        "risk_level": ["none", "low", "medium", "high"],
        "frame_role": ["none", "literal_topic", "metaphor_or_style", "roleplay", "emotional_distance"],
    }


def normalize_turn_interpretation(payload: JsonDict) -> JsonDict:
    schema = turn_interpretation_schema()
    intent = payload.get("intent") if isinstance(payload.get("intent"), dict) else {}
    context_reference = payload.get("context_reference") if isinstance(payload.get("context_reference"), dict) else {}
    topic = payload.get("topic") if isinstance(payload.get("topic"), dict) else {}
    domain = payload.get("domain") if isinstance(payload.get("domain"), dict) else {}
    framing = payload.get("framing") if isinstance(payload.get("framing"), dict) else {}
    response_requirements = payload.get("response_requirements") if isinstance(payload.get("response_requirements"), dict) else {}
    return {
        "mode": payload.get("mode", "llm"),
        "version": "turn_interpretation_v1",
        "input_summary": str(payload.get("input_summary", ""))[:300],
        "speech_act": enum_value(payload.get("speech_act"), schema["speech_act"], "question"),
        "conversation_move": enum_value(payload.get("conversation_move"), schema["conversation_move"], "new_topic"),
        "intent": {
            "primary": enum_value(intent.get("primary"), schema["intent_primary"], "ask_for_information"),
            "secondary": [str(item) for item in intent.get("secondary", []) if item][:5],
            "confidence": clamp01(intent.get("confidence", payload.get("confidence", 0.5))),
        },
        "context_reference": {
            "references_previous_context": bool(context_reference.get("references_previous_context", False)),
            "reference_type": enum_value(context_reference.get("reference_type"), schema["reference_type"], "none"),
            "resolved_targets": normalize_resolved_targets(context_reference.get("resolved_targets", [])),
            "unresolved_references": [str(item)[:160] for item in context_reference.get("unresolved_references", []) if item][:5],
        },
        "topic": {
            "current_topic": str(topic.get("current_topic", ""))[:200],
            "topic_shift": str(topic.get("topic_shift", "unknown"))[:80],
            "topic_continuity": str(topic.get("topic_continuity", "unknown"))[:80],
            "user_visible_topic_label": str(topic.get("user_visible_topic_label", ""))[:120],
        },
        "domain": {
            "primary": enum_value(domain.get("primary"), schema["domain_primary"], "general"),
            "secondary": [str(item) for item in domain.get("secondary", []) if item][:5],
            "risk_level": enum_value(domain.get("risk_level"), schema["risk_level"], "low"),
            "safety_policies": [str(item) for item in domain.get("safety_policies", []) if item][:8],
        },
        "framing": {
            "requested_frame": str(framing.get("requested_frame", "none"))[:120],
            "frame_role": enum_value(framing.get("frame_role"), schema["frame_role"], "none"),
            "should_answer_in_frame": bool(framing.get("should_answer_in_frame", False)),
            "frame_must_not_override_reality": bool(framing.get("frame_must_not_override_reality", True)),
        },
        "response_requirements": {
            "answer_directness": str(response_requirements.get("answer_directness", "direct"))[:80],
            "needs_clarification": bool(response_requirements.get("needs_clarification", False)),
            "question_budget": int(response_requirements.get("question_budget", 0) or 0),
            "should_acknowledge_context": bool(response_requirements.get("should_acknowledge_context", False)),
            "should_avoid": [str(item)[:160] for item in response_requirements.get("should_avoid", []) if item][:8],
        },
        "confidence": clamp01(payload.get("confidence", intent.get("confidence", 0.5))),
        "fallback_if_uncertain": str(payload.get("fallback_if_uncertain", "briefly clarify before answering"))[:200],
    }


def enum_value(value: Any, allowed: list[str], default: str) -> str:
    text = str(value or "")
    return text if text in allowed else default


def normalize_resolved_targets(value: Any) -> list[JsonDict]:
    if not isinstance(value, list):
        return []
    targets: list[JsonDict] = []
    for item in value[:5]:
        if not isinstance(item, dict):
            continue
        targets.append({
            "target_type": str(item.get("target_type", ""))[:80],
            "source_turn": str(item.get("source_turn", ""))[:80],
            "event_id": str(item.get("event_id", ""))[:120] or None,
            "summary": str(item.get("summary", ""))[:240],
            "confidence": clamp01(item.get("confidence", 0.5)),
        })
    return targets


def rule_turn_interpretation(
    user_input: str,
    recent_turns: list[JsonDict],
    proactive_trigger: JsonDict | None = None,
) -> JsonDict:
    if proactive_trigger:
        return normalize_turn_interpretation({
            "mode": "rule",
            "speech_act": "proactive_trigger",
            "conversation_move": "new_topic",
            "intent": {"primary": "continue_chat", "confidence": 0.7},
            "domain": {"primary": "general", "risk_level": "low"},
        })
    text = user_input.strip()
    references_previous = bool(recent_turns) and any(
        term in text for term in ["这个", "那个", "这样", "那怎么办", "怎么办", "有什么办法", "继续", "我是说", "你刚才"]
    )
    last_user = next((turn for turn in reversed(recent_turns) if turn.get("role") == "user"), {})
    resolved_targets = []
    if references_previous and last_user:
        resolved_targets.append({
            "target_type": "previous_user_turn",
            "source_turn": "last_user_turn",
            "summary": str(last_user.get("content", ""))[:160],
            "confidence": 0.65,
        })
    domain = "health" if any(term in text + " " + str(last_user.get("content", "")) for term in ["疼", "痛", "腰", "病", "医生", "治疗"]) else "general"
    risk = "medium" if domain == "health" else "low"
    frame_role = "metaphor_or_style" if any(term in text for term in ["你们那个世界", "你那边", "按你们", "如果是 Sora"]) and references_previous else "none"
    return normalize_turn_interpretation({
        "mode": "rule",
        "input_summary": text[:200],
        "speech_act": "question" if "?" in text or "？" in text or "吗" in text else "status_update",
        "conversation_move": "continue_previous_topic" if references_previous else "new_topic",
        "intent": {
            "primary": "ask_for_advice" if "办法" in text or "怎么办" in text else "ask_for_information",
            "confidence": 0.68 if references_previous else 0.55,
        },
        "context_reference": {
            "references_previous_context": references_previous,
            "reference_type": "implicit_deictic" if references_previous else "none",
            "resolved_targets": resolved_targets,
        },
        "topic": {
            "current_topic": resolved_targets[0]["summary"] if resolved_targets else text[:120],
            "topic_shift": "none" if references_previous else "unknown",
            "topic_continuity": "strong" if references_previous else "unknown",
        },
        "domain": {
            "primary": domain,
            "risk_level": risk,
            "safety_policies": ["health_advice", "no_diagnosis"] if domain == "health" else [],
        },
        "framing": {
            "requested_frame": "sora_world" if "世界" in text or "你那边" in text else "none",
            "frame_role": frame_role,
            "should_answer_in_frame": frame_role != "none",
            "frame_must_not_override_reality": True,
        },
        "response_requirements": {
            "question_budget": 0 if references_previous else 1,
            "should_acknowledge_context": references_previous,
            "should_avoid": ["ignoring previous context"] if references_previous else [],
        },
        "confidence": 0.68 if references_previous else 0.55,
    })


def clamp01(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, number)), 4)


def is_semantic_gray_signal(signal: JsonDict, initiative_window_frame: JsonDict) -> bool:
    if signal.get("source") == "initiative":
        return bool(
            initiative_window_frame.get("allowed")
            and signal.get("type") in initiative_window_frame.get("allowed_types", [])
        )
    return signal.get("source") in {"memory", "worldops", "relationship"}


def score_gray_signals(
    semantic_judger: ArbitrationJudger | None,
    gray_signals: list[JsonDict],
    initiative_window_frame: JsonDict,
    turn_frame: JsonDict,
    behavior_frame: JsonDict,
) -> JsonDict:
    if not gray_signals:
        return {"mode": "skipped", "scores": {}, "reason": "no_gray_signals"}
    if semantic_judger is None:
        return {"mode": "rule_only", "scores": {}, "reason": "no_semantic_judger"}
    context = {
        "turn_frame": turn_frame,
        "behavior_frame": behavior_frame,
        "initiative_window": initiative_window_frame,
        "hard_policy": {
            "llm_cannot_override": [
                "greeting_ack_blocks_topic_expansion",
                "focus_work_blocks_proactive_interruption",
                "memory_raw_evidence_disclosure_forbidden",
            ],
            "semantic_score_is_advisory": True,
        },
    }
    try:
        return semantic_judger.score(context, gray_signals)
    except Exception as exc:
        return {
            "mode": "llm_degraded",
            "scores": {},
            "reason": f"{type(exc).__name__}: {exc}",
        }


def semantic_blocks_signal(signal: JsonDict, scores: JsonDict) -> bool:
    score = scores.get(signal.get("id"))
    if not score:
        return False
    return score.get("recommended_visibility") == "suppress" or float(score.get("final_score", 0.0)) < 0.45


def semantic_promotes_signal(signal: JsonDict, scores: JsonDict) -> bool:
    score = scores.get(signal.get("id"))
    if not score:
        return False
    if score.get("recommended_visibility") != "active":
        return False
    if signal.get("source") not in {"memory", "worldops", "relationship"}:
        return False
    return float(score.get("final_score", 0.0)) >= 0.72


def semantic_backgrounds_signal(signal: JsonDict, scores: JsonDict) -> bool:
    score = scores.get(signal.get("id"))
    if not score:
        return False
    return score.get("recommended_visibility") == "background" and float(score.get("final_score", 0.0)) >= 0.45


def semantic_suppress_reason(signal: JsonDict, scores: JsonDict) -> str:
    score = scores.get(signal.get("id"), {})
    reason = score.get("reason") or "LLM semantic judge marked this signal unsuitable for expression."
    return f"semantic_judge_suppressed: {reason}"


def score_entry(signal: JsonDict, decision: str, reason: str, scores: JsonDict) -> JsonDict:
    score = scores.get(signal.get("id"), {})
    return {
        "id": signal.get("id"),
        "source": signal.get("source"),
        "type": signal.get("type"),
        "decision": decision,
        "reason": reason,
        "priority": signal.get("priority"),
        "confidence": signal.get("confidence"),
        "semantic": score or None,
    }


def arbitrate_signals(
    signals: list[JsonDict],
    initiative_window_frame: JsonDict,
    turn_frame: JsonDict,
    behavior_frame: JsonDict,
    semantic_judger: ArbitrationJudger | None = None,
) -> JsonDict:
    selected: list[str] = []
    background: list[str] = []
    suppressed: list[JsonDict] = []
    score_breakdown: list[JsonDict] = []
    directives = {
        "tone": "warm_natural",
        "length": "short" if turn_frame["speech_act"] == "greeting_ack" else "medium",
        "question_budget": 0 if not turn_frame["reply_shape"].get("can_ask_question", True) else 1,
        "context_depth": "shallow" if turn_frame["context_relevance"] in {"low", "low_commitment"} else "normal",
        "worldops_expression": "none",
        "worldops_event_expression": "none",
        "sora_world_lore_answer": "allowed" if allows_sora_world_lore_answer(turn_frame) else "none",
        "memory_expression": "implicit",
        "expression_hint": "text",
    }
    gray_signals = [
        signal
        for signal in signals
        if signal.get("visibility", "candidate") == "candidate"
        and is_semantic_gray_signal(signal, initiative_window_frame)
    ]
    semantic_judgment = score_gray_signals(
        semantic_judger,
        gray_signals,
        initiative_window_frame,
        turn_frame,
        behavior_frame,
    )
    semantic_scores = semantic_judgment.get("scores", {})

    for signal in sorted(signals, key=lambda item: item.get("priority", 0), reverse=True):
        source = signal["source"]
        visibility = signal.get("visibility", "candidate")
        if visibility == "active":
            selected.append(signal["id"])
            score_breakdown.append(score_entry(signal, "selected", "active_visibility", semantic_scores))
            if source == "worldops":
                directives["worldops_expression"] = "active"
                directives["worldops_event_expression"] = "active"
                directives["expression_hint"] = "bubble"
            continue
        if source == "initiative" and initiative_window_frame.get("allowed") and signal.get("type") in initiative_window_frame.get("allowed_types", []):
            if semantic_blocks_signal(signal, semantic_scores):
                reason = semantic_suppress_reason(signal, semantic_scores)
                suppressed.append({"id": signal["id"], "source": source, "type": signal["type"], "reason": reason})
                score_breakdown.append(score_entry(signal, "suppressed", reason, semantic_scores))
                continue
            selected.append(signal["id"])
            score_breakdown.append(score_entry(signal, "selected", "initiative_allowed_semantic_pass", semantic_scores))
            directives["worldops_expression"] = "initiative_candidate" if signal.get("topic_source") == "worldops" else directives["worldops_expression"]
            directives["worldops_event_expression"] = "initiative_candidate" if signal.get("topic_source") == "worldops" else directives["worldops_event_expression"]
            directives["expression_hint"] = "text"
            continue
        if semantic_promotes_signal(signal, semantic_scores):
            selected.append(signal["id"])
            score_breakdown.append(score_entry(signal, "selected", "semantic_judge_promoted", semantic_scores))
            if source == "worldops":
                directives["worldops_expression"] = "active"
                directives["worldops_event_expression"] = "active"
            continue
        if semantic_backgrounds_signal(signal, semantic_scores):
            background.append(signal["id"])
            score_breakdown.append(score_entry(signal, "background", "semantic_judge_background", semantic_scores))
            continue
        if visibility == "background":
            background.append(signal["id"])
            score_breakdown.append(score_entry(signal, "background", "background_visibility", semantic_scores))
            continue
        reason = suppress_reason(signal, initiative_window_frame, turn_frame, behavior_frame)
        suppressed.append({"id": signal["id"], "source": source, "type": signal["type"], "reason": reason})
        score_breakdown.append(score_entry(signal, "suppressed", reason, semantic_scores))

    primary_mode = {
        "greeting_ack": "light_greeting_reply",
        "sora_life_share": "sora_life_share_reply",
    }.get(turn_frame["speech_act"], "companion_reply")
    if initiative_window_frame.get("allowed") and any(sig_id.startswith("sig_initiative_self_life_share") for sig_id in selected):
        primary_mode = "initiative_self_life_share"
    if turn_frame["speech_act"] == "proactive_opening":
        primary_mode = (
            "initiative_self_life_share"
            if any(sig_id.startswith("sig_initiative_self_life_share") for sig_id in selected)
            else "proactive_silent"
        )

    return {
        "primary_mode": primary_mode,
        "selected_signals": selected,
        "background_signals": background,
        "suppressed_signals": suppressed,
        "response_directives": directives,
        "semantic_judgment": semantic_judgment,
        "score_breakdown": score_breakdown,
        "state_candidates": {
            "memory_hooks": [],
            "growth_events": [],
            "relationship_events": [],
            "worldops_consequences": [],
        },
        "audit_reason": "多子系统只产出 Signal；Arbiter 统一决定表达、背景、抑制和状态候选。",
    }


def allows_sora_world_lore_answer(turn_frame: JsonDict) -> bool:
    interpretation = turn_frame.get("interpretation", {})
    domain = interpretation.get("domain", {})
    framing = interpretation.get("framing", {})
    if turn_frame["speech_act"] == "sora_life_share":
        return True
    if domain.get("primary") == "world_lore":
        return True
    return framing.get("frame_role") in {"literal_topic", "metaphor_or_style", "roleplay"}


def suppress_reason(signal: JsonDict, initiative_window_frame: JsonDict, turn_frame: JsonDict, behavior_frame: JsonDict) -> str:
    if signal["source"] == "worldops" and turn_frame["speech_act"] == "greeting_ack":
        return "用户只是问候，不主动插入 WorldOps。"
    if signal["source"] == "initiative" and not initiative_window_frame.get("allowed"):
        return initiative_window_frame.get("reason", "主动窗口未开启。")
    if signal["source"] == "worldops" and allows_sora_world_lore_answer(turn_frame):
        return "本轮可以回答 Sora 世界设定，但未明确命中当前 WorldOps 事件，事件保持背景。"
    if signal["source"] == "worldops" and not is_non_work_scene(behavior_frame):
        return "当前不是非工作陪伴场景，WorldOps 保持静默或背景。"
    return "优先级或可见性不足，未进入本轮表达。"


def is_non_work_scene(behavior_frame: JsonDict) -> bool:
    return behavior_frame.get("scene_type") in {"game_companion", "watch_companion"} and not behavior_frame.get("afk", False)


def user_has_task_intent(event_bus: JsonDict) -> bool:
    text = ""
    for event in event_bus.get("events", []):
        if event.get("type") == "user_input":
            text = str(event.get("payload", {}).get("text", ""))
            break
    return any(term in text for term in ["设计", "实现", "开干", "更新", "打包", "修", "改", "方案", "架构", "代码", "测试"])


def proactive_cooldown_active(initiative: JsonDict) -> bool:
    last = initiative.get("last_initiated_at")
    if not last:
        return False
    minimum_ms = int(initiative.get("minimum_interval_sec", 900)) * 1000
    return now_ms() - int(last) < minimum_ms


def record_proactive_attempt(
    state: JsonDict,
    trigger: JsonDict,
    *,
    emitted: bool,
    reply: str | None = None,
    reason: str | None = None,
    topic_id: str | None = None,
) -> None:
    initiative = state.setdefault("initiative", {})
    timestamp = now_ms()
    if emitted:
        initiative["last_initiated_at"] = timestamp
        if topic_id:
            history = initiative.setdefault("proactive_topic_history", [])
            history.append({"topic_id": topic_id, "used_at": timestamp})
            del history[:-50]
    attempts = initiative.setdefault("recent_attempts", [])
    attempts.append({
        "at": timestamp,
        "trigger": trigger,
        "emitted": emitted,
        "reply": reply,
        "reason": reason,
        "topic_id": topic_id,
    })
    del attempts[:-20]


def selected_proactive_topic_id(context_pack: JsonDict) -> str | None:
    selected = set(context_pack.get("arbitration_result", {}).get("selected_signals", []))
    for signal in context_pack.get("signals", []):
        if signal.get("id") in selected and signal.get("source") == "initiative":
            return signal.get("topic_id")
    return None


def finalize_silent_proactive_result(
    state: JsonDict,
    context_pack: JsonDict,
    trace: list[JsonDict],
    timing: dict[str, float],
    trigger: JsonDict,
    start: float,
) -> JsonDict:
    window = context_pack["initiative_window"]
    reason = (
        "没有可进入表达的主动话题候选。"
        if window.get("allowed")
        else window.get("reason", "Arbiter chose silence.")
    )
    trace.append({
        "seq": len(trace) + 1,
        "kind": "proactive.silenced",
        "at": now_ms(),
        "payload": {"reason": reason},
    })
    record_proactive_attempt(state, trigger, emitted=False, reason=reason)
    timing["_total"] = _elapsed_since(start)
    result = {
        "user_input": "",
        "turn_kind": "proactive",
        "proactive_trigger": trigger,
        "emitted": False,
        "reply": "",
        "context_pack": context_pack,
        "context_pack_summary": summarize_context_pack(context_pack),
        "response_plan": None,
        "expression_event": None,
        "state_diff": {
            "session_changed": False,
            "subsystem_runtime": context_pack["policy_frame"].get("subsystem_runtime"),
            "initiative_allowed": context_pack["initiative_window"].get("allowed", False),
            "arbitration_mode": "proactive_silent",
            "policy_blocks": [reason],
        },
        "trace": trace,
        "timing": timing,
        "token_usage": {},
    }
    state["observability"]["last_trace"] = trace
    state["observability"]["last_state_diff"] = result["state_diff"]
    state["observability"]["last_response_plan"] = {}
    state["observability"]["last_context_pack"] = context_pack
    state["observability"]["last_run_result"] = result
    return result


def active_micro_quest(worldops: JsonDict, event_id: str) -> JsonDict | None:
    for session in worldops.get("micro_quest_sessions", []):
        if session.get("event_id") == event_id and session.get("status") in {"active", "completed"}:
            return session
    return None


def build_system_prompt(context_pack: JsonDict) -> str:
    core = context_pack["persona_frame"]["core"]
    return render_system_prompt(core)


def build_user_prompt(context_pack: JsonDict, user_input: str) -> str:
    safe_pack = {
        "turn_interpretation": context_pack.get("turn_interpretation", {}),
        "turn_frame": context_pack["turn_frame"],
        "context_use_policy": context_pack["context_use_policy"],
        "session_context": context_pack["session_context"],
        "behavior_frame": context_pack["behavior_frame"],
        "recollection_frame": context_pack["recollection_frame"],
        "persona_frame": context_pack["persona_frame"],
        "worldops_frame": context_pack["worldops_frame"],
        "origin_frame": context_pack["origin_frame"],
        "dialogue_control_frame": context_pack["dialogue_control_frame"],
        "event_bus": context_pack["event_bus"],
        "signals": context_pack["signals"],
        "subsystem_health": context_pack.get("subsystem_health", []),
        "initiative_window": context_pack["initiative_window"],
        "arbitration_result": context_pack["arbitration_result"],
        "policy_frame": context_pack["policy_frame"],
    }
    return render_user_prompt(user_input, json.dumps(safe_pack, ensure_ascii=False, indent=2))


def route_expression(response_plan: JsonDict, context_pack: JsonDict, step) -> JsonDict:
    hint = response_plan.get("expression_hint", "text")
    world_level = context_pack["worldops_frame"].get("expression_level", "L0")
    directives = context_pack.get("arbitration_result", {}).get("response_directives", {})
    worldops_expression = directives.get("worldops_expression", "none")
    worldops_event_expression = directives.get("worldops_event_expression", worldops_expression)
    sora_world_lore_answer = directives.get("sora_world_lore_answer", "none")
    if hint == "micro_quest_invite" and world_level != "L3":
        hint = "bubble"
    event = {
        "route": hint,
        "worldops_expression_level": world_level,
        "worldops_expression": worldops_expression,
        "worldops_event_expression": worldops_event_expression,
        "sora_world_lore_answer": sora_world_lore_answer,
        "reason": "用户主动输入时可以返回文本；WorldOps 和主动性由 Arbiter 决定可见性。",
    }
    step("expression.routed", event)
    return event


def update_session(state: JsonDict, user_input: str, reply: str) -> None:
    session = state["session"]
    session["dialogue_state"]["active_topic"] = infer_active_topic(user_input)
    session["dialogue_state"]["user_goal"] = "推进 Sora Agent State Sandbox 工程验证"
    session["working_memory"]["last_reply_summary"] = reply[:120]


def build_state_diff(context_pack: JsonDict, response_plan: JsonDict, expression_event: JsonDict) -> JsonDict:
    active_worldops_event = context_pack["worldops_frame"].get("selected_event_id")
    return {
        "session_changed": True,
        "turn_interpretation": {
            "conversation_move": context_pack.get("turn_interpretation", {}).get("conversation_move"),
            "intent": context_pack.get("turn_interpretation", {}).get("intent", {}).get("primary"),
            "references_previous_context": context_pack.get("turn_interpretation", {}).get("context_reference", {}).get("references_previous_context"),
            "domain": context_pack.get("turn_interpretation", {}).get("domain", {}).get("primary"),
            "risk_level": context_pack.get("turn_interpretation", {}).get("domain", {}).get("risk_level"),
        },
        "memories_used": response_plan["used_memory_ids"],
        "persona_debug_overrides_used": [item.get("target") for item in context_pack["persona_frame"].get("debug_overrides", [])],
        "active_worldops_event": active_worldops_event,
        "worldops_event_available": active_worldops_event,
        "worldops_event_expressed": expression_event.get("worldops_event_expression") not in {None, "none"},
        "worldops_event_used": active_worldops_event if expression_event.get("worldops_event_expression") not in {None, "none"} else None,
        "sora_world_lore_answer": expression_event.get("sora_world_lore_answer", "none"),
        "origin_template": context_pack.get("origin_frame", {}).get("template_id"),
        "growth_event_created": bool(response_plan.get("growth_event_candidate")),
        "memory_hook_created": bool(response_plan.get("memory_hook_candidate")),
        "expression_route": expression_event["route"],
        "question_budget": context_pack["dialogue_control_frame"]["question_budget"],
        "speech_act": context_pack["turn_frame"]["speech_act"],
        "context_relevance": context_pack["turn_frame"]["context_relevance"],
        "recent_turns_mode": context_pack["context_use_policy"]["recent_turns_mode"],
        "subsystem_runtime": context_pack["policy_frame"].get("subsystem_runtime"),
        "signal_count": len(context_pack.get("signals", [])),
        "initiative_allowed": context_pack.get("initiative_window", {}).get("allowed", False),
        "arbitration_mode": context_pack.get("arbitration_result", {}).get("primary_mode"),
        "selected_signals": context_pack.get("arbitration_result", {}).get("selected_signals", []),
        "suppressed_signals": context_pack.get("arbitration_result", {}).get("suppressed_signals", []),
        "policy_blocks": [],
    }


def summarize_context_pack(context_pack: JsonDict) -> JsonDict:
    return {
        "scene_type": context_pack["behavior_frame"]["scene_type"],
        "behavior_changed": context_pack["behavior_frame"].get("changed", False),
        "memory_ids": context_pack["recollection_frame"]["retrieved_ids"],
        "memory_runtime": {
            "backend": context_pack["recollection_frame"].get("backend", "legacy_json"),
            "recall_trace_id": context_pack["recollection_frame"].get("recall_trace_id"),
            "latency_ms": context_pack["recollection_frame"].get("latency_ms"),
        },
        "persona_influences": context_pack["persona_frame"]["influence_summary"],
        "worldops": {
            "event": context_pack["worldops_frame"].get("selected_event_id"),
            "level": context_pack["worldops_frame"].get("expression_level"),
        },
        "origin": {
            "template_id": context_pack.get("origin_frame", {}).get("template_id"),
            "name": context_pack.get("origin_frame", {}).get("name"),
        },
        "dialogue_control": {
            "question_budget": context_pack["dialogue_control_frame"]["question_budget"],
            "recent_sora_question_streak": context_pack["dialogue_control_frame"]["recent_sora_question_streak"],
        },
        "turn_frame": {
            "conversation_move": context_pack.get("turn_interpretation", {}).get("conversation_move"),
            "intent": context_pack.get("turn_interpretation", {}).get("intent", {}).get("primary"),
            "references_previous_context": context_pack.get("turn_interpretation", {}).get("context_reference", {}).get("references_previous_context"),
            "domain": context_pack.get("turn_interpretation", {}).get("domain", {}).get("primary"),
            "speech_act": context_pack["turn_frame"]["speech_act"],
            "context_relevance": context_pack["turn_frame"]["context_relevance"],
            "recent_turns_mode": context_pack["context_use_policy"]["recent_turns_mode"],
        },
        "subsystems": {
            "runtime": context_pack["policy_frame"].get("subsystem_runtime"),
            "signal_count": len(context_pack.get("signals", [])),
            "initiative_allowed": context_pack.get("initiative_window", {}).get("allowed", False),
            "arbitration_mode": context_pack.get("arbitration_result", {}).get("primary_mode"),
        },
    }


def maybe_growth_candidate(context_pack: JsonDict, user_input: str, reply: str) -> JsonDict | None:
    if is_positive_style_feedback(user_input):
        return {
            "candidate_id": f"growth_{now_ms()}",
            "target_type": "persona_trait",
            "target_key": "playfulness",
            "direction": "+",
            "confidence": 0.55,
            "reason": "用户输入可能对轻松互动有正向反馈；只作为候选，不自动更新。",
            "evidence": {"user_input": user_input, "reply": reply[:120]},
            "recommendation": "hold",
        }
    return None


def is_positive_style_feedback(user_input: str) -> bool:
    text = user_input.strip()
    if not text:
        return False
    question_markers = ["?", "？", "吗", "嘛", "能不能", "可不可以", "可以看到", "可以看", "是否"]
    if any(marker in text for marker in question_markers):
        return False
    direct_style_feedback = [
        "我喜欢", "挺喜欢", "这个语气", "这种语气", "这样说", "这么说",
        "接住", "这个吐槽", "这种吐槽", "哈哈，可以",
    ]
    if any(term in text for term in direct_style_feedback):
        return True
    positive_terms = ["不错", "可以", "挺好", "很好", "哈哈", "有意思", "不赖", "喜欢"]
    feedback_targets = ["你", "Sora", "回复", "语气", "说法", "这样", "这个", "吐槽"]
    return any(term in text for term in positive_terms) and any(target in text for target in feedback_targets)


def maybe_memory_hook_candidate(context_pack: JsonDict, user_input: str, reply: str) -> JsonDict | None:
    world = context_pack["worldops_frame"]
    quest = world.get("micro_quest")
    if quest and quest.get("status") == "completed":
        return {
            "candidate_id": f"memory_hook_{now_ms()}",
            "source": "worldops_micro_quest",
            "event_id": world.get("selected_event_id"),
            "episode_summary": f"用户参与了 Sora 的 WorldOps 微任务：{quest.get('choice', '')}",
            "sora_feedback": reply[:160],
            "write_policy": "manual_commit_required",
        }
    return None


def infer_intent(user_input: str) -> str:
    if classify_input_mode(user_input) == "greeting":
        return "greeting"
    if any(term in user_input for term in ["方案", "设计", "架构", "工程"]):
        return "engineering_design"
    if any(term in user_input for term in ["你今天", "自己的事", "世界", "星港", "档案"]):
        return "worldops_followup"
    if any(term in user_input for term in ["记得", "之前", "上次"]):
        return "memory_grounded_reply"
    return "general_companion_reply"


def infer_active_topic(user_input: str) -> str:
    if "WorldOps" in user_input or "世界" in user_input:
        return "WorldOps 工程验证"
    if "Persona" in user_input or "人格" in user_input:
        return "Persona 工程验证"
    if "记忆" in user_input:
        return "Memory 工程验证"
    return "Sora Agent State Sandbox 工程验证"


def _keywords(text: str) -> list[str]:
    base = ["方案", "设计", "agent", "Agent", "Sora", "sora", "WorldOps", "Persona", "记忆", "工程", "workflow"]
    return [term for term in base if term in text] + [part for part in text.replace("，", " ").replace("。", " ").split() if len(part) >= 2]


def classify_input_mode(text: str) -> str:
    normalized = re.sub(r"[\s，。！？!?,.～~呀啊呢啦喔哦哈]+", "", text.strip().lower())
    if normalized in {"你", ""}:
        return "other"
    greetings = {
        "你好",
        "嗨",
        "hi",
        "hello",
        "早",
        "早上好",
        "晚上好",
        "下午好",
        "在吗",
        "在不在",
        "哈喽",
    }
    if normalized in greetings:
        return "greeting"
    return "other"


def preferred_closing(input_mode: str, should_ask_question: bool) -> str:
    if input_mode == "greeting":
        return "short_greeting_ack"
    if should_ask_question:
        return "single_specific_question"
    return "statement_or_light_offer"


def count_recent_sora_question_streak(turns: list[JsonDict]) -> int:
    streak = 0
    for turn in reversed(turns):
        if turn.get("role") != "sora":
            continue
        content = str(turn.get("content", "")).rstrip()
        if ends_with_question(content):
            streak += 1
            continue
        break
    return streak


def ends_with_question(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped:
        return False
    return stripped[-1] in {"?", "？"}


def mark_behavior_observed(state: JsonDict) -> None:
    current = state.get("behavior", {}).get("current", {})
    if current.get("changed"):
        current["changed"] = False
    current["last_observed_at"] = now_ms()


def normalize_token_usage(usage: JsonDict) -> JsonDict:
    prompt = int(usage.get("prompt", usage.get("prompt_tokens", 0)) or 0)
    completion = int(usage.get("completion", usage.get("completion_tokens", 0)) or 0)
    total = int(usage.get("total", usage.get("total_tokens", prompt + completion)) or 0)
    return {"prompt": prompt, "completion": completion, "total": total}


def clean_response(text: str) -> str:
    if not text:
        return text

    def replace(match: re.Match[str]) -> str:
        block = match.group(0)
        inner = block[1:-1]
        has_technical_token = bool(re.search(r"[A-Za-z0-9_]", inner))
        if any(hint in inner for hint in _PAREN_ASIDE_HINTS):
            return ""
        if len(inner) <= 12 and not has_technical_token:
            return ""
        return block

    cleaned = _PAREN_BLOCK_RE.sub(replace, text)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def enforce_question_budget(text: str, dialogue_control: JsonDict) -> str:
    if dialogue_control.get("question_budget", 0) > 0 or not ends_with_question(text):
        return text

    without_trailing_question = remove_trailing_question_sentence(text)
    if without_trailing_question:
        return without_trailing_question

    return "我先把这个放在这里。"


def enforce_turn_frame(text: str, turn_frame: JsonDict) -> str:
    if turn_frame.get("speech_act") != "greeting_ack":
        return text
    blocked = ["看来你", "你更喜欢", "你是不是", "你想先", "需要我"]
    if any(term in text for term in blocked):
        return trim_to_greeting_ack(text)
    return text


def trim_to_greeting_ack(text: str) -> str:
    first_sentence = re.split(r"(?<=[。！？!?])", text.strip(), maxsplit=1)[0].strip()
    if first_sentence and not any(term in first_sentence for term in ["看来你", "你更喜欢", "你是不是", "你想先", "需要我"]):
        return first_sentence
    return "你好呀。"


def remove_trailing_question_sentence(text: str) -> str:
    stripped = text.rstrip()
    match = re.search(r"([^。！？!?；;\n]*[？?])$", stripped)
    if not match:
        return ""
    remaining = stripped[: match.start()].rstrip()
    if remaining.endswith(("。", "！", "!", "；", ";")):
        return remaining
    if remaining:
        return remaining + "。"
    return ""
