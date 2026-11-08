from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .memory_extractor_provider import MemoryExtractorProvider, OpenAICompatibleExtractorProvider
from .memory_extractor_worker import AsyncMemoryExtractorWorker
from .memory_provider_routes import RoutedMemoryExtractorProvider
from .memory_schema import JsonDict, utc_now
from .model_provider import Workload

if TYPE_CHECKING:
    from .memory_runtime import MemoryRuntime


@dataclass
class MemoryExtractionScheduler:
    runtime: MemoryRuntime
    poll_seconds: float = 1.0
    lease_seconds: int = 120
    _wake: threading.Event = field(default_factory=threading.Event, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _state_lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _state: JsonDict = field(default_factory=lambda: {"status": "stopped", "processed": 0}, init=False)

    def __post_init__(self) -> None:
        if self.poll_seconds <= 0 or self.lease_seconds < 1:
            raise ValueError("memory scheduler intervals must be positive")

    def start(self) -> None:
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(target=self._run, name="memory-extraction", daemon=True)
            self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, *, timeout: float = 30.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("memory extraction still draining; dependent stores must remain open")

    def snapshot(self) -> JsonDict:
        with self._state_lock:
            result = dict(self._state)
            result["running"] = bool(self._thread and self._thread.is_alive())
        with self.runtime._lock:
            result["queue"] = {row["status"]: row["count"] for row in self.runtime._conn.execute(
                "SELECT status, COUNT(*) AS count FROM extraction_job GROUP BY status",
            ).fetchall()}
        return result

    def provider(self) -> MemoryExtractorProvider | None:
        system = self.runtime.model_provider_system
        if system is not None:
            if not system.router.has_binding(Workload.MEMORY_EXTRACTION):
                return None
            system.router.resolve(Workload.MEMORY_EXTRACTION)
            return RoutedMemoryExtractorProvider(system.llm)
        if self.runtime.extractor_provider is not None:
            return self.runtime.extractor_provider
        try:
            return OpenAICompatibleExtractorProvider.from_env()
        except RuntimeError:
            return None

    def _record(self, status: str, **fields: object) -> None:
        with self._state_lock:
            self._state.update(status=status, updated_at=utc_now(), **fields)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._wake.clear()
                try:
                    provider = self.provider()
                    if provider is None:
                        self._record("waiting_provider")
                    else:
                        self._record("polling")
                        result = AsyncMemoryExtractorWorker(
                            self.runtime, provider=provider, lease_seconds=self.lease_seconds,
                        ).run_once()
                        self._record(result["status"], last_error=result.get("error"))
                        if result["status"] == "completed":
                            with self._state_lock:
                                self._state["processed"] += 1
                            continue
                except Exception as exc:
                    self._record("error", last_error=type(exc).__name__)
                self._wake.wait(self.poll_seconds)
        finally:
            self._record("stopped")
