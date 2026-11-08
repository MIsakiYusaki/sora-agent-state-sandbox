from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any

from .affect.provider import LLMAffectAppraisalProvider
from .affect.system import AffectSystem
from .agent import (
    AgentRunner,
    build_default_subsystem_registry,
    build_explicit_relationship_outcome,
)
from .conversation_runtime import ConversationRuntime
from .conversation_runtime.runtime import ConversationRuntimeConfig
from .interruptibility import (
    InterruptibilityService,
    LLMInterruptibilityResolutionProvider,
)
from .interruptibility_eval import run_interruptibility_eval
from .memory_system import MemorySystem
from .product_migrations import retire_untouched_example_memories
from .persona.provider import LLMPersonaCandidateProvider
from .persona.system import PersonaSystem
from .relationship.provider import LLMRelationshipEvidenceProvider
from .relationship.system import RelationshipAffectAdapter, RelationshipSystem
from .worldops.provider import LLMWorldOpsInterpretationProvider
from .worldops.contracts import IdentityScope
from .worldops.system import WorldOpsSystem


@dataclass
class SoraRuntimeStack:
    """Single composition root shared by the REPL and conversation gateway."""

    memory: MemorySystem
    interruptibility: InterruptibilityService
    persona: PersonaSystem
    affect: AffectSystem
    relationship: RelationshipSystem
    worldops: WorldOpsSystem
    runner: AgentRunner
    conversation: ConversationRuntime
    model_provider_system: Any | None = None

    @classmethod
    def build(
        cls,
        llm: Any,
        *,
        start_workers: bool = True,
        owner_user_id: str = "local-user",
        agent_id: str = "sora_default",
        tenant_id: str = "local",
        timeline_id: str | None = None,
    ) -> "SoraRuntimeStack":
        resources: list[Any] = []
        model_provider_system = getattr(llm, "provider_system", None)
        if model_provider_system is not None:
            resources.append(model_provider_system)
        try:
            memory = MemorySystem.default(
                owner_user_id=owner_user_id,
                agent_id=agent_id,
                model_provider_system=model_provider_system,
            )
            resources.append(memory)
            interruptibility = InterruptibilityService(
                provider=LLMInterruptibilityResolutionProvider(llm),
            )
            persona = PersonaSystem.default(
                provider=LLMPersonaCandidateProvider(llm),
                start_worker=start_workers,
                agent_id=agent_id,
            )
            resources.append(persona)
            affect = AffectSystem.default(
                provider=LLMAffectAppraisalProvider(llm),
                memory_runtime=memory,
                start_worker=start_workers,
                rollout_mode=os.environ.get("SORA_AFFECT_MODE", "active")
                .strip()
                .lower(),
                sora_id=agent_id,
            )
            resources.append(affect)
            relationship = RelationshipSystem.default(
                provider=LLMRelationshipEvidenceProvider(llm),
                start_worker=start_workers,
                rollout_mode=os.environ.get("SORA_RELATIONSHIP_MODE", "active")
                .strip()
                .lower(),
                sora_id=agent_id,
            )
            resources.append(relationship)
            actual_timeline_id = timeline_id or (
                "timeline."
                + hashlib.sha256(
                    f"{tenant_id}\x1f{owner_user_id}\x1f{agent_id}".encode("utf-8")
                ).hexdigest()[:24]
            )
            worldops = WorldOpsSystem.default(
                provider=LLMWorldOpsInterpretationProvider.from_environment(llm),
                memory_runtime=memory,
                start_worker=start_workers,
                identity=IdentityScope(
                    tenant_id=tenant_id,
                    user_id=owner_user_id,
                    sora_id=agent_id,
                    timeline_id=actual_timeline_id,
                ),
            )
            resources.append(worldops)
        except Exception:
            for resource in reversed(resources):
                try:
                    resource.close()
                except Exception:
                    pass
            raise
        try:
            relationship.register_domain_event_consumer(
                "affect",
                RelationshipAffectAdapter(affect, relationship).deliver,
            )
            memory.register_memory_change_listener("affect", affect.handle_memory_change)
            memory.projector_bus.register_adapter("persona", persona.projection_adapter)
            memory.projector_bus.register_adapter(
                "relationship", relationship.projection_adapter
            )
            memory.projector_bus.register_adapter("worldops", worldops.projection_adapter)
            # Recovery must run only after every real domain owner is attached.
            memory.process_memory_changes()
            retire_untouched_example_memories(memory)
            runner = AgentRunner(
                llm,
                subsystem_registry=build_default_subsystem_registry(
                    persistent_executor=True
                ),
                memory_runtime=memory,
                interruptibility=interruptibility,
                persona_runtime=persona,
                affect_runtime=affect,
                relationship_runtime=relationship,
                worldops_runtime=worldops,
            )
            conversation = ConversationRuntime.build(
                runner,
                llm=llm,
                memory_runtime=memory,
                affect_runtime=affect,
                relationship_runtime=relationship,
                worldops_runtime=worldops,
                relationship_outcome_builder=build_explicit_relationship_outcome,
                config=ConversationRuntimeConfig(start_post_turn_worker=start_workers),
            )
            resources.append(conversation)
            stack = cls(
                memory=memory,
                interruptibility=interruptibility,
                persona=persona,
                affect=affect,
                relationship=relationship,
                worldops=worldops,
                runner=runner,
                conversation=conversation,
                model_provider_system=getattr(llm, "provider_system", None),
            )
            stack.register_quality_ops()
            if start_workers:
                memory.start()
            return stack
        except Exception:
            for resource in reversed(resources):
                try:
                    resource.close()
                except Exception:
                    pass
            raise

    def register_quality_ops(self) -> None:
        if self.model_provider_system is not None:
            self.model_provider_system.register_quality_ops(self.memory)
        self.memory.register_quality_eval_suite(
            "interruptibility",
            run_interruptibility_eval,
            source_module="interruptibility_system",
        )
        self.memory.register_quality_export_section(
            "interruptibility",
            lambda: self.interruptibility.export_traces({}, limit=500),
        )
        self.persona.register_quality_ops(self.memory)
        self.affect.register_quality_ops(self.memory)
        self.relationship.register_quality_ops(self.memory)
        self.worldops.register_quality_ops(self.memory)
        self.conversation.register_quality_ops(self.memory)

    def reset(self) -> None:
        running = self.memory.extraction_scheduler.snapshot()["running"]
        self.memory.stop()
        try:
            self.conversation.reset()
            self.memory.reset()
            self.persona.reset()
            self.affect.reset()
            self.relationship.reset()
            self.worldops.reset()
        finally:
            if running:
                self.memory.start()

    def close(self) -> None:
        # Extraction can publish to domain adapters. Drain it before their stores
        # close; on timeout they must remain usable for an in-flight call.
        self.conversation.close()
        self.memory.stop()
        try:
            self.worldops.close()
        finally:
            try:
                self.relationship.close()
            finally:
                try:
                    self.affect.close()
                finally:
                    try:
                        self.persona.close()
                    finally:
                        try:
                            self.memory.close()
                        finally:
                            if self.model_provider_system is not None:
                                self.model_provider_system.close()
