from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .conversation_gateway.contracts import (
    AckCommand,
    CancelCommand,
    CancelRequest,
    ConnectionReady,
    GatewayBootstrap,
    GatewayError,
    GatewayEvent,
    HistoryPage,
    PingCommand,
    ResumeCommand,
    SubmitCommand,
    TurnAccepted,
    TurnStatusView,
    TurnSubmit,
    validate_identifier,
)
from .model_provider.admin import (
    BindWorkloadRequest,
    CreateProfileRequest,
    DeleteProfileRequest,
    SetupModelRoutesRequest,
    VerifyModelRequest,
)
from .model_provider.contracts import (
    ModelCapabilities,
    ModelDescriptor,
    ProviderRoutingSetupResult,
    ProviderProfile,
    WorkloadBinding,
)


class DesktopContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ShellErrorCode(StrEnum):
    GATEWAY_UNAVAILABLE = "GATEWAY_UNAVAILABLE"
    GATEWAY_RESTARTING = "GATEWAY_RESTARTING"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    PROTOCOL_MISMATCH = "PROTOCOL_MISMATCH"
    INVALID_REQUEST = "INVALID_REQUEST"
    TURN_CONFLICT = "TURN_CONFLICT"
    PROVIDER_REQUIRED = "PROVIDER_REQUIRED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    CREDENTIAL_CANCELLED = "CREDENTIAL_CANCELLED"
    CREDENTIAL_REJECTED = "CREDENTIAL_REJECTED"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ShellError(DesktopContractModel):
    schema_version: Literal["ShellError.v1"] = "ShellError.v1"
    code: ShellErrorCode
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = False
    retry_after_ms: int | None = Field(default=None, ge=1, le=3_600_000)
    correlation_id: str | None = None
    action: Literal[
        "none",
        "retry",
        "open_provider_settings",
        "restart_gateway",
        "reload_conversation",
    ] = "none"
    safe_details: dict[str, Any] = Field(default_factory=dict)


class DesktopStatus(DesktopContractModel):
    schema_version: Literal["DesktopStatus.v1"] = "DesktopStatus.v1"
    phase: Literal[
        "starting",
        "provider_setup_required",
        "ready",
        "degraded",
        "restarting",
        "stopped",
    ]
    profile_id: str
    client_instance_id: str
    protocol_version: Literal["1.0"] = "1.0"
    gateway_ready: bool
    provider_ready: bool
    recoverable: bool = True
    status_message: str | None = Field(default=None, max_length=240)


class DesktopSubmitTurnRequest(DesktopContractModel):
    schema_version: Literal["DesktopSubmitTurnRequest.v1"] = (
        "DesktopSubmitTurnRequest.v1"
    )
    conversation_id: str
    text: str = Field(min_length=1, max_length=32_000)
    branch_id: str = "main"
    expected_conversation_version: int | None = Field(default=None, ge=0)

    @field_validator("conversation_id", "branch_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_identifier(value)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if "\x00" in value or not value.strip():
            raise ValueError("text must contain visible content and no NUL")
        return value


class DesktopCancelTurnRequest(DesktopContractModel):
    schema_version: Literal["DesktopCancelTurnRequest.v1"] = (
        "DesktopCancelTurnRequest.v1"
    )
    conversation_id: str
    turn_id: str
    reason: str = Field(default="user_cancelled", min_length=1, max_length=120)

    @field_validator("conversation_id", "turn_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_identifier(value)


class DesktopHistoryRequest(DesktopContractModel):
    schema_version: Literal["DesktopHistoryRequest.v1"] = (
        "DesktopHistoryRequest.v1"
    )
    conversation_id: str
    limit: int = Field(default=50, ge=1, le=200)
    before_sequence: int | None = Field(default=None, ge=1)

    @field_validator("conversation_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_identifier(value)


class DesktopProviderCredentialPromptRequest(DesktopContractModel):
    """Secret-free request sent by WebView to the native credential prompt."""

    schema_version: Literal["DesktopProviderCredentialPromptRequest.v1"] = (
        "DesktopProviderCredentialPromptRequest.v1"
    )
    profile_id: str
    verification: VerifyModelRequest

    @field_validator("profile_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_identifier(value)


class DesktopProviderRotatePromptRequest(DesktopContractModel):
    """Secret-free rotation request; the old credential remains active on failure."""

    schema_version: Literal["DesktopProviderRotatePromptRequest.v1"] = (
        "DesktopProviderRotatePromptRequest.v1"
    )
    profile_id: str
    verification: VerifyModelRequest

    @field_validator("profile_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_identifier(value)


class DesktopProviderSetupOutcome(DesktopContractModel):
    schema_version: Literal["DesktopProviderSetupOutcome.v1"] = (
        "DesktopProviderSetupOutcome.v1"
    )
    profile: ProviderProfile
    routing: ProviderRoutingSetupResult


class DesktopEventEnvelope(DesktopContractModel):
    schema_version: Literal["DesktopEventEnvelope.v1"] = "DesktopEventEnvelope.v1"
    kind: Literal["gateway_event", "shell_status", "shell_error"]
    conversation_id: str | None = None
    turn_id: str | None = None
    payload: dict[str, Any]


class DesktopContractBundle(DesktopContractModel):
    """Schema-only root that keeps every Desktop boundary type in one graph."""

    schema_version: Literal["DesktopContractBundle.v1"] = "DesktopContractBundle.v1"
    desktop_status: DesktopStatus
    desktop_submit_turn_request: DesktopSubmitTurnRequest
    desktop_cancel_turn_request: DesktopCancelTurnRequest
    desktop_history_request: DesktopHistoryRequest
    desktop_provider_credential_prompt_request: DesktopProviderCredentialPromptRequest
    desktop_provider_rotate_prompt_request: DesktopProviderRotatePromptRequest
    desktop_provider_setup_outcome: DesktopProviderSetupOutcome
    desktop_event_envelope: DesktopEventEnvelope
    shell_error: ShellError
    gateway_bootstrap: GatewayBootstrap
    gateway_error: GatewayError
    gateway_event: GatewayEvent
    turn_submit: TurnSubmit
    turn_accepted: TurnAccepted
    turn_status: TurnStatusView
    cancel_request: CancelRequest
    history_page: HistoryPage
    connection_ready: ConnectionReady
    submit_command: SubmitCommand
    cancel_command: CancelCommand
    resume_command: ResumeCommand
    ack_command: AckCommand
    ping_command: PingCommand
    provider_create_request: CreateProfileRequest
    provider_verify_request: VerifyModelRequest
    provider_setup_routes_request: SetupModelRoutesRequest
    provider_bind_request: BindWorkloadRequest
    provider_delete_request: DeleteProfileRequest
    provider_profile: ProviderProfile
    provider_model: ModelDescriptor
    provider_capabilities: ModelCapabilities
    provider_binding: WorkloadBinding
    provider_routing_setup_result: ProviderRoutingSetupResult
