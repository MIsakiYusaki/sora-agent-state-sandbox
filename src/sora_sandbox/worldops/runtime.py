from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..memory_schema import new_id
from .contracts import (
    EXPRESSION_LEVELS,
    JsonDict,
    UserChoiceInterpretation,
    WorldOpsContractError,
)
from .provider import DeterministicChoiceMatcher, WorldOpsInterpretationProvider
from .store import WorldOpsStore


class PredicateBudgetExceeded(WorldOpsContractError):
    pass


class PredicateEngine:
    """Finite, non-executable DSL evaluator for authored conditions."""

    def __init__(self, *, max_nodes: int = 128, max_depth: int = 12) -> None:
        self.max_nodes = max_nodes
        self.max_depth = max_depth

    def evaluate(self, expression: Mapping[str, Any] | None, context: Mapping[str, Any]) -> bool:
        budget = {"nodes": 0}
        return self._evaluate(dict(expression or {"op": "always"}), context, budget=budget, depth=0)

    def _evaluate(
        self,
        expression: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        budget: JsonDict,
        depth: int,
    ) -> bool:
        budget["nodes"] += 1
        if budget["nodes"] > self.max_nodes or depth > self.max_depth:
            raise PredicateBudgetExceeded("predicate evaluation budget exceeded")
        op = str(expression.get("op") or "")
        if op == "always":
            return True
        if op == "all":
            return all(self._evaluate(item, context, budget=budget, depth=depth + 1) for item in expression.get("items") or [])
        if op == "any":
            return any(self._evaluate(item, context, budget=budget, depth=depth + 1) for item in expression.get("items") or [])
        if op == "not":
            return not self._evaluate(dict(expression.get("item") or {}), context, budget=budget, depth=depth + 1)
        if op == "fact_exists":
            return str(expression.get("key")) in dict(context.get("facts") or {})
        if op == "fact_equals":
            return dict(context.get("facts") or {}).get(str(expression.get("key"))) == expression.get("value")
        if op == "event_state":
            return dict(context.get("event_states") or {}).get(str(expression.get("event_id"))) == expression.get("state")
        if op == "choice_is":
            return context.get("choice_id") == expression.get("choice_id")
        if op == "clock_at_least":
            return float(context.get("world_clock", 0.0)) >= float(expression.get("value", 0.0))
        if op == "evidence_count":
            wanted_type = str(expression.get("evidence_type") or "")
            task_id = str(expression.get("task_id") or context.get("task_id") or "")
            count = sum(
                1
                for item in context.get("task_evidence") or []
                if bool(item.get("accepted"))
                and (not wanted_type or item.get("evidence_type") == wanted_type)
                and (not task_id or item.get("task_id") == task_id)
            )
            return count >= int(expression.get("min", 1))
        raise WorldOpsContractError(f"unsupported predicate opcode: {op}")


@dataclass
class WorldOpsRuntime:
    store: WorldOpsStore
    provider: WorldOpsInterpretationProvider | None = None
    deterministic_matcher: DeterministicChoiceMatcher = field(default_factory=DeterministicChoiceMatcher)
    predicate_engine: PredicateEngine = field(default_factory=PredicateEngine)

    def refresh_eligibility(self, *, correlation_id: str | None = None) -> JsonDict:
        active = self.store.active_bundle()
        if active is None:
            return {"evaluated": 0, "activated": 0, "reason": "no_active_bundle"}
        facts = self.store.active_facts()
        instances = self.store.list_event_instances()
        states = {item["event_id"]: item["state"] for item in instances}
        world_clock = float(self.store.timeline()["world_clock"])
        activated = 0
        failures: list[JsonDict] = []
        for instance in instances:
            if instance["state"] != "dormant":
                continue
            event = self.store.event_definition(
                instance["event_id"], definition_version=int(instance["definition_version"])
            )
            if event is None:
                failures.append({"instance_id": instance["instance_id"], "reason": "definition_missing"})
                continue
            try:
                eligible = self.predicate_engine.evaluate(
                    event.get("eligibility"),
                    {"facts": facts, "event_states": states, "world_clock": world_clock},
                )
            except WorldOpsContractError as exc:
                failures.append({"instance_id": instance["instance_id"], "reason": str(exc)})
                continue
            if eligible:
                self.store.transition_event(
                    instance["instance_id"],
                    to_state="hinted",
                    expected_revision=int(instance["revision"]),
                    trigger_type="eligible",
                    correlation_id=correlation_id,
                )
                activated += 1
        return {"evaluated": len(instances), "activated": activated, "failures": failures}

    def observe_turn(
        self,
        text: str,
        *,
        turn_id: str | int,
        correlation_id: str,
        turn_interpretation: Mapping[str, Any] | None = None,
        interruptibility_frame: Mapping[str, Any] | None = None,
        persona_frame: Mapping[str, Any] | None = None,
        relationship_frame: Mapping[str, Any] | None = None,
    ) -> JsonDict:
        started = time.perf_counter()
        eligibility = self.refresh_eligibility(correlation_id=correlation_id)
        selected = self._select_instance(turn_interpretation=turn_interpretation)
        interpretation_result: JsonDict | None = None
        if selected is not None and text.strip() and self._turn_is_relevant(
            text,
            selected,
            turn_interpretation=turn_interpretation,
        ):
            selected = self._invite_if_needed(selected, correlation_id=correlation_id, turn_id=turn_id)
            interpretation_result = self._interpret_and_apply(
                text,
                input_id=f"turn.{turn_id}",
                instance=selected,
                correlation_id=correlation_id,
                bounded_context={
                    "turn_intent": dict(turn_interpretation or {}).get("intent"),
                    "persona_knowledge_view": dict(persona_frame or {}).get("knowledge_view"),
                    "relationship_repair_state": dict(dict(relationship_frame or {}).get("repair") or {}).get("state"),
                },
            )
            selected = self.store.get_event_instance(selected["instance_id"])
        frame = self.frame(
            interruptibility_frame=interruptibility_frame,
            selected_instance=selected,
            correlation_id=correlation_id,
        )
        latency = round((time.perf_counter() - started) * 1000, 3)
        self.store.record_trace(
            "turn_observed",
            "completed",
            {
                "turn_id": str(turn_id),
                "eligibility": eligibility,
                "selected_instance_id": selected.get("instance_id") if selected else None,
                "interpretation_status": (interpretation_result or {}).get("status"),
                "raw_text_persisted": False,
            },
            latency_ms=latency,
            correlation_id=correlation_id,
            instance_id=selected.get("instance_id") if selected else None,
        )
        self.store.record_metric("worldops_turn_latency_ms", latency, labels={"llm_called": bool((interpretation_result or {}).get("llm_called"))})
        return {
            "frame": frame,
            "eligibility": eligibility,
            "interpretation": interpretation_result,
            "latency_ms": latency,
        }

    def frame(
        self,
        *,
        interruptibility_frame: Mapping[str, Any] | None = None,
        selected_instance: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> JsonDict:
        active_bundle = self.store.active_bundle()
        control = self.store.get_control()
        timeline = self.store.timeline()
        if active_bundle is None:
            return self.safe_frame(reason="no_active_bundle", correlation_id=correlation_id)
        selected = dict(selected_instance or self._select_instance(turn_interpretation=None) or {})
        event = (
            self.store.event_definition(
                str(selected.get("event_id")),
                definition_version=int(selected.get("definition_version", 1)),
            )
            if selected
            else None
        )
        surface = dict(interruptibility_frame or {}).get("state_surface_permission", {}).get("worldops")
        expression_permission = str(control.get("expression_permission") or "implicit")
        if surface == "silent" or expression_permission == "silent" or not control["ambient_enabled"]:
            max_level = "L0"
        else:
            max_level = str((event or {}).get("expression_default") or "L0")
            if max_level not in EXPRESSION_LEVELS:
                max_level = "L0"
        initiative_permission = str(dict(interruptibility_frame or {}).get("initiative_permission") or "none")
        initiative_allowed = (
            bool(event)
            and bool((event or {}).get("active_mention_allowed", False))
            and max_level != "L0"
            and initiative_permission in {"brief", "normal"}
        )
        facts = self.store.active_facts()
        allowed_facts = [str(item) for item in (event or {}).get("allowed_facts") or []]
        blocked_facts = [str(item) for item in (event or {}).get("blocked_facts") or []]
        return {
            "schema_version": "WorldOpsFrame.v1",
            "runtime_mode": "canonical_worldops_system",
            "world_id": self.store.world_id,
            "canon_version": active_bundle["manifest"]["canon_version"],
            "bundle_id": active_bundle["manifest"]["bundle_id"],
            "release_sequence": active_bundle["manifest"]["release_sequence"],
            "timeline_revision": int(timeline["revision"]),
            "world_clock": float(timeline["world_clock"]),
            "selected_event_id": (event or {}).get("event_id"),
            "selected_event": (
                {
                    "instance_id": selected.get("instance_id"),
                    "event_id": (event or {}).get("event_id"),
                    "title": (event or {}).get("title"),
                    "summary": (event or {}).get("summary"),
                    "one_sentence": (event or {}).get("summary"),
                    "state": selected.get("state"),
                    "tone": (event or {}).get("tone"),
                    "definition_version": (event or {}).get("definition_version", 1),
                }
                if event
                else None
            ),
            "candidate_events": [
                {
                    "instance_id": item["instance_id"],
                    "event_id": item["event_id"],
                    "state": item["state"],
                }
                for item in self.store.list_event_instances(states={"hinted", "invited", "active"})[:5]
            ],
            "allowed_facts": allowed_facts,
            "blocked_facts": blocked_facts,
            "personal_facts": facts,
            "action_options": [str(item.get("choice_id")) for item in (event or {}).get("choices") or []],
            "expression": {"max_level": max_level, "initiative_allowed": initiative_allowed},
            "expression_level": max_level,
            "expression_hint": "micro_quest_invite" if max_level == "L3" else ("bubble" if max_level in {"L1", "L2"} else "text"),
            "memory_policy": dict((event or {}).get("memory_policy") or {"write_only_if_user_participates": True}),
            "contribution_policy": dict((event or {}).get("contribution_policy") or {}),
            "state_surface_permission": expression_permission,
            "forbidden_behaviors": [
                "Do not invent unpublished world facts or event outcomes.",
                "Do not pressure the user to participate or punish absence, refusal, or delay.",
                "Do not turn an unaccepted or ambiguous choice into a shared experience.",
                "Do not use relationship intensity to increase initiative or disclosure.",
            ],
            "influence_summary": (
                [f"WorldOps {event['event_id']} is {selected['state']}: {event['summary']}"] if event else []
            ),
            "memory_refs": [item["source_memory_id"] for item in self.store.list_memory_projections(active=True)[:10]],
            "provenance": {
                "bundle": active_bundle["manifest"]["bundle_id"],
                "canon_version": active_bundle["manifest"]["canon_version"],
                "event_definition_version": (event or {}).get("definition_version"),
                "timeline_revision": int(timeline["revision"]),
                "correlation_id": correlation_id,
            },
            "degraded_reasons": [],
        }

    def safe_frame(self, *, reason: str, correlation_id: str | None = None) -> JsonDict:
        return {
            "schema_version": "WorldOpsFrame.v1",
            "runtime_mode": "neutral_safe_fallback",
            "world_id": self.store.world_id,
            "canon_version": None,
            "timeline_revision": int(self.store.timeline()["revision"]),
            "selected_event_id": None,
            "selected_event": None,
            "candidate_events": [],
            "allowed_facts": [],
            "blocked_facts": ["all_unverified_world_facts"],
            "action_options": [],
            "expression": {"max_level": "L0", "initiative_allowed": False},
            "expression_level": "L0",
            "expression_hint": "text",
            "memory_policy": {"write_only_if_user_participates": True},
            "forbidden_behaviors": ["Do not invent WorldOps content."],
            "influence_summary": [],
            "memory_refs": [],
            "provenance": {"correlation_id": correlation_id},
            "degraded_reasons": [reason],
        }

    def invite(self, event_id: str, *, correlation_id: str | None = None) -> JsonDict:
        instance = self.store.event_instance_for(event_id)
        if instance is None:
            raise KeyError(event_id)
        if instance["state"] == "dormant":
            result = self.store.transition_event(
                instance["instance_id"],
                to_state="hinted",
                expected_revision=int(instance["revision"]),
                trigger_type="manual_eligibility",
                correlation_id=correlation_id,
            )
            instance = result["event"]
        if instance["state"] == "hinted":
            return self.store.transition_event(
                instance["instance_id"],
                to_state="invited",
                expected_revision=int(instance["revision"]),
                trigger_type="surface_selected",
                correlation_id=correlation_id,
            )
        return {"status": "unchanged", "event": instance}

    def complete_task_manually(
        self,
        event_id: str,
        *,
        choice_text: str,
        correlation_id: str | None = None,
    ) -> JsonDict:
        instance = self.store.event_instance_for(event_id)
        if instance is None:
            raise KeyError(event_id)
        if instance["state"] in {"dormant", "hinted"}:
            instance = self.invite(event_id, correlation_id=correlation_id)["event"]
        result = self._interpret_and_apply(
            choice_text,
            input_id=new_id("admin-input"),
            instance=instance,
            correlation_id=correlation_id or new_id("corr"),
            bounded_context={"admin_compatibility": True},
            force_semantic_evidence=True,
        )
        return result

    def _interpret_and_apply(
        self,
        text: str,
        *,
        input_id: str,
        instance: Mapping[str, Any],
        correlation_id: str,
        bounded_context: Mapping[str, Any],
        force_semantic_evidence: bool = False,
    ) -> JsonDict:
        event = self.store.event_definition(
            str(instance["event_id"]), definition_version=int(instance["definition_version"])
        )
        if event is None:
            return {"status": "definition_missing", "llm_called": False}
        interpretation, trace = self.deterministic_matcher.interpret(
            text,
            input_id=input_id,
            instance=instance,
            event=event,
            context=bounded_context,
        )
        llm_called = False
        if interpretation is None and self.provider is not None:
            llm_called = True
            interpretation, trace = self.provider.interpret(
                text,
                input_id=input_id,
                instance=instance,
                event=event,
                context=bounded_context,
            )
        if interpretation is None:
            self.store.record_trace(
                "choice_interpretation",
                str(trace.get("status") or "no_match"),
                trace,
                correlation_id=correlation_id,
                instance_id=str(instance["instance_id"]),
                latency_ms=float(trace.get("latency_ms", 0.0)),
            )
            return {"status": str(trace.get("status") or "no_match"), "llm_called": llm_called, "trace": trace}
        allowed_choices = {str(item.get("choice_id")) for item in event.get("choices") or []}
        allowed_tasks = {str(item.get("task_id")) for item in event.get("tasks") or []}
        interpretation.validate(allowed_choice_ids=allowed_choices, allowed_task_ids=allowed_tasks)
        if interpretation.ambiguous or interpretation.confidence < 0.75:
            self.store.record_choice(interpretation.to_dict(), status="needs_clarification")
            return {
                "status": "needs_clarification",
                "clarification": interpretation.clarification,
                "interpretation": interpretation.to_dict(),
                "llm_called": llm_called,
            }
        recorded = self.store.record_choice(interpretation.to_dict(), status="accepted")
        if recorded["status"] == "duplicate":
            return {
                "status": "duplicate",
                "interpretation": interpretation.to_dict(),
                "llm_called": llm_called,
                "trace": trace,
            }
        current = self.store.get_event_instance(str(instance["instance_id"]))
        transition = self._transition_for_choice(event, current, interpretation.choice_id)
        if transition is not None:
            result = self.store.transition_event(
                current["instance_id"],
                to_state=str(transition["to"]),
                expected_revision=int(current["revision"]),
                trigger_type="choice",
                trigger_ref=input_id,
                choice_id=interpretation.choice_id,
                effects=[dict(item) for item in transition.get("effects") or []],
                correlation_id=correlation_id,
                causation_id=input_id,
            )
            current = result["event"]
        for evidence in interpretation.task_evidence:
            self.store.add_task_evidence(
                instance_id=current["instance_id"],
                task_id=str(evidence["task_id"]),
                input_id=input_id,
                evidence_type=str(evidence.get("evidence_type") or "semantic_claim"),
                payload={"claim": evidence.get("claim"), "evidence_refs": evidence.get("evidence_refs") or [input_id]},
                confidence=float(evidence.get("confidence", interpretation.confidence)),
                accepted=float(evidence.get("confidence", 0.0)) >= 0.75 or force_semantic_evidence,
                correlation_id=correlation_id,
            )
        completion = self._complete_if_ready(event, current, correlation_id=correlation_id, causation_id=input_id)
        return {
            "status": "applied",
            "interpretation": interpretation.to_dict(),
            "transition": transition,
            "completion": completion,
            "llm_called": llm_called,
            "trace": trace,
        }

    def _complete_if_ready(
        self,
        event: Mapping[str, Any],
        instance: Mapping[str, Any],
        *,
        correlation_id: str,
        causation_id: str,
    ) -> JsonDict | None:
        current = self.store.get_event_instance(str(instance["instance_id"]))
        if current["state"] != "active" or not event.get("tasks"):
            return None
        evidence = self.store.list_task_evidence(current["instance_id"])
        all_complete = True
        for task in event.get("tasks") or []:
            expression = dict(task.get("completion") or {})
            expression.setdefault("task_id", task["task_id"])
            if not self.predicate_engine.evaluate(
                expression,
                {"task_id": task["task_id"], "task_evidence": evidence},
            ):
                all_complete = False
                break
        if not all_complete:
            return None
        transition = next(
            (
                item
                for item in event.get("transitions") or []
                if item.get("from") == "active" and item.get("trigger") == "tasks_complete"
            ),
            {"to": "completed", "effects": []},
        )
        return self.store.transition_event(
            current["instance_id"],
            to_state=str(transition.get("to") or "completed"),
            expected_revision=int(current["revision"]),
            trigger_type="tasks_complete",
            trigger_ref=causation_id,
            effects=[dict(item) for item in transition.get("effects") or []],
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    def _select_instance(self, *, turn_interpretation: Mapping[str, Any] | None) -> JsonDict | None:
        instances = self.store.list_event_instances(states={"hinted", "invited", "active"})
        target_ids = {
            str(item.get("event_id"))
            for item in dict(turn_interpretation or {}).get("context_reference", {}).get("resolved_targets", []) or []
            if item.get("event_id")
        }
        if target_ids:
            matched = next((item for item in instances if item["event_id"] in target_ids), None)
            if matched:
                return matched
        order = {"active": 0, "invited": 1, "hinted": 2}
        return sorted(instances, key=lambda item: (order.get(item["state"], 9), item["updated_at"], item["instance_id"]))[0] if instances else None

    def _turn_is_relevant(
        self,
        text: str,
        instance: Mapping[str, Any],
        *,
        turn_interpretation: Mapping[str, Any] | None,
    ) -> bool:
        if instance["state"] in {"invited", "active"}:
            return True
        event = self.store.event_definition(str(instance["event_id"])) or {}
        lowered = text.casefold()
        if str(event.get("title") or "").casefold() in lowered:
            return True
        for target in dict(turn_interpretation or {}).get("context_reference", {}).get("resolved_targets", []) or []:
            if target.get("event_id") == instance["event_id"] or target.get("target_type") == "active_worldops_event":
                return True
        return False

    def _invite_if_needed(self, instance: JsonDict, *, correlation_id: str, turn_id: str | int) -> JsonDict:
        if instance["state"] != "hinted":
            return instance
        result = self.store.transition_event(
            instance["instance_id"],
            to_state="invited",
            expected_revision=int(instance["revision"]),
            trigger_type="user_reference",
            trigger_ref=f"turn.{turn_id}",
            correlation_id=correlation_id,
        )
        return result["event"]

    @staticmethod
    def _transition_for_choice(
        event: Mapping[str, Any],
        instance: Mapping[str, Any],
        choice_id: str | None,
    ) -> JsonDict | None:
        if choice_id is None:
            return None
        for transition in event.get("transitions") or []:
            if transition.get("from") != instance.get("state") or transition.get("trigger") != "choice":
                continue
            choices = {str(item) for item in transition.get("choice_ids") or []}
            if not choices or choice_id in choices:
                return dict(transition)
        return None
