from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .memory_runtime import MemoryRuntime
from .memory_extraction_scheduler import MemoryExtractionScheduler
from .memory_lifecycle_scheduler import MemoryLifecycleScheduler
from .memory_schema import JsonDict
from .model_provider import ModelProviderSystem


@dataclass
class MemorySystem:
    """Application-facing facade for the local memory module.

    The runtime remains the SQLite-backed source of truth. This facade gives the
    Agent and REPL one stable dependency while extraction, retrieval, storage,
    and governance keep evolving behind it.
    """

    runtime: MemoryRuntime
    extraction_scheduler: MemoryExtractionScheduler = field(init=False)
    lifecycle_scheduler: MemoryLifecycleScheduler = field(init=False)

    def __post_init__(self) -> None:
        self.extraction_scheduler = MemoryExtractionScheduler(self.runtime)
        self.lifecycle_scheduler = MemoryLifecycleScheduler(self.runtime)
        self.runtime.register_quality_export_section("memory_extraction_scheduler", self.extraction_scheduler.snapshot)
        self.runtime.register_quality_export_section("memory_lifecycle_scheduler", self.lifecycle_scheduler.snapshot)

    def start(self) -> None:
        self.runtime.change_delivery.start()
        self.extraction_scheduler.start()
        self.lifecycle_scheduler.start()

    def stop(self, *, timeout: float = 30.0) -> None:
        self.lifecycle_scheduler.stop(timeout=timeout)
        self.extraction_scheduler.stop(timeout=timeout)
        self.runtime.change_delivery.stop(timeout=timeout)

    @classmethod
    def default(
        cls,
        *,
        owner_user_id: str = "local-user",
        agent_id: str = "sora_default",
        model_provider_system: ModelProviderSystem | None = None,
    ) -> "MemorySystem":
        return cls(
            MemoryRuntime.default(
                owner_user_id=owner_user_id,
                agent_id=agent_id,
                model_provider_system=model_provider_system,
            )
        )

    def close(self) -> None:
        self.stop()
        self.runtime.close()

    def reset(self) -> None:
        running = self.extraction_scheduler.snapshot()["running"]
        self.stop()
        try:
            self.runtime.reset()
        finally:
            if running:
                self.start()

    def observe_committed_turn(self, event: JsonDict) -> JsonDict:
        result = self.runtime.observe_committed_turn(event)
        self.extraction_scheduler.wake()
        return result

    def recall(
        self,
        query_text: str,
        *,
        turn_frame: JsonDict | None = None,
        context_use_policy: JsonDict | None = None,
        context_features: JsonDict | None = None,
        top_k: int = 3,
        token_budget: int | None = None,
    ) -> JsonDict:
        return self.runtime.recall(
            query_text,
            turn_frame=turn_frame,
            context_use_policy=context_use_policy,
            context_features=context_features,
            top_k=top_k,
            token_budget=token_budget,
        )

    def observe_turn_candidates(self, user_input: str, reply: str) -> list[JsonDict]:
        return self.runtime.observe_turn_candidates(user_input, reply)

    def observe_turn(self, user_input: str, reply: str) -> JsonDict | None:
        return self.runtime.observe_turn(user_input, reply)

    def observe_response_plan(self, response_plan: JsonDict) -> JsonDict | None:
        return self.runtime.observe_response_plan(response_plan)

    def import_legacy_memories(self, items: list[JsonDict]) -> int:
        return self.runtime.import_legacy_memories(items)

    def sync_state_cache(self, state: JsonDict) -> None:
        self.runtime.sync_state_cache(state)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.runtime, name)
