from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


JsonDict = dict[str, Any]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderKind(StrEnum):
    SORA_MANAGED = "sora_managed"
    OPENAI = "openai"
    DEEPSEEK = "deepseek"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    OPENAI_COMPATIBLE = "openai_compatible"
    OLLAMA = "ollama"
    HTTP_RERANK = "http_rerank"


class ProviderProtocol(StrEnum):
    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    GEMINI_NATIVE = "gemini_native"
    OPENAI_EMBEDDINGS = "openai_embeddings"
    HTTP_RERANK = "http_rerank"


class ProfileStatus(StrEnum):
    DRAFT = "draft"
    CREDENTIAL_BOUND = "credential_bound"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    DEGRADED = "degraded"
    DISABLED = "disabled"
    DELETED = "deleted"


class EndpointMode(StrEnum):
    REMOTE = "remote"
    LOCAL = "local"


class Capability(StrEnum):
    CHAT = "chat"
    STREAMING = "streaming"
    STRUCTURED_OUTPUT = "structured_output"
    THINKING_CONTROL = "thinking_control"
    CANCELLATION = "cancellation"
    USAGE_REPORTING = "usage_reporting"
    EMBEDDING = "embedding"
    RERANK = "rerank"


class Workload(StrEnum):
    TURN_INTERPRETATION = "turn_interpretation"
    SEMANTIC_ARBITRATION = "semantic_arbitration"
    RESPONSE_GENERATION = "response_generation"
    INTERRUPTIBILITY_RESOLUTION = "interruptibility_resolution"
    MEMORY_EXTRACTION = "memory_extraction"
    MEMORY_RECALL_PLANNING = "memory_recall_planning"
    MEMORY_SURFACE = "memory_surface"
    MEMORY_CONSOLIDATION = "memory_consolidation"
    PERSONA_CANDIDATE = "persona_candidate"
    AFFECT_APPRAISAL = "affect_appraisal"
    RELATIONSHIP_EVIDENCE = "relationship_evidence"
    WORLDOPS_INTERPRETATION = "worldops_interpretation"
    DOMAIN_BACKGROUND = "domain_background"
    EMBEDDING_DOCUMENTS = "embedding_documents"
    EMBEDDING_QUERY = "embedding_query"
    MEMORY_RERANK = "memory_rerank"


class OwnerScope(StrictModel):
    tenant_id: str = "local"
    user_id: str = "local-user"
    agent_id: str = "sora_default"

    @field_validator("tenant_id", "user_id", "agent_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("owner scope values cannot be empty")
        if len(normalized) > 256:
            raise ValueError("owner scope value is too long")
        return normalized


class ProviderEndpoint(StrictModel):
    base_url: str
    mode: EndpointMode = EndpointMode.REMOTE
    custom: bool = False

    @field_validator("base_url")
    @classmethod
    def _base_url_non_empty(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized:
            raise ValueError("provider endpoint cannot be empty")
        if len(normalized) > 2048:
            raise ValueError("provider endpoint is too long")
        return normalized


class ProviderProfile(StrictModel):
    profile_id: str
    owner: OwnerScope
    display_name: str
    provider_kind: ProviderKind
    protocol: ProviderProtocol
    endpoint: ProviderEndpoint
    credential_ref: str | None = None
    auth_scheme: str = "bearer"
    status: ProfileStatus = ProfileStatus.DRAFT
    capability_revision: int = 0
    policy_revision: int = 1
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    @field_validator("profile_id", "display_name", "auth_scheme")
    @classmethod
    def _profile_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("provider profile text cannot be empty")
        return normalized


class ModelCapabilities(StrictModel):
    chat: bool = False
    streaming: bool = False
    structured_output: bool = False
    thinking_control: bool = False
    cancellation: bool = False
    usage_reporting: bool = False
    context_window: int | None = None
    max_output_tokens: int | None = None
    embedding: bool = False
    embedding_dimension: int | None = None
    rerank: bool = False

    @model_validator(mode="after")
    def _validate_dimensions(self) -> "ModelCapabilities":
        if self.embedding and (self.embedding_dimension or 0) < 1:
            raise ValueError("embedding capability requires a positive dimension")
        if not self.embedding and self.embedding_dimension is not None:
            raise ValueError("embedding dimension requires embedding capability")
        for name in ("context_window", "max_output_tokens"):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} must be positive")
        return self

    def supports(self, requirement: Capability) -> bool:
        mapping = {
            Capability.CHAT: self.chat,
            Capability.STREAMING: self.streaming,
            Capability.STRUCTURED_OUTPUT: self.structured_output,
            Capability.THINKING_CONTROL: self.thinking_control,
            Capability.CANCELLATION: self.cancellation,
            Capability.USAGE_REPORTING: self.usage_reporting,
            Capability.EMBEDDING: self.embedding,
            Capability.RERANK: self.rerank,
        }
        return bool(mapping[requirement])


class ModelDescriptor(StrictModel):
    provider_profile_id: str
    model_id: str
    display_name: str | None = None
    capabilities: ModelCapabilities
    source: str = "probe"
    revision: int = 1
    verified_at: str | None = None
    expires_at: str | None = None

    @field_validator("provider_profile_id", "model_id", "source")
    @classmethod
    def _model_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("model descriptor text cannot be empty")
        return normalized


class ModelTarget(StrictModel):
    provider_profile_id: str
    model_id: str


class WorkloadBinding(StrictModel):
    binding_id: str
    owner: OwnerScope
    workload: Workload
    primary: ModelTarget
    fallbacks: tuple[ModelTarget, ...] = ()
    requirements: tuple[Capability, ...] = ()
    cross_provider_fallback: bool = False
    revision: int = 1
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_fallbacks(self) -> "WorkloadBinding":
        targets = (self.primary, *self.fallbacks)
        keys = [(item.provider_profile_id, item.model_id) for item in targets]
        if len(keys) != len(set(keys)):
            raise ValueError("workload binding contains duplicate targets")
        if not self.cross_provider_fallback:
            foreign = [
                item
                for item in self.fallbacks
                if item.provider_profile_id != self.primary.provider_profile_id
            ]
            if foreign:
                raise ValueError("cross-provider fallback requires explicit authorization")
        return self


class WorkloadCompatibility(StrictModel):
    workload: Workload
    requirements: tuple[Capability, ...]
    missing_capabilities: tuple[Capability, ...]


class ProviderRoutingSetupResult(StrictModel):
    schema_version: str = "ProviderRoutingSetupResult.v1"
    profile_id: str
    model: ModelDescriptor
    bound_workloads: tuple[Workload, ...] = ()
    preserved_workloads: tuple[Workload, ...] = ()
    unsupported_workloads: tuple[WorkloadCompatibility, ...] = ()
    missing_conversation_workloads: tuple[Workload, ...] = ()
    ready_for_conversation: bool = False


class ResolvedTarget(StrictModel):
    profile: ProviderProfile
    model: ModelDescriptor


class RoutingSnapshot(StrictModel):
    snapshot_id: str
    turn_id: str | None = None
    owner: OwnerScope
    workload: Workload
    registry_revision: int
    binding_revision: int
    primary: ResolvedTarget
    fallbacks: tuple[ResolvedTarget, ...] = ()
    cross_provider_fallback: bool = False
    created_at: str = Field(default_factory=utc_now)


WORKLOAD_REQUIREMENTS: dict[Workload, tuple[Capability, ...]] = {
    Workload.TURN_INTERPRETATION: (
        Capability.CHAT,
        Capability.STRUCTURED_OUTPUT,
        Capability.THINKING_CONTROL,
    ),
    Workload.SEMANTIC_ARBITRATION: (
        Capability.CHAT,
        Capability.STRUCTURED_OUTPUT,
        Capability.THINKING_CONTROL,
    ),
    Workload.INTERRUPTIBILITY_RESOLUTION: (
        Capability.CHAT,
        Capability.STRUCTURED_OUTPUT,
        Capability.THINKING_CONTROL,
    ),
    Workload.RESPONSE_GENERATION: (
        Capability.CHAT,
        Capability.STREAMING,
        Capability.CANCELLATION,
    ),
    Workload.MEMORY_EXTRACTION: (Capability.CHAT, Capability.STRUCTURED_OUTPUT),
    Workload.MEMORY_RECALL_PLANNING: (Capability.CHAT, Capability.STRUCTURED_OUTPUT),
    Workload.MEMORY_SURFACE: (Capability.CHAT, Capability.STRUCTURED_OUTPUT),
    Workload.MEMORY_CONSOLIDATION: (Capability.CHAT,),
    Workload.PERSONA_CANDIDATE: (Capability.CHAT, Capability.STRUCTURED_OUTPUT),
    Workload.AFFECT_APPRAISAL: (Capability.CHAT, Capability.STRUCTURED_OUTPUT),
    Workload.RELATIONSHIP_EVIDENCE: (Capability.CHAT, Capability.STRUCTURED_OUTPUT),
    Workload.WORLDOPS_INTERPRETATION: (Capability.CHAT, Capability.STRUCTURED_OUTPUT),
    Workload.DOMAIN_BACKGROUND: (Capability.CHAT,),
    Workload.EMBEDDING_DOCUMENTS: (Capability.EMBEDDING,),
    Workload.EMBEDDING_QUERY: (Capability.EMBEDDING,),
    Workload.MEMORY_RERANK: (Capability.RERANK,),
}


CONVERSATION_REQUIRED_WORKLOADS: tuple[Workload, ...] = (
    Workload.TURN_INTERPRETATION,
    Workload.SEMANTIC_ARBITRATION,
    Workload.RESPONSE_GENERATION,
    Workload.INTERRUPTIBILITY_RESOLUTION,
    Workload.MEMORY_EXTRACTION,
    Workload.MEMORY_RECALL_PLANNING,
    Workload.MEMORY_SURFACE,
    Workload.MEMORY_CONSOLIDATION,
    Workload.PERSONA_CANDIDATE,
    Workload.AFFECT_APPRAISAL,
    Workload.RELATIONSHIP_EVIDENCE,
    Workload.WORLDOPS_INTERPRETATION,
    Workload.DOMAIN_BACKGROUND,
)
