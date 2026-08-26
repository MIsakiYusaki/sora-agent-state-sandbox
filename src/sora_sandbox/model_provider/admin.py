from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .contracts import (
    Capability,
    EndpointMode,
    ModelTarget,
    ProviderKind,
    ProviderProtocol,
    Workload,
)
from .secrets import SecretMaterial
from .system import ModelProviderSystem


class AdminModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProfileRequest(AdminModel):
    display_name: str
    provider_kind: ProviderKind
    protocol: ProviderProtocol
    base_url: str
    endpoint_mode: EndpointMode = EndpointMode.REMOTE
    custom_endpoint: bool = False
    auth_scheme: Literal[
        "none",
        "bearer",
        "anthropic_api_key",
        "google_api_key",
        "custom_headers",
    ] = "bearer"


class CredentialInput(AdminModel):
    api_key: SecretStr | None = None
    headers: dict[str, SecretStr] = Field(default_factory=dict)

    @field_validator("headers")
    @classmethod
    def _bounded_headers(cls, value: dict[str, SecretStr]) -> dict[str, SecretStr]:
        if len(value) > 16:
            raise ValueError("provider credential contains too many custom headers")
        return value


class VerifyModelRequest(AdminModel):
    model_id: str
    capability_kind: Literal["chat", "embedding", "rerank"] = "chat"
    thinking_control: bool = False
    context_window: int | None = None
    max_output_tokens: int | None = None
    embedding_dimension: int | None = None


class RotateCredentialRequest(AdminModel):
    credential: CredentialInput
    verification: VerifyModelRequest


class WorkloadTargetInput(AdminModel):
    provider_profile_id: str
    model_id: str

    def to_target(self) -> ModelTarget:
        return ModelTarget(
            provider_profile_id=self.provider_profile_id,
            model_id=self.model_id,
        )


class BindWorkloadRequest(AdminModel):
    workload: Workload
    primary: WorkloadTargetInput
    fallbacks: list[WorkloadTargetInput] = Field(default_factory=list, max_length=3)
    requirements: list[Capability] = Field(default_factory=list)
    cross_provider_fallback: bool = False
    expected_revision: int | None = None


class DeleteProfileRequest(AdminModel):
    confirmation: str


class ModelProviderAdminService:
    """Stable local management contract for Desktop Shell and admin tooling.

    Secret-bearing request objects are accepted only at the command boundary and
    are never returned, persisted in SQLite, or included in audit details.
    """

    def __init__(self, system: ModelProviderSystem) -> None:
        self.system = system

    def list_configuration(self) -> dict:
        return self.system.export_redacted()

    def create_profile(self, request: CreateProfileRequest) -> dict:
        if request.provider_kind == ProviderKind.SORA_MANAGED:
            raise ValueError("sora_managed profiles can only be provisioned by the application")
        profile = self.system.create_profile(
            display_name=request.display_name,
            provider_kind=request.provider_kind,
            protocol=request.protocol,
            base_url=request.base_url,
            endpoint_mode=request.endpoint_mode,
            custom_endpoint=request.custom_endpoint,
            auth_scheme=request.auth_scheme,
        )
        return self._profile_view(profile.profile_id)

    def save_credential(self, profile_id: str, request: CredentialInput) -> dict:
        headers = tuple(
            (name, value.get_secret_value()) for name, value in request.headers.items()
        )
        material = SecretMaterial(
            api_key=request.api_key.get_secret_value() if request.api_key else None,
            headers=headers,
        )
        profile = self.system.bind_credential(profile_id, material)
        return self._profile_view(profile.profile_id)

    def rotate_credential(
        self,
        profile_id: str,
        request: RotateCredentialRequest,
    ) -> dict:
        credential = request.credential
        verification = request.verification
        material = SecretMaterial(
            api_key=credential.api_key.get_secret_value() if credential.api_key else None,
            headers=tuple(
                (name, value.get_secret_value())
                for name, value in credential.headers.items()
            ),
        )
        self.system.rotate_credential(
            profile_id,
            material,
            model_id=verification.model_id,
            thinking_control=verification.thinking_control,
            capability_kind=verification.capability_kind,
            embedding_dimension=verification.embedding_dimension,
        )
        return self._profile_view(profile_id)

    def verify_model(self, profile_id: str, request: VerifyModelRequest) -> dict:
        if request.capability_kind == "embedding":
            descriptor = self.system.probe.verify_embedding_model(
                profile_id,
                request.model_id,
                expected_dimension=request.embedding_dimension,
            )
        elif request.capability_kind == "rerank":
            descriptor = self.system.probe.verify_rerank_model(
                profile_id,
                request.model_id,
            )
        else:
            descriptor = self.system.probe.verify_chat_model(
                profile_id,
                request.model_id,
                thinking_control=request.thinking_control,
                context_window=request.context_window,
                max_output_tokens=request.max_output_tokens,
            )
        return descriptor.model_dump(mode="json")

    def bind_workload(self, request: BindWorkloadRequest) -> dict:
        binding = self.system.bind_workload(
            request.workload,
            request.primary.to_target(),
            fallbacks=tuple(item.to_target() for item in request.fallbacks),
            requirements=tuple(request.requirements),
            cross_provider_fallback=request.cross_provider_fallback,
            expected_revision=request.expected_revision,
        )
        return binding.model_dump(mode="json")

    def delete_profile(self, profile_id: str, request: DeleteProfileRequest) -> dict:
        expected = f"delete:{profile_id}"
        if request.confirmation != expected:
            raise ValueError(f"profile deletion requires confirmation value: {expected}")
        self.system.delete_profile(profile_id)
        return {"profile_id": profile_id, "status": "deleted"}

    def health(self) -> dict:
        return self.system.health()

    def maintain(self) -> dict:
        return {"deleted": self.system.maintain()}

    def _profile_view(self, profile_id: str) -> dict:
        profile = self.system.registry.get_profile(profile_id)
        payload = profile.model_dump(mode="json")
        payload["credential_ref"] = "bound" if profile.credential_ref else None
        return payload
