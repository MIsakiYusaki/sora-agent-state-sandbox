from __future__ import annotations

from datetime import datetime
from typing import Any


JsonDict = dict[str, Any]


def render_trace_panel(result: JsonDict) -> str:
    context_pack = result.get("context_pack", {})
    behavior = context_pack.get("behavior_frame", {})
    recollection = context_pack.get("recollection_frame", {})
    persona = context_pack.get("persona_frame", {})
    worldops = context_pack.get("worldops_frame", {})
    origin = context_pack.get("origin_frame", {})
    session = context_pack.get("session_context", {})
    dialogue_control = context_pack.get("dialogue_control_frame", {})
    turn_frame = context_pack.get("turn_frame", {})
    interpretation = context_pack.get("turn_interpretation", {})
    context_use_policy = context_pack.get("context_use_policy", {})
    signals = context_pack.get("signals", [])
    initiative_window = context_pack.get("initiative_window", {})
    arbitration = context_pack.get("arbitration_result", {})

    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("🧠 Sora Agent 轨迹")
    lines.append("=" * 60)
    lines.append("")
    if result.get("turn_kind") == "proactive":
        trigger = result.get("proactive_trigger", {})
        lines.append(
            "⏰ 主动唤醒: "
            f"{trigger.get('type', 'unknown')} | idle={trigger.get('idle_seconds', 0)}s"
        )
    else:
        lines.append(f"👤 用户: {result.get('user_input', '')}")

    lines.append("")
    lines.append(f"💻 行为（{_changed_label(behavior)}）: {_format_behavior(behavior)}")
    lines.append(
        "   可打扰性: "
        f"{behavior.get('interruptibility', 'unknown')} | "
        f"confidence={float(behavior.get('interruptibility_confidence', 0) or 0):.2f} | "
        f"present={behavior.get('user_present', False)} | "
        f"focus={behavior.get('focus_level', 'unknown')}"
    )

    memory_summaries = recollection.get("memory_summaries", [])
    if recollection.get("backend"):
        lines.append("")
        lines.append(
            "🗄️ 记忆召回: "
            f"backend={recollection.get('backend')} | "
            f"latency={float(recollection.get('latency_ms', 0) or 0):.2f}ms | "
            f"trace={recollection.get('recall_trace_id', '-')}"
        )
    if memory_summaries:
        lines.append(f"🧠 记忆 ({len(memory_summaries)} 条):")
        for memory_id, summary in zip(recollection.get("retrieved_ids", []), memory_summaries):
            lines.append(f"   - [{memory_id}] {_truncate(summary, 86)}")

    if persona:
        short_state = persona.get("short_term_state", {})
        traits = persona.get("traits", {})
        core = persona.get("core", {})
        lines.append("")
        lines.append(f"🎭 人格状态: {short_state.get('mood', '-')} | energy={short_state.get('energy', '-')}")
        lines.append(f"   核心身份: {core.get('name', 'Sora')} / {core.get('species', '-')}")
        lines.append(
            "   长期特质: "
            f"warmth={traits.get('warmth', 0):.2f} "
            f"playfulness={traits.get('playfulness', 0):.2f} "
            f"caution={traits.get('caution', 0):.2f}"
        )

    if origin:
        lines.append("")
        lines.append(f"🪪 Origin: {origin.get('name', '未选择')} ({origin.get('template_id', '-')})")
        if origin.get("summary"):
            lines.append(f"   底色: {_truncate(origin.get('summary', ''), 86)}")
        habits = origin.get("speech_habits") or []
        if habits:
            lines.append(f"   表达习惯: {'；'.join(habits[:3])}")

    event = worldops.get("selected_event")
    if event:
        lines.append("")
        lines.append("⚡ 微事件 (1 条):")
        lines.append(f"   - [{worldops.get('expression_level', 'L0')}] {event.get('title', event.get('event_id'))}: {event.get('one_sentence', '')}")
    elif worldops:
        lines.append("")
        lines.append("⚡ 微事件: 当前无激活事件")

    history_items = session.get("history_items", 0)
    available_items = session.get("recent_turns_available", history_items)
    recent_mode = session.get("recent_turns_mode", "full")
    lines.append("")
    lines.append(
        f"💭 短期上下文: 注入 {history_items} 条 / 可用 {available_items} 条"
        f"（mode={recent_mode}，窗口上限 20 轮）"
    )
    if dialogue_control:
        lines.append(
            "🧭 对话控制: "
            f"speech_act={turn_frame.get('speech_act', '-')} | "
            f"context={turn_frame.get('context_relevance', '-')} | "
            f"recent_turns={context_use_policy.get('recent_turns_mode', '-')} | "
            f"input_mode={dialogue_control.get('input_mode', '-')} | "
            f"question_budget={dialogue_control.get('question_budget', 0)} | "
            f"recent_questions={dialogue_control.get('recent_sora_question_streak', 0)} | "
            f"closing={dialogue_control.get('preferred_closing', '-')}"
        )

    if interpretation:
        intent = interpretation.get("intent", {})
        reference = interpretation.get("context_reference", {})
        domain = interpretation.get("domain", {})
        framing = interpretation.get("framing", {})
        resolved_count = len(reference.get("resolved_targets", []) or [])
        lines.append(
            "意图识别: "
            f"move={interpretation.get('conversation_move', '-')} | "
            f"intent={intent.get('primary', '-')} | "
            f"refs={reference.get('references_previous_context', False)}/{resolved_count} | "
            f"domain={domain.get('primary', '-')}:{domain.get('risk_level', '-')} | "
            f"frame={framing.get('frame_role', '-')}"
        )

    if arbitration:
        source_counts: dict[str, int] = {}
        for signal in signals:
            source = str(signal.get("source", "-"))
            source_counts[source] = source_counts.get(source, 0) + 1
        source_summary = ", ".join(f"{source}:{count}" for source, count in sorted(source_counts.items())) or "-"
        lines.append(
            "🧩 子系统协同: "
            f"signals={len(signals)} [{source_summary}] | "
            f"initiative={'open' if initiative_window.get('allowed') else 'closed'} | "
            f"mode={arbitration.get('primary_mode', '-')}"
        )
        lines.append(
            "⚖️ 仲裁: "
            f"selected={len(arbitration.get('selected_signals', []))} | "
            f"background={len(arbitration.get('background_signals', []))} | "
            f"suppressed={len(arbitration.get('suppressed_signals', []))}"
        )
        health = context_pack.get("subsystem_health", [])
        degraded = [item for item in health if item.get("status") != "ok"]
        lines.append(
            "   Adapters: "
            f"ok={len(health) - len(degraded)} | degraded={len(degraded)}"
        )

    lines.append("")
    lines.append(f"💬 Sora: {result.get('reply', '')}")

    usage = _extract_usage(result)
    lines.append("")
    lines.append(f"📊 Token: {usage.get('total', 0)} (prompt={usage.get('prompt', 0)}, completion={usage.get('completion', 0)})")

    timing = result.get("timing", {})
    if timing:
        total = float(timing.get("_total", timing.get("total", 0)) or 0)
        lines.append("")
        lines.append(f"⏱️  耗时: {total:.1f}ms")
        lines.append("   Runtime phases")
        for key in ["event_bus", "context_frames", "subsystem_signals", "initiative_window", "arbitration", "response_planner", "expression_router", "state_commit"]:
            if key not in timing:
                continue
            value = float(timing.get(key) or 0)
            pct = (value / total * 100) if total else 0.0
            lines.append(f"   {key:<16} {value:>8.2f}ms  ({pct:5.1f}%)")
            for child_key in timing_child_keys(key):
                if child_key not in timing:
                    continue
                child_value = float(timing.get(child_key) or 0)
                child_pct = (child_value / total * 100) if total else 0.0
                lines.append(f"     {child_key:<14} {child_value:>8.2f}ms  ({child_pct:5.1f}%)")

    lines.append("=" * 60)
    return "\n".join(lines)


def _changed_label(behavior: JsonDict) -> str:
    return "切换" if behavior.get("changed") else "沿用"


def _format_behavior(behavior: JsonDict) -> str:
    changed_at = _format_time(behavior.get("last_changed_at"))
    prefix = f"[{changed_at}] " if changed_at else ""
    duration = _format_duration(behavior.get("duration_sec"))
    scene = behavior.get("scene_type") or behavior.get("preset") or "-"
    app = behavior.get("app") or "-"
    window = behavior.get("window") or "-"
    return f"{prefix}{scene} | {app} | {window} | {duration}"


def _format_time(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return ""
    try:
        return datetime.fromtimestamp(value / 1000).strftime("%H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


def _format_duration(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    minutes = int(round(value / 60))
    if minutes < 60:
        return f"{minutes}min"
    hours = minutes // 60
    rest = minutes % 60
    return f"{hours}h{rest:02d}min"


def _extract_usage(result: JsonDict) -> JsonDict:
    token_usage = result.get("token_usage", {})
    usage = token_usage.get("response_planner", token_usage.get("e_express", token_usage))
    prompt = int(usage.get("prompt", usage.get("prompt_tokens", 0)) or 0)
    completion = int(usage.get("completion", usage.get("completion_tokens", 0)) or 0)
    total = int(usage.get("total", usage.get("total_tokens", prompt + completion)) or 0)
    return {"prompt": prompt, "completion": completion, "total": total}


def timing_child_keys(key: str) -> list[str]:
    children = {
        "context_frames": ["turn_interpretation", "memory_recall"],
        "subsystem_signals": [
            "sig_dialogue",
            "sig_behavior",
            "sig_memory",
            "sig_persona",
            "sig_worldops",
            "sig_relationship",
            "sig_origin",
            "sig_initiative",
        ],
    }
    return children.get(key, [])


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
