from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

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


@dataclass
class AgentRunner:
    llm: LLMClient
    subsystem_registry: SubsystemRegistry | None = None

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
) -> JsonDict:
    context_start = _timer()
    behavior_frame = dict(state["behavior"]["current"])
    behavior_frame["changed"] = bool(behavior_frame.get("changed", True))
    step("behavior.frame.provided", {"scene_type": behavior_frame["scene_type"], "changed": behavior_frame["changed"]})

    turn_frame = build_turn_frame(state, user_input, proactive_trigger=proactive_trigger)
    step("turn.frame.built", {"speech_act": turn_frame["speech_act"], "context_relevance": turn_frame["context_relevance"]})

    context_use_policy = build_context_use_policy(turn_frame)
    step("context.use_policy.built", {"allowed": context_use_policy["allowed_context_sources"], "blocked": context_use_policy["blocked_context_sources"]})

    session_context = build_session_context(state, behavior_frame, context_use_policy)
    step("session.context.updated", {"turn_id": session_context["turn_id"]})

    origin_frame = build_origin_frame(state)
    step("origin.frame.provided", {"template_id": origin_frame.get("template_id"), "name": origin_frame.get("name")})

    recollection_frame = retrieve_memories(state, user_input)
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
    arbitration_result = arbitrate_signals(signals, initiative_window, turn_frame, behavior_frame)
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
        recent_turns = all_recent_turns[-6:]
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


def build_turn_frame(
    state: JsonDict,
    user_input: str,
    proactive_trigger: JsonDict | None = None,
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
        }
    input_mode = classify_input_mode(user_input)
    if input_mode == "greeting":
        return {
            "input_mode": input_mode,
            "speech_act": "greeting_ack",
            "context_relevance": "low",
            "current_turn_priority": "current_input_only",
            "blocked_inferences": ["user_preference", "topic_choice", "relationship_claim", "recent_topic_continuation"],
            "reply_shape": {
                "max_sentences": 2,
                "can_ask_question": False,
                "can_reference_recent_topic": False,
                "can_reference_worldops": False,
                "style": "short_warm_ack",
            },
            "decision_reason": "用户本轮只是轻量寒暄；上一轮话题不足以推断用户偏好或继续方向。",
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
    }


def build_context_use_policy(turn_frame: JsonDict) -> JsonDict:
    allowed = ["persona_tone"]
    blocked = []
    recent_turns_mode = "full"
    omitted_reason = None

    if turn_frame["speech_act"] == "proactive_opening":
        recent_turns_mode = "proactive_tail"
        omitted_reason = "主动搭话只注入最近 3 轮，避免携带完整对话历史。"
        allowed.extend(["recent_turns_tail", "worldops_event"])
        blocked.extend(["full_recent_turns", "memory_recollection"])
    elif turn_frame["context_relevance"] == "low":
        recent_turns_mode = "metadata_only"
        omitted_reason = "当前输入为轻量寒暄，recent_turns 只用于统计，不进入生成上下文。"
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
    user_invited = turn_frame["speech_act"] == "sora_life_share"
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
        },
    )


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


def arbitrate_signals(signals: list[JsonDict], initiative_window_frame: JsonDict, turn_frame: JsonDict, behavior_frame: JsonDict) -> JsonDict:
    selected: list[str] = []
    background: list[str] = []
    suppressed: list[JsonDict] = []
    directives = {
        "tone": "warm_natural",
        "length": "short" if turn_frame["speech_act"] == "greeting_ack" else "medium",
        "question_budget": 0 if not turn_frame["reply_shape"].get("can_ask_question", True) else 1,
        "context_depth": "shallow" if turn_frame["context_relevance"] == "low" else "normal",
        "worldops_expression": "none",
        "memory_expression": "implicit",
        "expression_hint": "text",
    }

    for signal in sorted(signals, key=lambda item: item.get("priority", 0), reverse=True):
        source = signal["source"]
        visibility = signal.get("visibility", "candidate")
        if visibility == "active":
            selected.append(signal["id"])
            if source == "worldops":
                directives["worldops_expression"] = "active"
                directives["expression_hint"] = "bubble"
            continue
        if source == "initiative" and initiative_window_frame.get("allowed") and signal.get("type") in initiative_window_frame.get("allowed_types", []):
            selected.append(signal["id"])
            directives["worldops_expression"] = "initiative_candidate" if signal.get("topic_source") == "worldops" else directives["worldops_expression"]
            directives["expression_hint"] = "text"
            continue
        if visibility == "background":
            background.append(signal["id"])
            continue
        reason = suppress_reason(signal, initiative_window_frame, turn_frame, behavior_frame)
        suppressed.append({"id": signal["id"], "source": source, "type": signal["type"], "reason": reason})

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
        "state_candidates": {
            "memory_hooks": [],
            "growth_events": [],
            "relationship_events": [],
            "worldops_consequences": [],
        },
        "audit_reason": "多子系统只产出 Signal；Arbiter 统一决定表达、背景、抑制和状态候选。",
    }


def suppress_reason(signal: JsonDict, initiative_window_frame: JsonDict, turn_frame: JsonDict, behavior_frame: JsonDict) -> str:
    if signal["source"] == "worldops" and turn_frame["speech_act"] == "greeting_ack":
        return "用户只是问候，不主动插入 WorldOps。"
    if signal["source"] == "initiative" and not initiative_window_frame.get("allowed"):
        return initiative_window_frame.get("reason", "主动窗口未开启。")
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
    worldops_expression = context_pack.get("arbitration_result", {}).get("response_directives", {}).get("worldops_expression", "none")
    if hint == "micro_quest_invite" and world_level != "L3":
        hint = "bubble"
    event = {
        "route": hint,
        "worldops_expression_level": world_level,
        "worldops_expression": worldops_expression,
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
    return {
        "session_changed": True,
        "memories_used": response_plan["used_memory_ids"],
        "persona_debug_overrides_used": [item.get("target") for item in context_pack["persona_frame"].get("debug_overrides", [])],
        "worldops_event_used": context_pack["worldops_frame"].get("selected_event_id"),
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
    if any(term in user_input for term in ["喜欢", "不错", "可以", "哈哈", "接住", "吐槽"]):
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
    blocked = ["看来你", "你更喜欢", "你是不是", "你想先", "需要我", "工程验证", "Project Sora", "我这边的事"]
    if any(term in text for term in blocked):
        return trim_to_greeting_ack(text)
    return text


def trim_to_greeting_ack(text: str) -> str:
    first_sentence = re.split(r"(?<=[。！？!?])", text.strip(), maxsplit=1)[0].strip()
    if first_sentence and not any(term in first_sentence for term in ["看来你", "你更喜欢", "你是不是", "工程验证", "Project Sora"]):
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
