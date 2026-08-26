from __future__ import annotations

import contextlib
import contextvars
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Iterator, Protocol

from .contracts import CancellationToken, JsonDict, TurnCancelled
from .journal import ConversationJournal


class ModelTask(StrEnum):
    TURN_INTERPRETATION = "turn_interpretation"
    SEMANTIC_ARBITRATION = "semantic_arbitration"
    RESPONSE_GENERATION = "response_generation"
    INTERRUPTIBILITY_RESOLUTION = "interruptibility_resolution"
    DOMAIN_BACKGROUND = "domain_background"


@dataclass(frozen=True)
class ModelRoute:
    name: str
    model: str | None
    thinking_disabled: bool
    temperature: float
    max_tokens: int


@dataclass
class TurnModelBudget:
    turn_id: str
    cancellation: CancellationToken
    correlation_id: str | None = None
    max_logical_calls: int = 3
    calls: list[JsonDict] = field(default_factory=list)

    def reserve(self, task: ModelTask, route: ModelRoute) -> int:
        self.cancellation.raise_if_cancelled()
        if task != ModelTask.RESPONSE_GENERATION and len(self.calls) >= self.max_logical_calls - 1:
            raise ModelBudgetExceeded(
                f"turn {self.turn_id} reserved its final LLM slot for response generation"
            )
        if len(self.calls) >= self.max_logical_calls:
            raise ModelBudgetExceeded(
                f"turn {self.turn_id} exceeded {self.max_logical_calls} logical LLM calls"
            )
        ordinal = len(self.calls) + 1
        self.calls.append(
            {
                "ordinal": ordinal,
                "task": task.value,
                "route": route.name,
                "model": route.model,
                "status": "reserved",
                "thinking_disabled": route.thinking_disabled,
            }
        )
        return ordinal


class ModelBudgetExceeded(RuntimeError):
    pass


class StreamingLLMClient(Protocol):
    def stream_chat(self, system: str, user: str, **kwargs: Any) -> Iterator[JsonDict]:
        ...


class MetricsSink(Protocol):
    def increment(self, name: str, *, labels: JsonDict | None = None, value: int = 1) -> None:
        ...

    def observe(self, name: str, value: float, *, labels: JsonDict | None = None) -> None:
        ...


_ACTIVE_BUDGET: contextvars.ContextVar[TurnModelBudget | None] = contextvars.ContextVar(
    "sora_turn_model_budget", default=None
)


class ModelRouteCoordinator:
    def __init__(
        self,
        *,
        fast_model: str | None = None,
        response_model: str | None = None,
        max_hot_path_calls: int = 3,
    ) -> None:
        self.max_hot_path_calls = max(1, max_hot_path_calls)
        self.routes = {
            ModelTask.TURN_INTERPRETATION: ModelRoute(
                "L0-fast-nonthinking", fast_model, True, 0.0, 900
            ),
            ModelTask.SEMANTIC_ARBITRATION: ModelRoute(
                "L0-fast-nonthinking", fast_model, True, 0.0, 700
            ),
            ModelTask.INTERRUPTIBILITY_RESOLUTION: ModelRoute(
                "L0-fast-nonthinking", fast_model, True, 0.0, 500
            ),
            ModelTask.RESPONSE_GENERATION: ModelRoute(
                "L2-response", response_model, False, 0.7, 800
            ),
            ModelTask.DOMAIN_BACKGROUND: ModelRoute(
                "L3-background", response_model, False, 0.0, 1200
            ),
        }

    @contextlib.contextmanager
    def activate(
        self,
        turn_id: str,
        cancellation: CancellationToken,
        *,
        correlation_id: str | None = None,
    ) -> Iterator[TurnModelBudget]:
        budget = TurnModelBudget(
            turn_id=turn_id,
            cancellation=cancellation,
            correlation_id=correlation_id,
            max_logical_calls=self.max_hot_path_calls,
        )
        token = _ACTIVE_BUDGET.set(budget)
        try:
            yield budget
        finally:
            _ACTIVE_BUDGET.reset(token)

    def route_for(self, task: ModelTask) -> ModelRoute:
        return self.routes[task]


class RoutedLLMClient:
    """Enforces foreground model routes while remaining transparent to background workers."""

    def __init__(
        self,
        delegate: Any,
        coordinator: ModelRouteCoordinator,
        *,
        journal: ConversationJournal | None = None,
        metrics: MetricsSink | None = None,
    ) -> None:
        self.delegate = delegate
        self.coordinator = coordinator
        self.journal = journal
        self.metrics = metrics

    def chat(self, system: str, user: str, **kwargs: Any) -> JsonDict:
        task = self._task_for(kwargs.get("workload"), user)
        budget = _ACTIVE_BUDGET.get()
        route = self.coordinator.route_for(task)
        if budget is None:
            return self.delegate.chat(system=system, user=user, **kwargs)
        ordinal = budget.reserve(task, route)
        call_kwargs = self._apply_route(
            kwargs,
            route,
            task,
            budget.turn_id,
            budget.correlation_id,
        )
        started = time.perf_counter()
        try:
            budget.cancellation.raise_if_cancelled()
            result = self._invoke(
                delegate=self.delegate,
                system=system,
                user=user,
                kwargs=call_kwargs,
                budget=budget,
                task=task,
            )
        except Exception as exc:
            budget.calls[ordinal - 1]["status"] = "failed"
            if self.journal is not None:
                self.journal.record_model_call(
                    budget.turn_id,
                    logical_task=task.value,
                    route=route.name,
                    model=route.model,
                    status="failed",
                    thinking_disabled=route.thinking_disabled,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error_code=type(exc).__name__,
                )
            raise
        latency_ms = (time.perf_counter() - started) * 1000
        budget.calls[ordinal - 1].update(
            {
                "status": "completed",
                "model": result.get("model") or route.model,
                "latency_ms": round(latency_ms, 2),
                "usage": dict(result.get("usage") or {}),
            }
        )
        if self.journal is not None:
            self.journal.record_model_call(
                budget.turn_id,
                logical_task=task.value,
                route=route.name,
                model=str(result.get("model") or route.model or "unknown"),
                status="completed",
                thinking_disabled=route.thinking_disabled,
                usage=dict(result.get("usage") or {}),
                latency_ms=latency_ms,
            )
        return result

    def _invoke(
        self,
        *,
        delegate: Any,
        system: str,
        user: str,
        kwargs: JsonDict,
        budget: TurnModelBudget,
        task: ModelTask,
    ) -> JsonDict:
        stream_chat = getattr(delegate, "stream_chat", None)
        if not callable(stream_chat) or task != ModelTask.RESPONSE_GENERATION:
            return delegate.chat(system=system, user=user, cancellation_token=budget.cancellation, **kwargs)
        chunks: list[str] = []
        usage: JsonDict = {}
        model: str | None = None
        event_count = 0
        stream = stream_chat(
            system=system,
            user=user,
            cancellation_token=budget.cancellation,
            **kwargs,
        )
        try:
            for event in stream:
                budget.cancellation.raise_if_cancelled()
                event_count += 1
                if event.get("delta"):
                    chunks.append(str(event["delta"]))
                if event.get("usage"):
                    usage = dict(event["usage"])
                if event.get("model"):
                    model = str(event["model"])
        except TurnCancelled:
            partial = "".join(chunks)
            if partial and self.journal is not None:
                self.journal.put_artifact(
                    budget.turn_id,
                    "aborted_response",
                    {
                        "text": partial,
                        "source": "provider_buffer",
                        "copy_allowed": False,
                        "validated": False,
                    },
                    schema_version="AbortedConversationResponse.v1",
                )
            if partial and self.metrics is not None:
                labels = {
                    "provider": type(self.delegate).__name__,
                    "capability": "stream_chat",
                }
                self.metrics.increment("runtime.provider.cancel_waste", labels=labels)
                self.metrics.observe(
                    "runtime.provider.cancel_waste_chars", len(partial), labels=labels
                )
            raise
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        return {
            "content": "".join(chunks),
            "usage": usage,
            "model": model or kwargs.get("model"),
            "raw": {"stream_events": event_count},
        }

    @staticmethod
    def _apply_route(
        kwargs: JsonDict,
        route: ModelRoute,
        task: ModelTask,
        turn_id: str,
        correlation_id: str | None,
    ) -> JsonDict:
        routed = dict(kwargs)
        routed["workload"] = task.value
        routed["_sora_turn_id"] = turn_id
        if correlation_id:
            routed["correlation_id"] = correlation_id
        if route.model:
            routed["model"] = route.model
        routed.setdefault("temperature", route.temperature)
        routed.setdefault("max_tokens", route.max_tokens)
        if route.thinking_disabled:
            routed["thinking"] = {"type": "disabled"}
        return routed

    @staticmethod
    def _task_for(explicit_workload: Any, user_payload: str) -> ModelTask:
        if explicit_workload is not None:
            try:
                return ModelTask(str(explicit_workload))
            except ValueError:
                return ModelTask.DOMAIN_BACKGROUND
        return classify_model_task(user_payload)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)


def classify_model_task(user_payload: str) -> ModelTask:
    lowered = user_payload.lower()
    if '"task": "interpret_turn"' in lowered:
        return ModelTask.TURN_INTERPRETATION
    if '"task": "score_candidate_signals"' in lowered:
        return ModelTask.SEMANTIC_ARBITRATION
    if '"task": "resolve_interruptibility"' in lowered:
        return ModelTask.INTERRUPTIBILITY_RESOLUTION
    if "context_pack" in lowered:
        return ModelTask.RESPONSE_GENERATION
    return ModelTask.DOMAIN_BACKGROUND


class AdaptiveTurnInterpreter:
    """Uses deterministic parsing for clear turns and the LLM only for ambiguity."""

    def __init__(self, llm_interpreter: Any, rule_interpreter: Callable[..., JsonDict]) -> None:
        self.llm_interpreter = llm_interpreter
        self.rule_interpreter = rule_interpreter

    def interpret(self, context: JsonDict) -> JsonDict:
        text = str(context.get("user_input") or "")
        recent = list(context.get("recent_turns") or [])
        rule = self.rule_interpreter(
            text,
            recent,
            proactive_trigger=context.get("proactive_trigger"),
        )
        if not _needs_semantic_interpretation(text, rule, recent):
            rule["mode"] = "deterministic_fast_path"
            return rule
        try:
            return self.llm_interpreter.interpret(context)
        except Exception as exc:
            rule["mode"] = "llm_degraded"
            rule["degraded_reason"] = f"{type(exc).__name__}: {exc}"
            return rule


def _needs_semantic_interpretation(text: str, rule: JsonDict, recent: list[JsonDict]) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) <= 2:
        return False
    if rule.get("speech_act") in {"greeting", "control"}:
        return False
    reference = dict(rule.get("context_reference") or {})
    if reference.get("references_previous_context") or reference.get("unresolved_references"):
        return True
    ambiguous_terms = ("这个", "那个", "这样", "那样", "怎么办", "你觉得呢", "接着说")
    if any(term in stripped for term in ambiguous_terms) and recent:
        return True
    return bool(dict(rule.get("intent") or {}).get("confidence", 1.0) < 0.55)
