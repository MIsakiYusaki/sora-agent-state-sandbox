from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterator, Literal

from ..conversation_runtime.contracts import ClientEvent, TurnExecutionContext, TurnStatus
from .contracts import (
    GatewayErrorCode,
    GatewayEvent,
    HistoryItem,
    HistoryPage,
    PROTOCOL_VERSION,
    TurnAccepted,
    TurnStatusView,
    TurnSubmit,
    validate_identifier,
)
from .observability import GatewayMetrics
from .registry import GatewayRegistry, RegistryConflict, TurnBinding
from .runtime_port import ConversationRuntimePort, RuntimeIdempotencyConflict
from .security import SlidingWindowRateLimiter, VerifiedIdentity
from .state import ProfileStateRepository


TERMINAL_EVENT_TYPES = frozenset({"turn.committed", "turn.cancelled", "turn.failed"})


@dataclass(frozen=True)
class GatewayPolicy:
    max_workers: int = 8
    turns_per_minute: int = 30
    max_active_turns_per_profile: int = 2
    submit_accept_timeout_ms: int = 5_000
    event_poll_interval_ms: int = 50
    heartbeat_interval_ms: int = 20_000
    max_frame_bytes: int = 64 * 1024
    max_connections_per_profile: int = 4
    websocket_queue_size: int = 128
    slow_consumer_grace_ms: int = 5_000
    shutdown_grace_ms: int = 30_000
    stale_recovery_after_ms: int = 300_000
    history_page_limit: int = 40
    history_page_max: int = 200
    event_ack_retention_days: int = 30
    read_requests_per_minute: int = 600
    stream_connections_per_minute: int = 60
    turn_stream_timeout_ms: int = 5 * 60 * 1000
    turn_timeout_cancel_grace_ms: int = 5_000

    def __post_init__(self) -> None:
        positive = {
            name: value
            for name, value in vars(self).items()
            if isinstance(value, int) and name != "event_poll_interval_ms"
        }
        invalid = [name for name, value in positive.items() if value < 1]
        if invalid or self.event_poll_interval_ms < 1:
            raise ValueError(f"gateway policy values must be positive: {invalid}")


class GatewayServiceError(RuntimeError):
    def __init__(
        self,
        code: GatewayErrorCode,
        message: str,
        *,
        status_code: int,
        retryable: bool = False,
        retry_after_ms: int | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms
        self.details = details or {}


@dataclass
class _RequestGuard:
    lock: threading.Lock
    users: int = 0


class ConversationGatewayService:
    """Transport-independent gateway orchestration and ownership boundary."""

    def __init__(
        self,
        runtime: ConversationRuntimePort,
        *,
        registry: GatewayRegistry,
        state_repository: ProfileStateRepository,
        policy: GatewayPolicy | None = None,
        metrics: GatewayMetrics | None = None,
        identity_scope: tuple[str, str, str],
    ) -> None:
        self.runtime = runtime
        self.registry = registry
        self.state_repository = state_repository
        self.policy = policy or GatewayPolicy()
        self.metrics = metrics or GatewayMetrics()
        if len(identity_scope) != 3:
            raise ValueError("identity_scope must contain profile, subject, and agent ids")
        self.identity_scope = tuple(validate_identifier(value) for value in identity_scope)
        if state_repository.profile_scope != self.identity_scope[0]:
            raise ValueError("state repository scope must match gateway identity scope")
        self.rate_limiter = SlidingWindowRateLimiter()
        self._executor = ThreadPoolExecutor(
            max_workers=self.policy.max_workers,
            thread_name_prefix="sora-gateway-turn",
        )
        self._active_by_profile: dict[str, int] = {}
        self._active_conversations: set[str] = set()
        self._connections_by_profile: dict[str, int] = {}
        self._futures: set[Future[None]] = set()
        self._request_guards: dict[tuple[str, ...], _RequestGuard] = {}
        self._lock = threading.RLock()
        self._draining = False
        self._started = False

    def start(self) -> dict[str, object]:
        with self._lock:
            if self._started:
                return {"status": "already_started"}
            self._started = True
        recovery = self.runtime.recover(
            stale_after_ms=self.policy.stale_recovery_after_ms
        )
        maintenance = self.maintain()
        return {
            "status": "started",
            "recovery": recovery,
            "maintenance": maintenance,
        }

    def begin_drain(self) -> None:
        with self._lock:
            self._draining = True

    def close(self) -> dict[str, object]:
        self.begin_drain()
        deadline = time.monotonic() + self.policy.shutdown_grace_ms / 1000
        while time.monotonic() < deadline:
            with self._lock:
                active = [future for future in self._futures if not future.done()]
            if not active:
                break
            time.sleep(0.02)
        with self._lock:
            active_conversations = tuple(self._active_conversations)
        for conversation_id in active_conversations:
            self.runtime.cancel(conversation_id, reason="gateway_shutdown")
        self._executor.shutdown(wait=True, cancel_futures=False)
        with self._lock:
            remaining = sum(1 for future in self._futures if not future.done())
        return {"status": "closed", "remaining_turns": remaining}

    def submit(self, identity: VerifiedIdentity, request: TurnSubmit) -> TurnAccepted:
        self._require_permission(identity, "turn.submit")
        if request.client_instance_id != identity.client_instance_id:
            raise GatewayServiceError(
                GatewayErrorCode.FORBIDDEN,
                "client_instance_id does not match the capability",
                status_code=403,
            )
        key = (
            identity.profile_id,
            identity.subject_id,
            identity.agent_id,
            request.conversation_id,
            request.request_id,
        )
        with self._serialize_request(key):
            return self._submit_once(identity, request)

    def _submit_once(
        self, identity: VerifiedIdentity, request: TurnSubmit
    ) -> TurnAccepted:
        started = time.perf_counter()
        with self._lock:
            if self._draining:
                raise GatewayServiceError(
                    GatewayErrorCode.DRAINING,
                    "gateway is draining",
                    status_code=503,
                    retryable=True,
                    retry_after_ms=1_000,
                )

        input_hash = self._request_fingerprint(request)
        existing = self.registry.turn_for_request(
            profile_id=identity.profile_id,
            subject_id=identity.subject_id,
            agent_id=identity.agent_id,
            external_conversation_id=request.conversation_id,
            request_id=request.request_id,
        )
        if existing is not None:
            if not self._fingerprint_matches(existing.input_hash, request):
                raise GatewayServiceError(
                    GatewayErrorCode.IDEMPOTENCY_CONFLICT,
                    "request_id was reused with different input",
                    status_code=409,
                )
            return self._accepted(existing, idempotent_replay=True)

        self._consume_rate_limit(
            identity,
            "turn.submit",
            limit=self.policy.turns_per_minute,
        )
        conversation = self.registry.ensure_conversation(
            profile_id=identity.profile_id,
            subject_id=identity.subject_id,
            agent_id=identity.agent_id,
            external_conversation_id=request.conversation_id,
        )
        correlation_id = request.correlation_id or uuid.uuid4().hex
        current_version = self.runtime.conversation_version(
            conversation.runtime_conversation_id
        )
        if (
            request.expected_conversation_version is not None
            and request.expected_conversation_version != current_version
        ):
            raise GatewayServiceError(
                GatewayErrorCode.VERSION_CONFLICT,
                "conversation version does not match",
                status_code=409,
                details={"current_conversation_version": current_version},
            )
        self._reserve_turn_slot(
            identity.profile_id, conversation.runtime_conversation_id
        )
        first: queue.Queue[ClientEvent | BaseException] = queue.Queue(maxsize=1)

        def execute() -> None:
            first_sent = False
            try:
                with self.state_repository.turn_state(
                    identity.profile_id,
                    request.conversation_id,
                    user_id=identity.subject_id,
                    agent_id=identity.agent_id,
                    pair_id=identity.pair_id,
                ) as state:
                    for event in self.runtime.stream_turn(
                        state,
                        request.text,
                        request_id=request.request_id,
                        conversation_id=conversation.runtime_conversation_id,
                        branch_id=request.branch_id,
                        correlation_id=correlation_id,
                    ):
                        if not first_sent:
                            first.put(event)
                            first_sent = True
                if not first_sent:
                    context = self.runtime.turn_by_request(
                        conversation.runtime_conversation_id, request.request_id
                    )
                    if context is None:
                        raise RuntimeError("runtime returned no event or turn")
                    replay = self.runtime.reconnect(context.turn_id, after_sequence=0)
                    if not replay:
                        raise RuntimeError("runtime persisted no client event")
                    first.put(replay[0])
            except BaseException as exc:
                if not first_sent:
                    first.put(exc)
                self.metrics.increment(
                    "gateway_turn_total", labels={"status": "worker_failed"}
                )
            finally:
                self._release_turn_slot(
                    identity.profile_id, conversation.runtime_conversation_id
                )

        future = self._executor.submit(execute)
        with self._lock:
            self._futures.add(future)
        future.add_done_callback(self._forget_future)
        try:
            outcome = first.get(timeout=self.policy.submit_accept_timeout_ms / 1000)
        except queue.Empty as exc:
            raise GatewayServiceError(
                GatewayErrorCode.INTERNAL_ERROR,
                "runtime did not acknowledge the turn in time",
                status_code=503,
                retryable=True,
                retry_after_ms=250,
            ) from exc
        if isinstance(outcome, BaseException):
            self._raise_runtime_error(outcome)
        context = self.runtime.turn_status(outcome.turn_id)
        correlation_id = context.correlation_id
        try:
            binding = self.registry.bind_turn(
                turn_id=outcome.turn_id,
                profile_id=identity.profile_id,
                subject_id=identity.subject_id,
                agent_id=identity.agent_id,
                client_instance_id=identity.client_instance_id,
                external_conversation_id=request.conversation_id,
                runtime_conversation_id=conversation.runtime_conversation_id,
                request_id=request.request_id,
                client_message_id=request.client_message_id,
                input_hash=input_hash,
                correlation_id=correlation_id,
                protocol_version=request.protocol_version,
            )
        except RegistryConflict as exc:
            raise GatewayServiceError(
                GatewayErrorCode.IDEMPOTENCY_CONFLICT,
                str(exc),
                status_code=409,
            ) from exc
        self.metrics.increment(
            "gateway_turn_total", labels={"status": self._product_status(context)}
        )
        self.metrics.observe(
            "gateway_submit_ack_ms", (time.perf_counter() - started) * 1000
        )
        return self._accepted(binding, idempotent_replay=False)

    def status(self, identity: VerifiedIdentity, turn_id: str) -> TurnStatusView:
        self._consume_rate_limit(
            identity,
            "turn.status",
            limit=self.policy.read_requests_per_minute,
        )
        binding = self._require_turn(identity, turn_id, permission="turn.read")
        return self._status_view(binding)

    def _status_view(self, binding: TurnBinding) -> TurnStatusView:
        turn_id = binding.turn_id
        context = self._status_or_not_found(turn_id)
        return TurnStatusView(
            turn_id=turn_id,
            request_id=binding.request_id,
            conversation_id=binding.external_conversation_id,
            correlation_id=binding.correlation_id,
            runtime_status=context.status.value,
            product_status=self._product_status(context),
            stage=context.stage,
            revision=context.revision,
            visible_output_started=context.visible_output_started,
            created_at_ms=context.created_at_ms,
            updated_at_ms=context.updated_at_ms,
            committed_at_ms=context.committed_at_ms,
        )

    def events(
        self,
        identity: VerifiedIdentity,
        turn_id: str,
        *,
        after_sequence: int = 0,
    ) -> list[GatewayEvent]:
        binding = self._require_turn(identity, turn_id, permission="turn.read")
        return self._events_for_binding(binding, after_sequence=after_sequence)

    def authorize_turn(self, identity: VerifiedIdentity, turn_id: str) -> None:
        self._consume_rate_limit(
            identity,
            "stream.open",
            limit=self.policy.stream_connections_per_minute,
        )
        self._require_turn(identity, turn_id, permission="turn.read")

    def iter_events(
        self,
        identity: VerifiedIdentity,
        turn_id: str,
        *,
        after_sequence: int = 0,
    ) -> Iterator[GatewayEvent | None]:
        binding = self._require_turn(identity, turn_id, permission="turn.read")
        context = self._status_or_not_found(turn_id)
        expires_at_ms = context.created_at_ms + self.policy.turn_stream_timeout_ms
        timeout_cancelled_at: float | None = None
        sequence = max(0, after_sequence)
        heartbeat_at = time.monotonic() + self.policy.heartbeat_interval_ms / 1000
        while True:
            batch = self._events_for_binding(binding, after_sequence=sequence)
            for event in batch:
                sequence = event.sequence
                yield event
                if event.event_type in TERMINAL_EVENT_TYPES:
                    return
            context = self._status_or_not_found(turn_id)
            if context.status in {
                TurnStatus.COMMITTED,
                TurnStatus.CANCELLED,
                TurnStatus.FAILED,
            }:
                repaired = self._events_for_binding(binding, after_sequence=sequence)
                if not repaired:
                    return
                continue
            if int(time.time() * 1000) >= expires_at_ms:
                if timeout_cancelled_at is None:
                    self.runtime.cancel(
                        binding.runtime_conversation_id,
                        reason="gateway_turn_timeout",
                    )
                    timeout_cancelled_at = time.monotonic()
                    self.metrics.increment(
                        "gateway_stream_disconnect_total",
                        labels={"reason": "turn_timeout"},
                    )
                elif (
                    time.monotonic() - timeout_cancelled_at
                    >= self.policy.turn_timeout_cancel_grace_ms / 1000
                ):
                    raise GatewayServiceError(
                        GatewayErrorCode.TURN_TIMEOUT,
                        "turn exceeded the maximum stream lifetime",
                        status_code=504,
                        retryable=True,
                        retry_after_ms=1_000,
                    )
            if time.monotonic() >= heartbeat_at:
                yield None
                heartbeat_at = time.monotonic() + self.policy.heartbeat_interval_ms / 1000
            time.sleep(self.policy.event_poll_interval_ms / 1000)

    def cancel(
        self,
        identity: VerifiedIdentity,
        turn_id: str,
        *,
        reason: str = "user_cancelled",
    ) -> TurnStatusView:
        binding = self._require_turn(identity, turn_id, permission="turn.cancel")
        self.runtime.cancel(binding.runtime_conversation_id, reason=reason[:120])
        return self._status_view(binding)

    def acknowledge(self, identity: VerifiedIdentity, turn_id: str, sequence: int) -> None:
        self._require_turn(identity, turn_id, permission="turn.read")
        self.registry.acknowledge(identity.capability_id, turn_id, sequence)

    def history(
        self,
        identity: VerifiedIdentity,
        conversation_id: str,
        *,
        limit: int | None = None,
        before_sequence: int | None = None,
    ) -> HistoryPage:
        self._consume_rate_limit(
            identity,
            "history.read",
            limit=self.policy.read_requests_per_minute,
        )
        self._require_permission(identity, "history.read")
        binding = self.registry.conversation_for_identity(
            profile_id=identity.profile_id,
            subject_id=identity.subject_id,
            agent_id=identity.agent_id,
            external_conversation_id=conversation_id,
        )
        if binding is None:
            return HistoryPage(
                conversation_id=conversation_id,
                conversation_version=0,
                items=(),
                next_before_sequence=None,
            )
        bounded = min(
            max(1, limit or self.policy.history_page_limit),
            self.policy.history_page_max,
        )
        records = self.runtime.history_page(
            binding.runtime_conversation_id,
            limit=bounded,
            before_sequence=before_sequence,
        )
        items = tuple(
            HistoryItem(
                role="user" if str(item["role"]) == "user" else "sora",
                content=str(item["content"]),
                turn_id=str(item["turn_id"]),
                sequence=int(item["sequence"]),
                created_at_ms=int(item["created_at_ms"]),
            )
            for item in records
        )
        next_before = items[0].sequence if len(items) == bounded else None
        return HistoryPage(
            conversation_id=conversation_id,
            conversation_version=self.runtime.conversation_version(
                binding.runtime_conversation_id
            ),
            items=items,
            next_before_sequence=next_before,
        )

    def conversations(
        self,
        identity: VerifiedIdentity,
        *,
        limit: int = 100,
    ) -> dict[str, object]:
        """Build the user-facing conversation index from identity-scoped bindings."""
        self._consume_rate_limit(
            identity,
            "history.read",
            limit=self.policy.read_requests_per_minute,
        )
        self._require_permission(identity, "history.read")
        bindings = self.registry.list_conversations_for_identity(
            profile_id=identity.profile_id,
            subject_id=identity.subject_id,
            agent_id=identity.agent_id,
            limit=limit,
        )
        items: list[dict[str, object]] = []
        for binding in bindings:
            records = self.runtime.history_page(
                binding.runtime_conversation_id,
                limit=200,
                before_sequence=None,
            )
            if not records:
                continue
            first_user = next(
                (item for item in records if str(item.get("role")) == "user"),
                records[0],
            )
            last = records[-1]
            title = str(first_user.get("content") or "新的对话").strip()
            preview = str(last.get("content") or "").strip()
            items.append(
                {
                    "conversation_id": binding.external_conversation_id,
                    "title": title[:42] or "新的对话",
                    "preview": preview[:96],
                    "created_at_ms": binding.created_at_ms,
                    "updated_at_ms": int(last.get("created_at_ms") or binding.created_at_ms),
                    "message_count": len(records),
                    "conversation_version": self.runtime.conversation_version(
                        binding.runtime_conversation_id
                    ),
                }
            )
        items.sort(key=lambda item: int(item["updated_at_ms"]), reverse=True)
        return {
            "schema_version": "ConversationIndex.v1",
            "items": items,
        }

    def readiness(self) -> dict[str, object]:
        runtime = self.runtime.health()
        registry = self.registry.health()
        with self._lock:
            draining = self._draining
            active = sum(self._active_by_profile.values())
        ready = (
            not draining
            and str(runtime.get("status")) == "ok"
            and registry.get("status") == "ok"
        )
        return {
            "status": "ready" if ready else "not_ready",
            "protocol_version": PROTOCOL_VERSION,
            "draining": draining,
            "active_turns": active,
            "runtime": runtime,
            "registry": registry,
        }

    def quality_report(self) -> dict[str, object]:
        return {
            "schema_version": "ConversationGatewayQualityReport.v1",
            "readiness": self.readiness(),
            "metrics": self.metrics.export(),
        }

    def maintain(self, *, at_ms: int | None = None) -> dict[str, object]:
        current = at_ms or int(time.time() * 1000)
        day_ms = 24 * 60 * 60 * 1000
        return {
            "runtime": self.runtime.maintain(at_ms=current),
            "registry": self.registry.maintain(
                ack_before_ms=current - self.policy.event_ack_retention_days * day_ms
            ),
        }

    def register_quality_ops(self, memory_runtime) -> None:
        from .eval import run_gateway_self_check

        memory_runtime.register_quality_eval_suite(
            "conversation_gateway",
            lambda: run_gateway_self_check(self),
            source_module="live_text_conversation_gateway",
        )
        memory_runtime.register_quality_export_section(
            "conversation_gateway",
            self.quality_report,
        )

    def reserve_connection(self, identity: VerifiedIdentity) -> None:
        self._require_permission(identity, "stream.connect")
        self._consume_rate_limit(
            identity,
            "stream.connect",
            limit=self.policy.stream_connections_per_minute,
        )
        with self._lock:
            current = self._connections_by_profile.get(identity.profile_id, 0)
            if current >= self.policy.max_connections_per_profile:
                raise GatewayServiceError(
                    GatewayErrorCode.RATE_LIMITED,
                    "profile has too many active stream connections",
                    status_code=429,
                    retryable=True,
                    retry_after_ms=1_000,
                )
            self._connections_by_profile[identity.profile_id] = current + 1

    def release_connection(self, identity: VerifiedIdentity) -> None:
        with self._lock:
            current = self._connections_by_profile.get(identity.profile_id, 0)
            if current <= 1:
                self._connections_by_profile.pop(identity.profile_id, None)
            else:
                self._connections_by_profile[identity.profile_id] = current - 1

    def _accepted(self, binding: TurnBinding, *, idempotent_replay: bool) -> TurnAccepted:
        context = self._status_or_not_found(binding.turn_id)
        return TurnAccepted(
            turn_id=binding.turn_id,
            request_id=binding.request_id,
            conversation_id=binding.external_conversation_id,
            correlation_id=binding.correlation_id,
            status=self._accepted_status(context),
            conversation_version=self.runtime.conversation_version(
                binding.runtime_conversation_id
            ),
            idempotent_replay=idempotent_replay,
            events_url=f"/v1/turns/{binding.turn_id}/events",
        )

    @staticmethod
    def _accepted_status(
        context: TurnExecutionContext,
    ) -> Literal["accepted", "committed", "cancelled", "failed"]:
        product = ConversationGatewayService._product_status(context)
        return "accepted" if product == "active" else product

    @staticmethod
    def _product_status(
        context: TurnExecutionContext,
    ) -> Literal["active", "committed", "cancelled", "failed"]:
        if context.status == TurnStatus.COMMITTED:
            return "committed"
        if context.status == TurnStatus.CANCELLED:
            return "cancelled"
        if context.status == TurnStatus.FAILED:
            return "failed"
        return "active"

    def _map_event(
        self, binding: TurnBinding, event: ClientEvent, *, replayed: bool
    ) -> GatewayEvent:
        event_type = event.event_type
        payload = dict(event.payload)
        if event_type == "turn.preparing":
            event_type = "turn.progress"
            payload.setdefault("runtime_status", "preparing")
        elif event_type == "response.delta" and "text" in payload:
            payload["delta"] = payload.pop("text")
        elif event_type == "response.completed" and bool(payload.get("committed")):
            event_type = "turn.committed"
            payload["conversation_version"] = self.runtime.conversation_version(
                binding.runtime_conversation_id
            )
        return GatewayEvent(
            turn_id=event.turn_id,
            sequence=event.sequence,
            event_type=event_type,
            correlation_id=binding.correlation_id,
            payload=payload,
            created_at_ms=event.created_at_ms,
            replayed=replayed,
        )

    def _require_turn(
        self, identity: VerifiedIdentity, turn_id: str, *, permission: str
    ) -> TurnBinding:
        self._require_permission(identity, permission)
        binding = self.registry.turn_for_identity(
            turn_id=turn_id,
            profile_id=identity.profile_id,
            subject_id=identity.subject_id,
            agent_id=identity.agent_id,
        )
        if binding is None:
            raise GatewayServiceError(
                GatewayErrorCode.TURN_NOT_FOUND,
                "turn was not found",
                status_code=404,
            )
        return binding

    def _require_permission(self, identity: VerifiedIdentity, permission: str) -> None:
        self._require_identity_scope(identity)
        if permission not in identity.permissions:
            raise GatewayServiceError(
                GatewayErrorCode.FORBIDDEN,
                "capability does not grant this operation",
                status_code=403,
            )

    def _require_identity_scope(self, identity: VerifiedIdentity) -> None:
        if (
            identity.profile_id,
            identity.subject_id,
            identity.agent_id,
        ) != self.identity_scope:
            raise GatewayServiceError(
                GatewayErrorCode.FORBIDDEN,
                "capability is outside this gateway process identity scope",
                status_code=403,
            )

    def _reserve_turn_slot(self, profile_id: str, runtime_conversation_id: str) -> None:
        with self._lock:
            current = self._active_by_profile.get(profile_id, 0)
            if current >= self.policy.max_active_turns_per_profile:
                raise GatewayServiceError(
                    GatewayErrorCode.TURN_IN_PROGRESS,
                    "profile has too many active turns",
                    status_code=409,
                    retryable=True,
                    retry_after_ms=100,
                )
            if runtime_conversation_id in self._active_conversations:
                raise GatewayServiceError(
                    GatewayErrorCode.TURN_IN_PROGRESS,
                    "conversation already has an active turn",
                    status_code=409,
                    retryable=True,
                    retry_after_ms=100,
                )
            self._active_by_profile[profile_id] = current + 1
            self._active_conversations.add(runtime_conversation_id)

    def _release_turn_slot(self, profile_id: str, runtime_conversation_id: str) -> None:
        with self._lock:
            current = self._active_by_profile.get(profile_id, 0)
            if current <= 1:
                self._active_by_profile.pop(profile_id, None)
            else:
                self._active_by_profile[profile_id] = current - 1
            self._active_conversations.discard(runtime_conversation_id)

    def _forget_future(self, future: Future[None]) -> None:
        with self._lock:
            self._futures.discard(future)

    def _events_for_binding(
        self, binding: TurnBinding, *, after_sequence: int
    ) -> list[GatewayEvent]:
        try:
            events = self.runtime.reconnect(
                binding.turn_id, after_sequence=after_sequence
            )
        except KeyError as exc:
            raise GatewayServiceError(
                GatewayErrorCode.TURN_NOT_FOUND,
                "turn was not found",
                status_code=404,
            ) from exc
        return [self._map_event(binding, event, replayed=True) for event in events]

    def _consume_rate_limit(
        self, identity: VerifiedIdentity, operation: str, *, limit: int
    ) -> None:
        self._require_identity_scope(identity)
        retry_after = self.rate_limiter.consume(
            identity.profile_id,
            operation,
            limit=limit,
        )
        if not retry_after:
            return
        self.metrics.increment(
            "gateway_rate_limited_total", labels={"operation": operation}
        )
        raise GatewayServiceError(
            GatewayErrorCode.RATE_LIMITED,
            "gateway request rate limit exceeded",
            status_code=429,
            retryable=True,
            retry_after_ms=retry_after,
        )

    @contextmanager
    def _serialize_request(self, key: tuple[str, ...]):
        with self._lock:
            guard = self._request_guards.get(key)
            if guard is None:
                guard = _RequestGuard(lock=threading.Lock())
                self._request_guards[key] = guard
            guard.users += 1
        try:
            with guard.lock:
                yield
        finally:
            with self._lock:
                guard.users -= 1
                if guard.users == 0 and self._request_guards.get(key) is guard:
                    self._request_guards.pop(key, None)

    @staticmethod
    def _request_fingerprint(request: TurnSubmit) -> str:
        canonical = json.dumps(
            {
                "text": request.text,
                "branch_id": request.branch_id,
                "client_message_id": request.client_message_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "v2:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _fingerprint_matches(cls, stored: str, request: TurnSubmit) -> bool:
        if stored.startswith("v2:"):
            return stored == cls._request_fingerprint(request)
        legacy = hashlib.sha256(request.text.encode("utf-8")).hexdigest()
        return stored == legacy

    def _status_or_not_found(self, turn_id: str) -> TurnExecutionContext:
        try:
            return self.runtime.turn_status(turn_id)
        except KeyError as exc:
            raise GatewayServiceError(
                GatewayErrorCode.TURN_NOT_FOUND,
                "turn was not found",
                status_code=404,
            ) from exc

    @staticmethod
    def _raise_runtime_error(exc: BaseException) -> None:
        name = type(exc).__name__
        if isinstance(exc, RuntimeIdempotencyConflict):
            raise GatewayServiceError(
                GatewayErrorCode.IDEMPOTENCY_CONFLICT,
                "request_id was reused with different input",
                status_code=409,
            ) from exc
        raise GatewayServiceError(
            GatewayErrorCode.INTERNAL_ERROR,
            "runtime could not accept the turn",
            status_code=503,
            retryable=True,
            retry_after_ms=250,
            details={"runtime_error": name},
        ) from exc
