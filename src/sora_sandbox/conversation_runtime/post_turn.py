from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .contracts import JsonDict
from .journal import ConversationJournal
from ..turn_observability import TurnEvidence, observe_call


class PostTurnConsumer(Protocol):
    name: str
    event_types: frozenset[str]

    def handle(self, event: Mapping[str, Any]) -> JsonDict | None:
        ...


class MetricsSink(Protocol):
    def increment(self, name: str, *, labels: JsonDict | None = None, value: int = 1) -> None:
        ...


@dataclass(frozen=True)
class FunctionPostTurnConsumer:
    name: str
    handler: Callable[[Mapping[str, Any]], JsonDict | None]
    event_types: frozenset[str] = frozenset({"conversation.turn_committed"})

    def handle(self, event: Mapping[str, Any]) -> JsonDict | None:
        return self.handler(event)


class PostTurnDispatcher:
    """At-least-once outbox delivery with per-consumer idempotent receipts."""

    def __init__(
        self,
        journal: ConversationJournal,
        *,
        poll_interval_seconds: float = 0.25,
        metrics: MetricsSink | None = None,
    ) -> None:
        self.journal = journal
        self.poll_interval_seconds = max(0.05, poll_interval_seconds)
        self.worker_id = f"postturn_{uuid.uuid4().hex}"
        self.metrics = metrics
        self._consumers: dict[str, PostTurnConsumer] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def register(self, consumer: PostTurnConsumer, *, replace: bool = False) -> None:
        with self._lock:
            if consumer.name in self._consumers and not replace:
                raise ValueError(f"post-turn consumer already registered: {consumer.name}")
            self._consumers[consumer.name] = consumer

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="sora-post-turn-outbox",
                daemon=True,
            )
            self._thread.start()

    def notify(self) -> None:
        self._wake.set()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def close(self, *, timeout: float = 10.0, drain: bool = True) -> None:
        if drain:
            deadline = time.monotonic() + max(0.0, timeout / 2)
            while time.monotonic() < deadline:
                if self.process_once(limit=100)["claimed"] == 0:
                    break
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.1, timeout))
            if thread.is_alive():
                raise RuntimeError("post-turn dispatcher did not stop cleanly")
        self._thread = None

    def process_once(self, *, limit: int = 20) -> JsonDict:
        events = self.journal.claim_outbox(worker_id=self.worker_id, limit=limit)
        delivered = 0
        failed = 0
        for event in events:
            matching = self._matching_consumers(str(event["event_type"]))
            errors: list[str] = []
            for consumer in matching:
                receipt = self.journal.consumer_receipt(consumer.name, str(event["event_id"]))
                if receipt is not None and receipt.get("status") == "delivered":
                    if receipt.get("payload_hash") != event["payload_hash"]:
                        errors.append(
                            f"payload hash changed for delivered event consumer={consumer.name}"
                        )
                    continue
                try:
                    result = self._consume_observed(consumer, event)
                    self.journal.complete_consumer(
                        consumer_name=consumer.name,
                        event_id=str(event["event_id"]),
                        payload_hash=str(event["payload_hash"]),
                        details=result or {},
                    )
                except Exception as exc:
                    error = f"{consumer.name}:{type(exc).__name__}:{exc}"
                    errors.append(error)
                    self.journal.complete_consumer(
                        consumer_name=consumer.name,
                        event_id=str(event["event_id"]),
                        payload_hash=str(event["payload_hash"]),
                        status="failed",
                        details={"error": error[:500]},
                    )
            if not errors:
                self.journal.finalize_outbox(str(event["event_id"]), delivered=True)
                delivered += 1
            else:
                error = "; ".join(errors)
                delivery_status = self.journal.finalize_outbox(
                    str(event["event_id"]), delivered=False, error=error
                )
                if self.metrics is not None and delivery_status == "dead_letter":
                    failed_consumer = error.split(":", 1)[0] if error else "unknown"
                    self.metrics.increment(
                        "runtime.outbox.dead_letter",
                        labels={"consumer": failed_consumer},
                    )
                failed += 1
        return {"claimed": len(events), "delivered": delivered, "failed": failed}

    def _consume_observed(self, consumer, event):
        evidence = TurnEvidence(lane="post_turn")
        name = consumer.name if consumer.name in {"memory", "affect", "relationship", "worldops"} else "external"
        try:
            with evidence.activate():
                return observe_call("post_turn." + name, consumer.handle, event)
        finally:
            try:
                self.journal.put_artifact(
                    str(event["turn_id"]), "post_turn_evidence",
                    evidence.snapshot(final=True) | {"event_id": str(event["event_id"]), "attempt": int(event["attempts"])},
                    schema_version="TurnExecutionEvidence.v1",
                )
            except Exception:
                if self.metrics is not None:
                    self.metrics.increment("conversation_evidence_write_failed")

    def _matching_consumers(self, event_type: str) -> list[PostTurnConsumer]:
        with self._lock:
            return [
                consumer
                for consumer in self._consumers.values()
                if event_type in consumer.event_types or "*" in consumer.event_types
            ]

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.process_once(limit=50)
            except Exception:
                self._stop.wait(min(5.0, self.poll_interval_seconds * 4))
                continue
            if result["claimed"] == 0:
                self._wake.wait(self.poll_interval_seconds)
                self._wake.clear()


class MemoryPostTurnConsumer:
    name = "memory"
    event_types = frozenset({"conversation.turn_committed"})

    def __init__(self, memory_runtime: Any) -> None:
        self.memory_runtime = memory_runtime

    def handle(self, event: Mapping[str, Any]) -> JsonDict:
        observe_committed = getattr(self.memory_runtime, "observe_committed_turn", None)
        if callable(observe_committed):
            return observe_committed(dict(event))
        payload = dict(event["payload"])
        updates = self.memory_runtime.observe_turn_candidates(
            str(payload.get("user_input") or ""), str(payload.get("reply") or "")
        )
        plan_update = self.memory_runtime.observe_response_plan(
            dict(payload.get("response_plan") or {})
        )
        return {
            "candidate_count": len(updates),
            "response_plan_candidate": plan_update,
        }


class DomainPostTurnConsumer:
    name = "domain_observers"
    event_types = frozenset({"conversation.turn_committed"})

    def __init__(
        self,
        *,
        affect_runtime: Any | None = None,
        relationship_runtime: Any | None = None,
        worldops_runtime: Any | None = None,
        relationship_outcome_builder: Callable[..., JsonDict | None] | None = None,
    ) -> None:
        self.affect_runtime = affect_runtime
        self.relationship_runtime = relationship_runtime
        self.worldops_runtime = worldops_runtime
        self.relationship_outcome_builder = relationship_outcome_builder

    def handle(self, event: Mapping[str, Any]) -> JsonDict:
        payload = dict(event["payload"])
        user_input = str(payload.get("user_input") or "")
        turn_id = str(payload.get("turn_id") or event["turn_id"])
        correlation_id = str(payload.get("correlation_id") or "")
        user_id = str(payload.get("user_id") or "default-user")
        interpretation = dict(payload.get("turn_interpretation") or {})
        interrupt = dict(payload.get("interruptibility_frame") or {})
        persona = dict(payload.get("persona_frame") or {})
        relationship = dict(payload.get("relationship_frame") or {})
        results: JsonDict = {}
        failures: list[str] = []
        consumers: list[PostTurnConsumer] = []
        if self.relationship_runtime is not None:
            consumers.append(
                RelationshipPostTurnConsumer(
                    self.relationship_runtime,
                    outcome_builder=self.relationship_outcome_builder,
                )
            )
        if self.affect_runtime is not None:
            consumers.append(AffectPostTurnConsumer(self.affect_runtime))
        if self.worldops_runtime is not None:
            consumers.append(WorldOpsPostTurnConsumer(self.worldops_runtime))
        for consumer in consumers:
            try:
                results[consumer.name] = consumer.handle(event)
            except Exception as exc:
                failures.append(f"{consumer.name}:{type(exc).__name__}:{exc}")
        if failures:
            raise RuntimeError("; ".join(failures))
        return results


class RelationshipPostTurnConsumer:
    name = "relationship"
    event_types = frozenset({"conversation.turn_committed"})

    def __init__(
        self,
        runtime: Any,
        *,
        outcome_builder: Callable[..., JsonDict | None] | None = None,
    ) -> None:
        self.runtime = runtime
        self.outcome_builder = outcome_builder

    def handle(self, event: Mapping[str, Any]) -> JsonDict:
        payload = dict(event["payload"])
        user_input = str(payload.get("user_input") or "")
        turn_id = str(payload.get("turn_id") or event["turn_id"])
        correlation_id = str(payload.get("correlation_id") or "")
        user_id = str(payload.get("user_id") or "default-user")
        ingestion = self.runtime.observe_user_turn(
            user_input,
            user_id=user_id,
            turn_id=turn_id,
            correlation_id=correlation_id,
            turn_interpretation=dict(payload.get("turn_interpretation") or {}),
        ).get("ingestion", {})
        outcome_result = None
        if self.outcome_builder is not None:
            outcome = self.outcome_builder(
                user_input,
                turn_id=turn_id,
                correlation_id=correlation_id,
                relationship_frame=dict(payload.get("relationship_frame") or {}),
            )
            if outcome is not None:
                outcome_result = self.runtime.observe_interaction_outcome(
                    outcome,
                    user_id=user_id,
                )
        return {"ingestion": ingestion, "explicit_outcome": outcome_result}


class AffectPostTurnConsumer:
    name = "affect"
    event_types = frozenset({"conversation.turn_committed"})

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def handle(self, event: Mapping[str, Any]) -> JsonDict:
        payload = dict(event["payload"])
        return self.runtime.observe_user_turn(
            str(payload.get("user_input") or ""),
            user_id=str(payload.get("user_id") or "default-user"),
            turn_id=str(payload.get("turn_id") or event["turn_id"]),
            correlation_id=str(payload.get("correlation_id") or ""),
            turn_interpretation=dict(payload.get("turn_interpretation") or {}),
            conversation_scope=str(payload.get("conversation_id") or "default"),
            relationship_state=dict(payload.get("relationship_frame") or {}),
            persona_frame=dict(payload.get("persona_frame") or {}),
            interruptibility_frame=dict(payload.get("interruptibility_frame") or {}),
        ).get("ingestion", {})


class WorldOpsPostTurnConsumer:
    name = "worldops"
    event_types = frozenset({"conversation.turn_committed"})

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def handle(self, event: Mapping[str, Any]) -> JsonDict:
        payload = dict(event["payload"])
        return self.runtime.observe_user_turn(
            str(payload.get("user_input") or ""),
            turn_id=str(payload.get("turn_id") or event["turn_id"]),
            correlation_id=str(payload.get("correlation_id") or ""),
            turn_interpretation=dict(payload.get("turn_interpretation") or {}),
            interruptibility_frame=dict(payload.get("interruptibility_frame") or {}),
            persona_frame=dict(payload.get("persona_frame") or {}),
            relationship_frame=dict(payload.get("relationship_frame") or {}),
        ).get("ingestion", {})
