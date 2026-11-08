"""Content-free execution evidence. Never record call arguments or return values."""
from __future__ import annotations

import contextlib
import contextvars
import copy
import math
import re
import threading
import time
from typing import Any, Callable

_CURRENT = contextvars.ContextVar("sora_execution_evidence", default=None)
_PARENT = contextvars.ContextVar("sora_execution_parent", default=None)


class TurnEvidence:
    def __init__(self, *, lane: str = "foreground", limit: int = 512) -> None:
        self.anchor_at_ms = time.time_ns() / 1_000_000
        self.started = time.perf_counter()
        self.lane = lane
        self.limit = limit
        self.spans: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.pre_generation: dict[str, Any] | None = None
        # Private reference-only basis, separate from content-free diagnostic snapshots.
        self._response_basis: dict[str, Any] | None = None
        self.dropped = 0
        self._lock = threading.RLock()

    @contextlib.contextmanager
    def activate(self, parent=None):
        token = _CURRENT.set(self)
        parent_token = _PARENT.set(parent)
        try:
            yield self
        finally:
            _PARENT.reset(parent_token)
            _CURRENT.reset(token)

    @contextlib.contextmanager
    def span(self, name: str, *, link_children: bool = True):
        started = time.perf_counter()
        with self._lock:
            if len(self.spans) >= self.limit:
                self.dropped += 1
                item = None
            else:
                item = {
                    "span_id": f"span_{len(self.spans) + 1}", "parent_id": _PARENT.get(),
                    "name": name, "lane": self.lane, "status": "running",
                    "started_at_ms": time.time_ns() / 1_000_000,
                    "start_offset_ms": (started - self.started) * 1000,
                    "ended_at_ms": None, "duration_ms": None,
                }
                self.spans.append(item)
        token = _PARENT.set(item["span_id"] if item else _PARENT.get()) if link_children else None
        try:
            yield
        except BaseException as exc:
            if item is not None:
                with self._lock:
                    item["status"] = ("interrupted" if isinstance(exc, GeneratorExit) else
                                      "cancelled" if type(exc).__name__ == "TurnCancelled" else "failed")
            raise
        finally:
            if token is not None:
                _PARENT.reset(token)
            if item is not None:
                with self._lock:
                    item["ended_at_ms"] = time.time_ns() / 1_000_000
                    item["duration_ms"] = (time.perf_counter() - started) * 1000
                    if item["status"] == "running":
                        item["status"] = "completed"

    def event(self, name: str) -> None:
        # Names are source-code constants; legacy payloads can contain private text.
        with self._lock:
            if len(self.events) >= self.limit:
                self.dropped += 1
                return
            self.events.append({"name": name, "at_ms": time.time_ns() / 1_000_000,
                                "offset_ms": (time.perf_counter() - self.started) * 1000})

    def snapshot(self, *, final: bool = False) -> dict[str, Any]:
        with self._lock:
            spans = copy.deepcopy(self.spans)
            if final:
                # Timed-out signal workers can outlive their foreground parent.
                # A persisted snapshot cannot assert that they are still running.
                for span in spans:
                    if span["status"] == "running":
                        span["status"] = "end_unobserved"
            return {"schema_version": "TurnExecutionEvidence.v1", "clock": "monotonic_duration_with_wall_anchor",
                    "snapshot_state": "final" if final else "live",
                    "anchor_at_ms": self.anchor_at_ms, "lane": self.lane,
                    "spans": spans, "events": copy.deepcopy(self.events),
                    "pre_generation": copy.deepcopy(self.pre_generation),
                    "dropped_count": self.dropped}

    def capture_decisions(self, context_pack: dict[str, Any]) -> None:
        # Capture only the existing L0 allowlist, never the original prompt
        # frame. The generation packer intentionally drops diagnostic fields.
        projected = legacy_evidence({"context_pack": context_pack})
        with self._lock:
            self.pre_generation = {**(self.pre_generation or {}), **{key: projected[key] for key in
                                   ("signals", "signal_decisions", "decisions")}}

    def capture_frames(self, context_pack: dict[str, Any]) -> None:
        from .conversation_runtime.debug_frames import project_generation
        projected = project_generation(context_pack, captured_at_ms=time.time_ns() // 1_000_000)
        with self._lock:
            self.pre_generation = {**(self.pre_generation or {}), "frame_projection": projected}

    def capture_response_basis(self, context: dict[str, Any]) -> None:
        recall = context.get("recollection_frame") or {}
        items = [*(recall.get("items") or []), *(recall.get("memory_summaries") or [])]
        references, unidentified = [], 0
        for item in items:
            identifier = (item.get("memory_id") or item.get("id")) if isinstance(item, dict) else None
            if isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", identifier):
                if identifier not in references:
                    references.append(identifier)
            else:
                unidentified += 1
        recent = (context.get("session_context") or {}).get("recent_turns")
        with self._lock:
            self._response_basis = {"schema_version": "UserResponseBasisReferences.v1", "source": "generation_sealed",
                "memory_ids": references[:100], "unidentified_memory_count": unidentified + max(0, len(references) - 100),
                "recent_message_count": len(recent) if isinstance(recent, list) else None}

    def response_basis(self) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._response_basis)


def observe_call(name: str, function: Callable, /, *args, **kwargs):
    current = _CURRENT.get()
    if current is None:
        return function(*args, **kwargs)
    with current.span(name):
        return function(*args, **kwargs)


def observe_http(name: str):
    """Measure one transport request lifetime, not wire-only latency or TTFT.

    Names are source constants only. A stream can yield back into another
    collector/context: do not hold a ContextVar token across those yields or
    incorrectly attach consumer work as a child of its HTTP request.
    """
    current = _CURRENT.get()
    return current.span(name, link_children=False) if current is not None else contextlib.nullcontext()


def observe_event(name: str) -> None:
    current = _CURRENT.get()
    if current is not None:
        current.event(name)


def observe_decisions(context_pack: dict[str, Any]) -> None:
    current = _CURRENT.get()
    if current is not None:
        current.capture_decisions(context_pack)


def observe_generation_frames(context_pack: dict[str, Any]) -> None:
    current = _CURRENT.get()
    if current is not None:
        try:
            current.capture_frames(context_pack)
        except Exception:
            # Failure of diagnostic projection must not change generation,
            # retries, disclosure, state or the turn's success status.
            pass


def observe_response_basis(context_pack: dict[str, Any]) -> None:
    current = _CURRENT.get()
    if current is not None:
        try:
            current.capture_response_basis(context_pack)
        except Exception:
            # Basis collection is observational, not a generation dependency.
            pass


def capture_context():
    return _CURRENT.get(), _PARENT.get()


def observed_worker(context, name, function, *args):
    collector, parent = context
    if collector is None:
        return function(*args)
    with collector.activate(parent):
        return observe_call(name, function, *args)


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def legacy_evidence(result: dict[str, Any] | None) -> dict[str, Any]:
    """Strict L0 allowlist: no input, reply, prompt, evidence IDs, or free-text reasons."""
    if not result:
        return {"status": "unobserved", "durations": [], "milestones": [], "signals": [], "decisions": {}}
    pack = result.get("context_pack") or {}
    timings = result.get("timing") or {}
    allowed = {"_total", "context_frames", "turn_interpretation", "interruptibility", "relationship", "affect",
               "worldops", "memory_recall", "event_bus", "subsystem_signals", "initiative_window", "arbitration",
               "response_planner", "expression_router", "state_commit"}
    domains = {"dialogue", "behavior", "memory", "persona", "origin", "affect", "relationship", "worldops", "initiative"}
    allowed |= {f"sig_{domain}" for domain in domains}
    milestones = {"user.input.received", "behavior.frame.provided", "turn.interpretation.provided", "turn.frame.built",
                  "context.use_policy.built", "interruptibility.frame.provided", "relationship.frame.provided",
                  "session.context.updated", "origin.frame.provided", "persona.frame.provided", "affect.frame.provided",
                  "memory.recollection.provided", "worldops.frame.provided", "subsystems.signals.collected",
                  "arbiter.result.provided", "context.pack.transformed", "llm.response.received", "expression.routed",
                  "interruptibility.enforced"}
    durations = [{"name": name, "duration_ms": _number(timings[name]),
                  "measurement": "aggregate_non_contiguous" if name == "subsystem_signals" else "duration_only"}
                 for name in sorted(allowed & timings.keys())]
    interpretation = pack.get("turn_interpretation") or {}
    reference = interpretation.get("context_reference") or {}
    recall = pack.get("recollection_frame") or {}
    arbiter = pack.get("arbitration_result") or {}
    def count(mapping, key):
        value = mapping.get(key)
        return len(value) if isinstance(value, list) else None
    def enum(value, choices):
        return value if isinstance(value, str) and value in choices else None
    reason_codes = {
        "active_visibility": "active_visibility",
        "background_visibility": "background_visibility",
        "initiative_allowed_semantic_pass": "initiative_allowed_semantic_pass",
        "semantic_judge_promoted": "semantic_judge_promoted",
        "semantic_judge_background": "semantic_judge_background",
        "interruptibility_initiative_blocked": "interruptibility_initiative_blocked",
        "interruptibility_expansion_blocked": "interruptibility_expansion_blocked",
        "用户只是问候，不主动插入 WorldOps。": "greeting_no_worldops",
        "本轮可以回答 Sora 世界设定，但未明确命中当前 WorldOps 事件，事件保持背景。": "world_lore_not_event",
        "当前不是非工作陪伴场景，WorldOps 保持静默或背景。": "worldops_scene_restricted",
        "优先级或可见性不足，未进入本轮表达。": "priority_or_visibility",
        **{f"interruptibility_surface_silent:{source}": "surface_silent"
           for source in ("memory", "relationship", "worldops", "affect")},
    }
    return {
        "status": "available" if durations or result.get("trace") else "unobserved",
        "durations": durations,
        "milestones": [{"name": item["kind"], "at_ms": _number(item.get("at"))}
                       for item in result.get("trace") or [] if isinstance(item, dict) and item.get("kind") in milestones],
        "signals": [{"subsystem": item["subsystem"],
                     "status": enum(item.get("status"), {"ok", "timeout", "degraded", "failed"}),
                     "latency_ms": _number(item.get("latency_ms")), "signal_count": _number(item.get("signal_count"))}
                    for item in pack.get("subsystem_health") or [] if isinstance(item, dict) and item.get("subsystem") in domains],
        "signal_decisions": [{"source": item["source"],
                              "decision": enum(item.get("decision"), {"selected", "background", "suppressed"}),
                              "reason_code": reason_codes.get(item.get("reason")) if isinstance(item.get("reason"), str) else None}
                             for item in arbiter.get("score_breakdown") or []
                             if isinstance(item, dict) and item.get("source") in domains],
        "decisions": {
            "interpretation_mode": enum(interpretation.get("mode"), {"llm", "rule", "rules", "deterministic", "deterministic_fast_path", "llm_degraded", "interpreter_degraded", "fallback", "skipped"}),
            "references_previous_context": reference.get("references_previous_context") if isinstance(reference.get("references_previous_context"), bool) else None,
            "resolved_target_count": count(reference, "resolved_targets"),
            "unresolved_reference_count": count(reference, "unresolved_references"),
            "recalled_item_count": count(recall, "retrieved_ids"),
            "memory_surface": enum(recall.get("surface_action"), {"none", "implicit", "explicit", "gist", "ask_consent", "suppress", "hint", "mention"}),
            "recall_degraded": recall.get("degraded") if isinstance(recall.get("degraded"), bool) else None,
            "selected_signal_count": count(arbiter, "selected_signals"),
            "background_signal_count": count(arbiter, "background_signals"),
            "suppressed_signal_count": count(arbiter, "suppressed_signals"),
            "semantic_judgment_mode": enum((arbiter.get("semantic_judgment") or {}).get("mode"), {"llm", "llm_degraded", "skipped", "fallback", "degraded", "rules"}),
            "question_budget": _number((pack.get("dialogue_control_frame") or {}).get("question_budget")),
            "worldops_expression": enum((arbiter.get("response_directives") or {}).get("worldops_expression"), {"none", "active", "initiative_candidate", "background", "implicit", "explicit", "brief", "full"}),
            **{key: value if isinstance(value := (result.get("state_diff") or {}).get(key), bool) else None
               for key in ("worldops_event_expressed", "growth_event_created", "memory_hook_created", "initiative_allowed")},
        },
    }
