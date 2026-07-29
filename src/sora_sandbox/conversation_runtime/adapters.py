from __future__ import annotations

from typing import Any, Callable, Protocol

from .contracts import JsonDict


class GenerationContextGateway(Protocol):
    """Fail-closed prompt projection installed before response planning."""

    def __call__(self, context_pack: JsonDict) -> JsonDict:
        ...


class ConversationPlannerAdapter(Protocol):
    """Temporary planner boundary while frame ownership remains in AgentRunner."""

    llm: Any
    context_pack_transformer: Callable[[JsonDict], JsonDict] | None
    response_max_tokens: int
    defer_domain_observation: bool
    defer_post_turn_writes: bool

    def run(
        self,
        state: JsonDict,
        user_input: str,
        *,
        correlation_id: str | None = None,
    ) -> JsonDict:
        ...


class RelationshipOutcomeBuilder(Protocol):
    def __call__(
        self,
        user_input: str,
        *,
        turn_id: int | str,
        correlation_id: str,
        relationship_frame: JsonDict,
    ) -> JsonDict | None:
        ...
