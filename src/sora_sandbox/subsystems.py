from __future__ import annotations

import json
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from typing import Callable, Protocol
from urllib.request import Request, urlopen

from .contracts import JsonDict, SubsystemHealth, TurnContext, validate_signal


SignalBuilder = Callable[[TurnContext], list[JsonDict]]


class SubsystemAdapter(Protocol):
    """Boundary implemented by local modules or remote-service adapters."""

    name: str
    timeout_ms: int

    def evaluate(self, context: TurnContext) -> list[JsonDict]:
        ...

    def propose_updates(self, context: TurnContext) -> list[JsonDict]:
        ...

    def commit(self, candidate_ids: list[str]) -> JsonDict:
        ...

    def health(self) -> JsonDict:
        ...


@dataclass
class FunctionSubsystemAdapter:
    name: str
    signal_builder: SignalBuilder
    timeout_ms: int = 250

    def evaluate(self, context: TurnContext) -> list[JsonDict]:
        return self.signal_builder(context)

    def propose_updates(self, context: TurnContext) -> list[JsonDict]:
        return []

    def commit(self, candidate_ids: list[str]) -> JsonDict:
        return {"status": "unsupported", "candidate_ids": candidate_ids}

    def health(self) -> JsonDict:
        return {"status": "ok", "adapter": type(self).__name__}


@dataclass
class HttpSubsystemAdapter:
    """Reference adapter for an independently deployed subsystem service."""

    name: str
    base_url: str
    timeout_ms: int = 800
    auth_token: str | None = None

    def evaluate(self, context: TurnContext) -> list[JsonDict]:
        result = self._post("/evaluate", {"context": context.to_dict()})
        return list(result.get("signals", []))

    def propose_updates(self, context: TurnContext) -> list[JsonDict]:
        result = self._post("/propose-updates", {"context": context.to_dict()})
        return list(result.get("candidates", []))

    def commit(self, candidate_ids: list[str]) -> JsonDict:
        return self._post("/commit", {"candidate_ids": candidate_ids})

    def health(self) -> JsonDict:
        return self._post("/health", {})

    def _post(self, path: str, payload: JsonDict) -> JsonDict:
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=max(self.timeout_ms, 1) / 1000) as response:
            return json.loads(response.read().decode("utf-8"))


class SubsystemRegistry:
    def __init__(self, adapters: list[SubsystemAdapter] | None = None) -> None:
        self._adapters: dict[str, SubsystemAdapter] = {}
        for adapter in adapters or []:
            self.register(adapter)

    def register(self, adapter: SubsystemAdapter, *, replace: bool = False) -> None:
        if adapter.name in self._adapters and not replace:
            raise ValueError(f"subsystem already registered: {adapter.name}")
        self._adapters[adapter.name] = adapter

    def names(self) -> list[str]:
        return list(self._adapters)

    def collect(self, context: TurnContext) -> tuple[list[JsonDict], list[JsonDict]]:
        if not self._adapters:
            return [], []

        executor = ThreadPoolExecutor(
            max_workers=len(self._adapters),
            thread_name_prefix="sora-subsystem",
        )
        submitted: dict[
            str,
            tuple[SubsystemAdapter, Future[tuple[list[JsonDict], float]], float],
        ] = {}
        for name, adapter in self._adapters.items():
            started = time.perf_counter()
            submitted[name] = (
                adapter,
                executor.submit(_evaluate_adapter, adapter, context),
                started,
            )

        signals: list[JsonDict] = []
        health: list[JsonDict] = []
        try:
            for name, (adapter, future, started) in submitted.items():
                try:
                    raw_signals, adapter_latency = future.result(
                        timeout=max(adapter.timeout_ms, 1) / 1000
                    )
                    normalized = [validate_signal(signal, name) for signal in raw_signals]
                    signals.extend(normalized)
                    health.append(
                        SubsystemHealth(
                            subsystem=name,
                            status="ok",
                            latency_ms=adapter_latency,
                            adapter=type(adapter).__name__,
                            signal_count=len(normalized),
                        ).to_dict()
                    )
                except TimeoutError:
                    future.cancel()
                    health.append(
                        SubsystemHealth(
                            subsystem=name,
                            status="timeout",
                            latency_ms=_elapsed_ms(started),
                            adapter=type(adapter).__name__,
                            detail=f"exceeded {adapter.timeout_ms}ms; continued without this subsystem",
                        ).to_dict()
                    )
                except Exception as exc:
                    health.append(
                        SubsystemHealth(
                            subsystem=name,
                            status="degraded",
                            latency_ms=_elapsed_ms(started),
                            adapter=type(adapter).__name__,
                            detail=f"{type(exc).__name__}: {exc}",
                        ).to_dict()
                    )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return signals, health


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _evaluate_adapter(
    adapter: SubsystemAdapter,
    context: TurnContext,
) -> tuple[list[JsonDict], float]:
    started = time.perf_counter()
    signals = adapter.evaluate(context)
    return signals, _elapsed_ms(started)
