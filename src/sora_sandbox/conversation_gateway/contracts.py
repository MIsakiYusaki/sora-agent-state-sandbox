from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


JsonDict = dict[str, Any]
ProtocolVersion = Literal["1.0"]
PROTOCOL_VERSION: ProtocolVersion = "1.0"
SUPPORTED_PROTOCOL_MINORS = frozenset({"1.0"})

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_CORRELATION_PATTERN = re.compile(r"^[A-Fa-f0-9]{32}$|^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


def validate_identifier(value: str) -> str:
    if not _ID_PATTERN.fullmatch(value):
        raise ValueError("identifier contains unsupported characters or length")
    return value


def validate_correlation_identifier(value: str) -> str:
    if not _CORRELATION_PATTERN.fullmatch(value):
        raise ValueError("invalid correlation_id")
    return value


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class GatewayErrorCode(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    FORBIDDEN = "FORBIDDEN"
    ORIGIN_REJECTED = "ORIGIN_REJECTED"
    PROTOCOL_UNSUPPORTED = "PROTOCOL_UNSUPPORTED"
    INVALID_REQUEST = "INVALID_REQUEST"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    RATE_LIMITED = "RATE_LIMITED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    TURN_IN_PROGRESS = "TURN_IN_PROGRESS"
    TURN_NOT_FOUND = "TURN_NOT_FOUND"
    TURN_TIMEOUT = "TURN_TIMEOUT"
    DRAINING = "DRAINING"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class GatewayError(ProtocolModel):
    schema_version: Literal["GatewayError.v1"] = "GatewayError.v1"
    code: GatewayErrorCode
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = False
    retry_after_ms: int | None = Field(default=None, ge=1, le=3_600_000)
    correlation_id: str | None = None
    details: JsonDict = Field(default_factory=dict)


class TurnSubmit(ProtocolModel):
    schema_version: Literal["TurnSubmit.v1"] = "TurnSubmit.v1"
    protocol_version: ProtocolVersion = PROTOCOL_VERSION
    request_id: str
    conversation_id: str
    client_message_id: str | None = None
    client_instance_id: str
    text: str = Field(min_length=1, max_length=32_000)
    branch_id: str = "main"
    expected_conversation_version: int | None = Field(default=None, ge=0)
    correlation_id: str | None = None

    @field_validator(
        "request_id",
        "conversation_id",
        "client_message_id",
        "client_instance_id",
        "branch_id",
    )
    @classmethod
    def validate_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_identifier(value)

    @field_validator("correlation_id")
    @classmethod
    def validate_correlation_id(cls, value: str | None) -> str | None:
        if value is not None and not _CORRELATION_PATTERN.fullmatch(value):
            validate_correlation_identifier(value)
        return value

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("text contains NUL")
        if not value.strip():
            raise ValueError("text must contain visible content")
        return value


class TurnAccepted(ProtocolModel):
    schema_version: Literal["TurnAccepted.v1"] = "TurnAccepted.v1"
    protocol_version: ProtocolVersion = PROTOCOL_VERSION
    turn_id: str
    request_id: str
    conversation_id: str
    correlation_id: str
    status: Literal["accepted", "committed", "cancelled", "failed"]
    conversation_version: int = Field(ge=0)
    idempotent_replay: bool = False
    events_url: str


class CancelRequest(ProtocolModel):
    schema_version: Literal["CancelRequest.v1"] = "CancelRequest.v1"
    protocol_version: ProtocolVersion = PROTOCOL_VERSION
    reason: str = Field(default="user_cancelled", min_length=1, max_length=120)


class GatewayEvent(ProtocolModel):
    schema_version: Literal["GatewayEvent.v1"] = "GatewayEvent.v1"
    protocol_version: ProtocolVersion = PROTOCOL_VERSION
    turn_id: str
    sequence: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=80)
    correlation_id: str
    payload: JsonDict = Field(default_factory=dict)
    created_at_ms: int = Field(ge=0)
    replayed: bool = False


class TurnStatusView(ProtocolModel):
    schema_version: Literal["TurnStatusView.v1"] = "TurnStatusView.v1"
    protocol_version: ProtocolVersion = PROTOCOL_VERSION
    turn_id: str
    request_id: str
    conversation_id: str
    correlation_id: str
    runtime_status: str
    product_status: Literal["active", "committed", "cancelled", "failed"]
    stage: str
    revision: int = Field(ge=1)
    visible_output_started: bool
    created_at_ms: int = Field(ge=0)
    updated_at_ms: int = Field(ge=0)
    committed_at_ms: int | None = Field(default=None, ge=0)


class HistoryItem(ProtocolModel):
    role: Literal["user", "sora"]
    content: str
    turn_id: str
    sequence: int = Field(ge=1)
    created_at_ms: int = Field(ge=0)


class HistoryPage(ProtocolModel):
    schema_version: Literal["HistoryPage.v1"] = "HistoryPage.v1"
    protocol_version: ProtocolVersion = PROTOCOL_VERSION
    conversation_id: str
    conversation_version: int = Field(ge=0)
    items: tuple[HistoryItem, ...]
    next_before_sequence: int | None = Field(default=None, ge=1)


class ConnectionReady(ProtocolModel):
    schema_version: Literal["ConnectionReady.v1"] = "ConnectionReady.v1"
    protocol_version: ProtocolVersion = PROTOCOL_VERSION
    connection_id: str
    heartbeat_interval_ms: int = Field(ge=1_000)
    max_frame_bytes: int = Field(ge=1_024)


class GatewayBootstrap(ProtocolModel):
    schema_version: Literal["ConversationGatewayBootstrap.v1"] = (
        "ConversationGatewayBootstrap.v1"
    )
    base_url: str
    capability_token: str = Field(min_length=32)
    capability_id: str
    protocol_version: ProtocolVersion = PROTOCOL_VERSION
    expires_at_ms: int = Field(ge=0)
    client_instance_id: str


class SubmitCommand(ProtocolModel):
    type: Literal["turn.submit"]
    payload: TurnSubmit


class CancelCommand(ProtocolModel):
    type: Literal["turn.cancel"]
    conversation_id: str
    turn_id: str
    reason: str = Field(default="user_cancelled", min_length=1, max_length=120)


class ResumeCommand(ProtocolModel):
    type: Literal["stream.resume"]
    turn_id: str
    after_sequence: int = Field(default=0, ge=0)


class AckCommand(ProtocolModel):
    type: Literal["event.ack"]
    turn_id: str
    sequence: int = Field(ge=1)


class PingCommand(ProtocolModel):
    type: Literal["ping"]
    nonce: str | None = Field(default=None, max_length=128)


ConnectionCommand = Annotated[
    Union[SubmitCommand, CancelCommand, ResumeCommand, AckCommand, PingCommand],
    Field(discriminator="type"),
]
CONNECTION_COMMAND_ADAPTER = TypeAdapter(ConnectionCommand)


def parse_connection_command(value: Any) -> ConnectionCommand:
    return CONNECTION_COMMAND_ADAPTER.validate_python(value, strict=True)


def protocol_minor(value: str) -> str:
    if value not in SUPPORTED_PROTOCOL_MINORS:
        raise ValueError(f"unsupported protocol version: {value}")
    return value
