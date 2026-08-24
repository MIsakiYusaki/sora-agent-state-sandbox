from __future__ import annotations

import copy
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ..paths import workspace_state_dir
from .adapters import ConversationPlannerAdapter, RelationshipOutcomeBuilder
from .context import (
    GENERATION_CONTEXT_MARKER,
    ContextBudget,
    ConversationContextPacker,
    generation_context_hash,
)
from .contracts import (
    CancellationToken,
    ClientEvent,
    JsonDict,
    TurnCancelled,
    TurnKind,
    TurnStatus,
)
from .coordinator import TurnCoordinator
from .journal import ConversationJournal, JournalConflict, now_ms
from .observability import RuntimeMetrics
from .eval import validate_golden_set
from .post_turn import (
    AffectPostTurnConsumer,
    MemoryPostTurnConsumer,
    PostTurnDispatcher,
    RelationshipPostTurnConsumer,
    WorldOpsPostTurnConsumer,
)
from .routing import AdaptiveTurnInterpreter, ModelRouteCoordinator, RoutedLLMClient
from .security import AtRestCipher, LocalProtectedKeyProvider


@dataclass(frozen=True)
class ConversationRuntimeConfig:
    max_workers: int = 4
    lease_ttl_ms: int = 120_000
    max_hot_path_llm_calls: int = 3
    client_delta_chars: int = 48
    turn_deadline_ms: int | None = None
    raw_artifact_retention_days: int = 7
    client_event_retention_days: int = 30
    delivered_outbox_retention_days: int = 30
    dead_letter_retention_days: int = 90
    start_post_turn_worker: bool = True
    context_budget: ContextBudget = ContextBudget()

    def __post_init__(self) -> None:
        if self.max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if self.max_hot_path_llm_calls < 1:
            raise ValueError("max_hot_path_llm_calls must be at least 1")
        if self.client_delta_chars < 1:
            raise ValueError("client_delta_chars must be at least 1")
        retention = {
            "raw_artifact_retention_days": self.raw_artifact_retention_days,
            "client_event_retention_days": self.client_event_retention_days,
            "delivered_outbox_retention_days": self.delivered_outbox_retention_days,
            "dead_letter_retention_days": self.dead_letter_retention_days,
        }
        invalid = [name for name, value in retention.items() if value < 1]
        if invalid:
            raise ValueError(f"retention days must be positive: {', '.join(invalid)}")


class ConversationRuntime:
    """Production execution boundary for the text input to text output chain."""

    def __init__(
        self,
        runner: ConversationPlannerAdapter,
        *,
        journal: ConversationJournal,
        model_routes: ModelRouteCoordinator,
        post_turn: PostTurnDispatcher | None = None,
        config: ConversationRuntimeConfig | None = None,
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        self.runner = runner
        self.journal = journal
        self.config = config or ConversationRuntimeConfig()
        self.model_routes = model_routes
        self.post_turn = post_turn or PostTurnDispatcher(journal)
        self.metrics = metrics or RuntimeMetrics()
        self.post_turn.metrics = self.metrics
        self.context_packer = ConversationContextPacker(self.config.context_budget)
        self.coordinator = TurnCoordinator(
            journal,
            max_workers=self.config.max_workers,
            lease_ttl_ms=self.config.lease_ttl_ms,
        )
        self._state_lock = threading.RLock()
        if hasattr(runner, "defer_domain_observation"):
            runner.defer_domain_observation = True
        if hasattr(runner, "defer_post_turn_writes"):
            runner.defer_post_turn_writes = True
        if not hasattr(runner, "context_pack_transformer"):
            raise TypeError(
                "conversation runner must expose context_pack_transformer so the "
                "generation disclosure gate cannot be bypassed"
            )
        runner.context_pack_transformer = self.context_packer.pack_for_generation
        if hasattr(runner, "response_max_tokens"):
            runner.response_max_tokens = self.config.context_budget.response_reserve
        routed_llm = getattr(runner, "llm", None)
        if isinstance(routed_llm, RoutedLLMClient):
            routed_llm.metrics = self.metrics
        if self.config.start_post_turn_worker:
            self.post_turn.start()

    @classmethod
    def build(
        cls,
        runner: ConversationPlannerAdapter,
        *,
        llm: Any,
        memory_runtime: Any | None = None,
        affect_runtime: Any | None = None,
        relationship_runtime: Any | None = None,
        worldops_runtime: Any | None = None,
        relationship_outcome_builder: RelationshipOutcomeBuilder | None = None,
        state_dir: Path | None = None,
        config: ConversationRuntimeConfig | None = None,
    ) -> "ConversationRuntime":
        directory = Path(state_dir or workspace_state_dir())
        key_provider = LocalProtectedKeyProvider(directory / "conversation_runtime.key")
        journal = ConversationJournal(
            directory / "conversation_runtime.db", AtRestCipher(key_provider)
        )
        routes = ModelRouteCoordinator(
            fast_model=os.environ.get("SORA_FAST_MODEL") or None,
            response_model=os.environ.get("OPENAI_MODEL") or None,
            max_hot_path_calls=(config or ConversationRuntimeConfig()).max_hot_path_llm_calls,
        )
        routed_llm = llm if isinstance(llm, RoutedLLMClient) else RoutedLLMClient(
            llm, routes, journal=journal
        )
        runner.llm = routed_llm
        from ..agent import LLMTurnInterpreter, rule_turn_interpretation

        runner.turn_interpreter = AdaptiveTurnInterpreter(
            LLMTurnInterpreter(routed_llm), rule_turn_interpretation
        )
        provider_owners = [
            getattr(runner, "interruptibility", None),
            getattr(runner, "persona_runtime", None),
            affect_runtime,
            relationship_runtime,
            worldops_runtime,
        ]
        for owner in provider_owners:
            provider = getattr(owner, "provider", None)
            if provider is not None and hasattr(provider, "llm"):
                provider.llm = routed_llm
        post_turn = PostTurnDispatcher(journal)
        if memory_runtime is not None:
            post_turn.register(MemoryPostTurnConsumer(memory_runtime))
        if affect_runtime is not None:
            post_turn.register(AffectPostTurnConsumer(affect_runtime))
        if relationship_runtime is not None:
            post_turn.register(
                RelationshipPostTurnConsumer(
                    relationship_runtime,
                    outcome_builder=relationship_outcome_builder,
                )
            )
        if worldops_runtime is not None:
            post_turn.register(WorldOpsPostTurnConsumer(worldops_runtime))
        return cls(
            runner,
            journal=journal,
            model_routes=routes,
            post_turn=post_turn,
            config=config,
        )

    @property
    def llm(self) -> Any:
        return self.runner.llm

    def close(self) -> None:
        self.post_turn.close()
        self.coordinator.close()
        registry = getattr(self.runner, "subsystem_registry", None)
        close_registry = getattr(registry, "close", None)
        if callable(close_registry):
            close_registry()
        self.journal.close()

    def reset(self) -> None:
        self.journal.reset()

    def register_quality_ops(self, memory_runtime: Any) -> None:
        memory_runtime.register_quality_eval_suite(
            "conversation_runtime",
            self.quality_eval,
            source_module="text_conversation_runtime",
        )
        memory_runtime.register_quality_export_section(
            "conversation_runtime",
            self.export_quality_report,
        )

    def run(
        self,
        state: JsonDict,
        user_input: str,
        *,
        request_id: str | None = None,
        conversation_id: str | None = None,
        branch_id: str = "main",
        correlation_id: str | None = None,
    ) -> JsonDict:
        context, created = self._accept(
            state,
            user_input=user_input,
            request_id=request_id,
            conversation_id=conversation_id,
            branch_id=branch_id,
            turn_kind=TurnKind.REACTIVE,
            correlation_id=correlation_id,
        )
        if not created:
            replay = self.journal.read_result(context.turn_id)
            if replay is not None:
                replay = dict(replay)
                replay.setdefault("runtime", {})["idempotent_replay"] = True
                return replay
            raise JournalConflict("request is already in progress")
        return self.coordinator.run(
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            operation=lambda cancellation: self._execute(
                state,
                context.turn_id,
                user_input=user_input,
                proactive_trigger=None,
                cancellation=cancellation,
                sink=None,
            ),
        )

    def run_proactive(
        self,
        state: JsonDict,
        trigger: JsonDict,
        *,
        request_id: str | None = None,
        conversation_id: str | None = None,
        branch_id: str = "main",
    ) -> JsonDict:
        raise NotImplementedError(
            "proactive conversation turns are reserved for a later runtime release"
        )

    def stream_turn(
        self,
        state: JsonDict,
        user_input: str,
        *,
        request_id: str | None = None,
        conversation_id: str | None = None,
        branch_id: str = "main",
        after_sequence: int = 0,
        correlation_id: str | None = None,
    ) -> Iterator[ClientEvent]:
        context, created = self._accept(
            state,
            user_input=user_input,
            request_id=request_id,
            conversation_id=conversation_id,
            branch_id=branch_id,
            turn_kind=TurnKind.REACTIVE,
            correlation_id=correlation_id,
        )
        if not created:
            yield from self.journal.client_events(
                context.turn_id, after_sequence=after_sequence
            )
            return
        events: queue.Queue[ClientEvent | BaseException | None] = queue.Queue()

        def sink(event: ClientEvent) -> None:
            events.put(event)

        future = self.coordinator.submit(
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            operation=lambda cancellation: self._execute(
                state,
                context.turn_id,
                user_input=user_input,
                proactive_trigger=None,
                cancellation=cancellation,
                sink=sink,
            ),
        )
        future.add_done_callback(
            lambda completed: events.put(completed.exception() if completed.exception() else None)
        )
        while True:
            item = events.get()
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            if item.sequence > after_sequence:
                yield item

    def reconnect(self, turn_id: str, *, after_sequence: int = 0) -> list[ClientEvent]:
        self._repair_terminal_client_event(turn_id)
        return self.journal.client_events(turn_id, after_sequence=after_sequence)

    def history_page(
        self,
        conversation_id: str,
        *,
        limit: int = 40,
        before_sequence: int | None = None,
    ) -> list[JsonDict]:
        return self.journal.history_page(
            conversation_id,
            limit=limit,
            before_sequence=before_sequence,
        )

    def conversation_version(self, conversation_id: str) -> int:
        return self.journal.conversation_version(conversation_id)

    def turn_status(self, turn_id: str):
        return self.journal.get_turn(turn_id)

    def aborted_response(self, turn_id: str) -> str | None:
        """Returns only validated partial output that the client had already seen."""
        artifacts = self.journal.artifacts(turn_id, "aborted_response")
        for artifact in reversed(artifacts):
            payload = dict(artifact.get("payload") or {})
            if payload.get("copy_allowed") and payload.get("validated"):
                return str(payload.get("text") or "")
        return None

    def cancel(self, conversation_id: str, *, reason: str = "user_cancelled") -> bool:
        return self.coordinator.cancel(conversation_id, reason=reason)

    def recover(self, *, stale_after_ms: int = 300_000) -> JsonDict:
        stale = self.journal.recover_incomplete(
            stale_before_ms=now_ms() - max(1_000, stale_after_ms)
        )
        recovered = []
        for context in stale:
            try:
                self.journal.transition(
                    context.turn_id,
                    TurnStatus.FAILED,
                    stage="startup_recovery",
                    error_code="STALE_INCOMPLETE_TURN",
                    error_detail="Turn was incomplete when the runtime recovered.",
                )
                recovered.append(context.turn_id)
            except RuntimeError:
                continue
        return {"status": "ok", "recovered_turn_ids": recovered}

    def quality_eval(self) -> JsonDict:
        started = time.perf_counter()
        health = self.health()
        golden = validate_golden_set(
            Path(__file__).resolve().parent / "eval_data" / "golden_v1.jsonl"
        )
        checks = {
            "database_wal": health["database"]["journal_mode"] == "wal",
            "foreign_keys": bool(health["database"]["foreign_keys"]),
            "schema_version": health["database"]["schema_version"] >= 1,
            "bounded_model_calls": self.model_routes.max_hot_path_calls <= 3,
            "post_turn_worker": health["post_turn_worker"] in {"running", "configured"},
            "golden_set_complete": golden["status"] == "passed",
        }
        failed = [name for name, passed in checks.items() if not passed]
        return {
            "status": "passed" if not failed else "failed",
            "total_cases": len(checks),
            "passed_cases": len(checks) - len(failed),
            "failed_cases": len(failed),
            "summary": {
                "checks": checks,
                "failed": failed,
                "golden_set": golden,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        }

    def export_quality_report(self) -> JsonDict:
        return {
            "schema_version": "ConversationRuntimeQualityReport.v1",
            "health": self.health(),
            "metrics": self.metrics.export(),
            "eval": self.quality_eval(),
        }

    def maintain(self, *, at_ms: int | None = None) -> JsonDict:
        current = at_ms or now_ms()
        day_ms = 24 * 60 * 60 * 1000
        return self.journal.purge_expired_content(
            raw_content_before_ms=current - self.config.raw_artifact_retention_days * day_ms,
            client_events_before_ms=current - self.config.client_event_retention_days * day_ms,
            delivered_outbox_before_ms=(
                current - self.config.delivered_outbox_retention_days * day_ms
            ),
            dead_letter_before_ms=current - self.config.dead_letter_retention_days * day_ms,
        )

    def health(self) -> JsonDict:
        database = self.journal.health()
        return {
            "status": "ok",
            "database": database,
            "post_turn_worker": "running" if self.post_turn.running else "configured",
            "max_hot_path_llm_calls": self.model_routes.max_hot_path_calls,
        }

    def _accept(
        self,
        state: JsonDict,
        *,
        user_input: str,
        request_id: str | None,
        conversation_id: str | None,
        branch_id: str,
        turn_kind: TurnKind,
        correlation_id: str | None = None,
    ):
        session = dict(state.get("session") or {})
        active_conversation = conversation_id or str(
            session.get("session_id") or "default-conversation"
        )
        self.journal.import_legacy_history(
            active_conversation,
            list(session.get("recent_turns") or []),
        )
        deadline = (
            now_ms() + self.config.turn_deadline_ms
            if self.config.turn_deadline_ms is not None
            else None
        )
        return self.journal.create_turn(
            conversation_id=active_conversation,
            request_id=request_id or f"req_{uuid.uuid4().hex}",
            user_input=user_input,
            turn_kind=turn_kind,
            branch_id=branch_id,
            deadline_at_ms=deadline,
            correlation_id=correlation_id,
        )

    def _execute(
        self,
        state: JsonDict,
        turn_id: str,
        *,
        user_input: str,
        proactive_trigger: JsonDict | None,
        cancellation: CancellationToken,
        sink: Callable[[ClientEvent], None] | None,
    ) -> JsonDict:
        started = time.perf_counter()
        context = self.journal.get_turn(turn_id)
        working_state = copy.deepcopy(state)
        visible_partial: list[str] = []
        deadline_timer: threading.Timer | None = None
        if context.deadline_at_ms is not None:
            delay_seconds = max(0.0, (context.deadline_at_ms - now_ms()) / 1000)
            deadline_timer = threading.Timer(
                delay_seconds,
                lambda: cancellation.cancel("turn_deadline_exceeded"),
            )
            deadline_timer.daemon = True
            deadline_timer.start()
        encrypted_history = self.journal.recent_turns(context.conversation_id, limit=40)
        working_state.setdefault("session", {})["recent_turns"] = [
            {
                "role": item["role"],
                "content": item["content"],
                "at": item["created_at_ms"],
            }
            for item in encrypted_history
        ]
        try:
            self.journal.transition(turn_id, TurnStatus.PREPARING, stage="wave_a")
            self._emit(turn_id, "turn.accepted", {"correlation_id": context.correlation_id}, sink)
            cancellation.raise_if_cancelled()
            self.journal.transition(turn_id, TurnStatus.GENERATING, stage="wave_b_and_generation")
            self._emit(turn_id, "turn.preparing", {"stage": "wave_b_and_generation"}, sink)
            with self.model_routes.activate(turn_id, cancellation) as model_budget:
                if proactive_trigger is None:
                    result = self.runner.run(
                        working_state,
                        user_input,
                        correlation_id=context.correlation_id,
                    )
                else:
                    result = self.runner.run_proactive(
                        working_state,
                        proactive_trigger,
                        correlation_id=context.correlation_id,
                    )
            cancellation.raise_if_cancelled()

            context_pack = dict(result.get("context_pack") or {})
            generation_gate = dict(context_pack.pop(GENERATION_CONTEXT_MARKER, {}) or {})
            if generation_gate.get("schema_version") != "GenerationContextGate.v1":
                raise RuntimeError(
                    "runner generated a response without the required generation context gate"
                )
            if generation_gate.get("context_hash") != generation_context_hash(context_pack):
                raise RuntimeError(
                    "generation context changed after the disclosure gate was applied"
                )
            result["context_pack"] = context_pack
            packed = self.context_packer.pack(context_pack)
            wave_a = {
                "turn_frame": context_pack.get("turn_frame"),
                "turn_interpretation": context_pack.get("turn_interpretation"),
                "interruptibility_frame": context_pack.get("interruptibility_frame"),
                "context_use_policy": context_pack.get("context_use_policy"),
            }
            self.journal.put_artifact(
                turn_id, "wave_a", wave_a, schema_version="ConversationWaveA.v1"
            )
            self.journal.put_artifact(
                turn_id,
                "wave_b",
                packed.to_dict(),
                schema_version="PackedConversationContext.v1",
            )

            reply = str(result.get("reply") or "")
            if reply:
                self.journal.transition(
                    turn_id,
                    TurnStatus.STREAMING,
                    stage="validated_stream",
                    visible_output_started=True,
                )
                self._emit(turn_id, "response.start", {"mode": "validated_stream"}, sink)
                for chunk in _chunks(reply, self.config.client_delta_chars):
                    cancellation.raise_if_cancelled()
                    self._emit(turn_id, "response.delta", {"text": chunk}, sink)
                    visible_partial.append(chunk)
            self.journal.transition(turn_id, TurnStatus.COMMITTING, stage="atomic_commit")
            runtime_metadata = {
                "schema_version": "ConversationRuntimeResult.v1",
                "turn_id": turn_id,
                "request_id": context.request_id,
                "conversation_id": context.conversation_id,
                "branch_id": context.branch_id,
                "correlation_id": context.correlation_id,
                "model_calls": list(model_budget.calls),
                "packed_context": {
                    "token_estimate": packed.token_estimate,
                    "pools": packed.pools,
                    "disclosure": packed.disclosure,
                    "dropped": list(packed.dropped),
                    "reply_budget_tokens": packed.reply_budget_tokens,
                    "generation_gate": generation_gate,
                },
                "streaming_mode": "provider_buffered_then_validated_client_stream",
                "idempotent_replay": False,
            }
            result["runtime"] = runtime_metadata
            outbox_payload = self._outbox_payload(
                result,
                context=context,
                working_state=working_state,
                user_input=user_input,
                reply=reply,
            )
            self._scrub_legacy_plaintext_state(working_state, turn_id)
            self.journal.commit_turn(
                turn_id,
                user_input=user_input,
                reply=reply,
                result=result,
                outbox_events=(
                    (("conversation.turn_committed", outbox_payload),)
                    if proactive_trigger is None
                    else ()
                ),
            )
            with self._state_lock:
                stale_keys = set(state) - set(working_state)
                state.update(working_state)
                for key in stale_keys:
                    state.pop(key, None)
            committed_event = self._emit(
                turn_id,
                "response.completed",
                {"reply_chars": len(reply), "committed": True},
                sink,
            )
            runtime_metadata["last_client_sequence"] = committed_event.sequence
            self.post_turn.notify()
            elapsed_ms = (time.perf_counter() - started) * 1000
            self.metrics.increment("conversation_turn_total", labels={"status": "committed"})
            self.metrics.observe(
                "conversation_turn_latency_ms",
                elapsed_ms,
                labels={"turn_kind": context.turn_kind.value},
            )
            self._record_context_metrics(context_pack, packed.dropped)
            return result
        except TurnCancelled as exc:
            self._finish_cancelled(
                turn_id,
                str(exc),
                sink,
                partial_text="".join(visible_partial),
            )
            self.metrics.increment("conversation_turn_total", labels={"status": "cancelled"})
            raise
        except Exception as exc:
            try:
                current = self.journal.get_turn(turn_id)
                if current.status not in {TurnStatus.COMMITTED, TurnStatus.CANCELLED, TurnStatus.FAILED}:
                    self.journal.transition(
                        turn_id,
                        TurnStatus.FAILED,
                        stage="failed",
                        error_code=type(exc).__name__,
                        error_detail=str(exc),
                    )
                self._emit(
                    turn_id,
                    "turn.failed",
                    {"error_code": type(exc).__name__},
                    sink,
                )
            finally:
                self.metrics.increment("conversation_turn_total", labels={"status": "failed"})
            raise
        finally:
            if deadline_timer is not None:
                deadline_timer.cancel()

    def _finish_cancelled(
        self,
        turn_id: str,
        reason: str,
        sink: Callable[[ClientEvent], None] | None,
        *,
        partial_text: str = "",
    ) -> None:
        current = self.journal.get_turn(turn_id)
        if current.status not in {TurnStatus.COMMITTED, TurnStatus.CANCELLED, TurnStatus.FAILED}:
            self.journal.transition(
                turn_id,
                TurnStatus.CANCELLED,
                stage="cancelled",
                error_code="TURN_CANCELLED",
                error_detail=reason,
            )
            artifact_id = None
            if partial_text:
                artifact_id = self.journal.put_artifact(
                    turn_id,
                    "aborted_response",
                    {
                        "text": partial_text,
                        "source": "validated_client_stream",
                        "copy_allowed": True,
                        "validated": True,
                    },
                    schema_version="AbortedConversationResponse.v1",
                )
            self._emit(
                turn_id,
                "turn.cancelled",
                {
                    "reason": reason,
                    "partial_response_available": artifact_id is not None,
                    "partial_response_artifact_id": artifact_id,
                },
                sink,
            )

    def _record_context_metrics(
        self, context_pack: Mapping[str, Any], dropped: tuple[str, ...]
    ) -> None:
        for reason in sorted(set(dropped)):
            self.metrics.increment(
                "runtime.degraded",
                labels={"stage": "context_pack", "domain": "prompt", "reason": reason},
            )
        for domain in ("persona", "affect", "relationship", "worldops"):
            frame = dict(context_pack.get(f"{domain}_frame") or {})
            reasons = [str(item) for item in frame.get("degraded_reasons") or []]
            runtime_mode = str(frame.get("runtime_mode") or "")
            if "stale" in runtime_mode or any("stale" in reason for reason in reasons):
                self.metrics.increment(
                    "runtime.snapshot.stale", labels={"domain": domain}
                )

    def _emit(
        self,
        turn_id: str,
        event_type: str,
        payload: JsonDict,
        sink: Callable[[ClientEvent], None] | None,
    ) -> ClientEvent:
        if event_type in {"response.completed", "turn.cancelled", "turn.failed"}:
            event = self.journal.ensure_client_event(turn_id, event_type, payload)
        else:
            event = self.journal.append_client_event(turn_id, event_type, payload)
        if sink is not None:
            sink(event)
        return event

    def _repair_terminal_client_event(self, turn_id: str) -> ClientEvent | None:
        context = self.journal.get_turn(turn_id)
        if context.status == TurnStatus.COMMITTED:
            result = self.journal.read_result(turn_id) or {}
            reply = str(result.get("reply") or "")
            return self.journal.ensure_client_event(
                turn_id,
                "response.completed",
                {"reply_chars": len(reply), "committed": True, "recovered": True},
            )
        if context.status == TurnStatus.CANCELLED:
            return self.journal.ensure_client_event(
                turn_id,
                "turn.cancelled",
                {"reason": "recovered_terminal", "recovered": True},
            )
        if context.status == TurnStatus.FAILED:
            return self.journal.ensure_client_event(
                turn_id,
                "turn.failed",
                {"error_code": "RECOVERED_TERMINAL", "recovered": True},
            )
        return None

    @staticmethod
    def _scrub_legacy_plaintext_state(state: JsonDict, turn_id: str) -> None:
        session = state.setdefault("session", {})
        session["recent_turns"] = []
        session["rolling_summary"] = ""
        session.setdefault("working_memory", {}).pop("last_reply_summary", None)
        observability = state.setdefault("observability", {})
        observability["last_context_pack"] = {
            "runtime_turn_id": turn_id,
            "storage": "encrypted_conversation_journal",
        }
        observability["last_response_plan"] = {
            "runtime_turn_id": turn_id,
            "storage": "encrypted_conversation_journal",
        }
        observability["last_run_result"] = {
            "runtime_turn_id": turn_id,
            "storage": "encrypted_conversation_journal",
        }
        dialogue_state = session.setdefault("dialogue_state", {})
        dialogue_state["active_topic"] = "encrypted_conversation_context"
        dialogue_state["user_goal"] = "continue_current_conversation"

    @staticmethod
    def _outbox_payload(
        result: JsonDict,
        *,
        context: Any,
        working_state: JsonDict,
        user_input: str,
        reply: str,
    ) -> JsonDict:
        pack = dict(result.get("context_pack") or {})
        return {
            "schema_version": "TurnCommittedEvent.v1",
            "runtime_turn_id": context.turn_id,
            "turn_id": str(working_state.get("session", {}).get("turn_id") or context.turn_id),
            "conversation_id": context.conversation_id,
            "correlation_id": context.correlation_id,
            "user_id": str(working_state.get("session", {}).get("user_id") or "default-user"),
            "user_input": user_input,
            "reply": reply,
            "turn_interpretation": pack.get("turn_interpretation") or {},
            "interruptibility_frame": pack.get("interruptibility_frame") or {},
            "persona_frame": pack.get("persona_frame") or {},
            "affect_frame": pack.get("affect_frame") or {},
            "relationship_frame": pack.get("relationship_frame") or {},
            "worldops_frame": pack.get("worldops_frame") or {},
            "response_plan": result.get("response_plan") or {},
        }


def _chunks(text: str, max_chars: int) -> Iterator[str]:
    size = max(8, max_chars)
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            for separator in ("。", "！", "？", "\n", "，", " "):
                candidate = text.rfind(separator, start + 1, end + 1)
                if candidate > start + size // 3:
                    end = candidate + 1
                    break
        yield text[start:end]
        start = end
