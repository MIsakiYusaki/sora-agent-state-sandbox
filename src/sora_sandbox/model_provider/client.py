from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from .adapters import AdapterRegistry
from .broker import CredentialedHTTPBroker
from .contracts import OwnerScope, ResolvedTarget, RoutingSnapshot, Workload
from .errors import ModelProviderError, ProviderErrorCode
from .registry import ProviderRegistry
from .router import WorkloadRouter


JsonDict = dict[str, Any]


class ProviderLLMClient:
    """LLMClient implementation backed by provider profiles and workload routes."""

    def __init__(
        self,
        registry: ProviderRegistry,
        router: WorkloadRouter,
        broker: CredentialedHTTPBroker,
        *,
        adapters: AdapterRegistry | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.registry = registry
        self.router = router
        self.broker = broker
        self.adapters = adapters or AdapterRegistry()
        self.timeout_seconds = timeout_seconds
        self.provider_system: Any | None = None

    @contextmanager
    def activate_turn(self, turn_id: str):
        with self.router.activate_turn(turn_id) as snapshots:
            yield snapshots

    def has_binding(self, workload: Workload | str) -> bool:
        return self.router.has_binding(workload)

    def chat(self, system: str, user: str, **kwargs: Any) -> JsonDict:
        workload = self._workload(kwargs.pop("workload", None), user)
        turn_id = kwargs.pop("_sora_turn_id", None)
        correlation_id = kwargs.pop("correlation_id", None)
        requested_model = kwargs.pop("model", None)
        snapshot = self.router.resolve(workload, turn_id=turn_id)
        self._check_requested_model(snapshot, requested_model)
        timeout = float(kwargs.pop("timeout", self.timeout_seconds))
        cancellation = kwargs.pop("cancellation_token", None)
        targets = (snapshot.primary, *snapshot.fallbacks)
        last_error: ModelProviderError | None = None
        for ordinal, target in enumerate(targets):
            invocation_id = f"pi_{uuid.uuid4().hex}"
            started = time.perf_counter()
            try:
                adapter = self.adapters.get(target.profile.protocol)
                chat = getattr(adapter, "chat", None)
                if not callable(chat):
                    raise ModelProviderError(
                        ProviderErrorCode.UNSUPPORTED_PROTOCOL,
                        f"provider protocol cannot serve chat: {target.profile.protocol.value}",
                    )
                target_kwargs = dict(kwargs)
                if not target.model.capabilities.thinking_control:
                    target_kwargs.pop("thinking", None)
                result = chat(
                    self.broker,
                    target,
                    system=system,
                    user=user,
                    timeout_seconds=timeout,
                    cancellation_token=cancellation,
                    **target_kwargs,
                )
                self._trace(
                    invocation_id,
                    snapshot,
                    target,
                    ordinal=ordinal,
                    correlation_id=correlation_id,
                    status="completed",
                    latency_ms=(time.perf_counter() - started) * 1000,
                    usage=result.get("usage"),
                    provider_request_id=(result.get("raw") or {}).get("provider_request_id"),
                )
                result.setdefault("raw", {})["provider_profile_id"] = target.profile.profile_id
                result["raw"]["workload"] = workload.value
                result["raw"]["routing_snapshot_id"] = snapshot.snapshot_id
                return result
            except ModelProviderError as exc:
                last_error = exc
                self._trace(
                    invocation_id,
                    snapshot,
                    target,
                    ordinal=ordinal,
                    correlation_id=correlation_id,
                    status="failed",
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error=exc,
                )
                if not exc.retryable or ordinal + 1 >= len(targets):
                    raise
        assert last_error is not None
        raise last_error

    def stream_chat(self, system: str, user: str, **kwargs: Any) -> Iterator[JsonDict]:
        workload = self._workload(kwargs.pop("workload", None), user)
        turn_id = kwargs.pop("_sora_turn_id", None)
        correlation_id = kwargs.pop("correlation_id", None)
        requested_model = kwargs.pop("model", None)
        snapshot = self.router.resolve(workload, turn_id=turn_id)
        self._check_requested_model(snapshot, requested_model)
        timeout = float(kwargs.pop("timeout", self.timeout_seconds))
        cancellation = kwargs.pop("cancellation_token", None)
        targets = (snapshot.primary, *snapshot.fallbacks)
        for ordinal, target in enumerate(targets):
            invocation_id = f"pi_{uuid.uuid4().hex}"
            started = time.perf_counter()
            emitted = False
            usage: JsonDict = {}
            provider_request_id: str | None = None
            try:
                adapter = self.adapters.get(target.profile.protocol)
                stream = getattr(adapter, "stream_chat", None)
                if not callable(stream):
                    raise ModelProviderError(
                        ProviderErrorCode.UNSUPPORTED_PROTOCOL,
                        f"provider protocol cannot stream chat: {target.profile.protocol.value}",
                    )
                target_kwargs = dict(kwargs)
                if not target.model.capabilities.thinking_control:
                    target_kwargs.pop("thinking", None)
                for event in stream(
                    self.broker,
                    target,
                    system=system,
                    user=user,
                    timeout_seconds=timeout,
                    cancellation_token=cancellation,
                    **target_kwargs,
                ):
                    emitted = emitted or bool(event.get("delta"))
                    if event.get("usage"):
                        usage = dict(event["usage"])
                    if event.get("provider_request_id"):
                        provider_request_id = str(event["provider_request_id"])
                    yield event
                self._trace(
                    invocation_id,
                    snapshot,
                    target,
                    ordinal=ordinal,
                    correlation_id=correlation_id,
                    status="completed",
                    latency_ms=(time.perf_counter() - started) * 1000,
                    usage=usage,
                    provider_request_id=provider_request_id,
                )
                return
            except ModelProviderError as exc:
                self._trace(
                    invocation_id,
                    snapshot,
                    target,
                    ordinal=ordinal,
                    correlation_id=correlation_id,
                    status="failed",
                    latency_ms=(time.perf_counter() - started) * 1000,
                    usage=usage,
                    error=exc,
                    provider_request_id=provider_request_id,
                )
                if emitted or not exc.retryable or ordinal + 1 >= len(targets):
                    raise

    def _trace(
        self,
        invocation_id: str,
        snapshot: RoutingSnapshot,
        target: ResolvedTarget,
        *,
        ordinal: int,
        correlation_id: str | None,
        status: str,
        latency_ms: float,
        usage: Any = None,
        error: ModelProviderError | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        usage_data = dict(usage or {})
        prompt_tokens = usage_data.get("prompt_tokens")
        if prompt_tokens is None:
            prompt_tokens = usage_data.get("input_tokens")
        completion_tokens = usage_data.get("completion_tokens")
        if completion_tokens is None:
            completion_tokens = usage_data.get("output_tokens")
        self.registry.record_invocation(
            {
                "invocation_id": invocation_id,
                "correlation_id": correlation_id,
                "workload": snapshot.workload.value,
                "provider_profile_id": target.profile.profile_id,
                "model_id": target.model.model_id,
                "routing_revision": snapshot.registry_revision,
                "status": status,
                "latency_ms": round(latency_ms, 2),
                "prompt_tokens": int(prompt_tokens) if prompt_tokens is not None else None,
                "completion_tokens": int(completion_tokens) if completion_tokens is not None else None,
                "retry_count": max(0, ordinal),
                "fallback_ordinal": ordinal,
                "error_code": error.code.value if error else None,
                "provider_request_id": provider_request_id or (error.provider_request_id if error else None),
            }
        )

    @staticmethod
    def _check_requested_model(snapshot: RoutingSnapshot, requested_model: Any) -> None:
        if requested_model and str(requested_model) != snapshot.primary.model.model_id:
            raise ModelProviderError(
                ProviderErrorCode.CAPABILITY_MISMATCH,
                "caller model override conflicts with the verified workload binding",
            )

    @staticmethod
    def _workload(explicit: Any, user_payload: str) -> Workload:
        if explicit is not None:
            return Workload(str(explicit))
        task = ""
        try:
            payload = json.loads(user_payload)
            if isinstance(payload, dict):
                task = str(payload.get("task") or "").strip().lower()
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        task_routes = {
            "interpret_turn": Workload.TURN_INTERPRETATION,
            "score_candidate_signals": Workload.SEMANTIC_ARBITRATION,
            "resolve_interruptibility": Workload.INTERRUPTIBILITY_RESOLUTION,
            "extract_relationship_evidence": Workload.RELATIONSHIP_EVIDENCE,
            "adjudicate_relationship_candidate_entailment_and_coverage": Workload.RELATIONSHIP_EVIDENCE,
            "adjudicate_explicit_romantic_intent": Workload.RELATIONSHIP_EVIDENCE,
            "repair_relationship_json": Workload.RELATIONSHIP_EVIDENCE,
        }
        if task in task_routes:
            return task_routes[task]
        lowered = user_payload.lower()
        if "context_pack" in lowered:
            return Workload.RESPONSE_GENERATION
        return Workload.DOMAIN_BACKGROUND
