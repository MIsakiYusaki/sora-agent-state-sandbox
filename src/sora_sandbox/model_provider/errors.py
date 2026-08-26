from __future__ import annotations

from enum import StrEnum


class ProviderErrorCode(StrEnum):
    PROFILE_NOT_FOUND = "profile_not_found"
    PROFILE_NOT_READY = "profile_not_ready"
    MODEL_NOT_FOUND = "model_not_found"
    CAPABILITY_MISMATCH = "capability_mismatch"
    BINDING_NOT_FOUND = "binding_not_found"
    REVISION_CONFLICT = "revision_conflict"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    INVALID_CREDENTIAL = "invalid_credential"
    ENDPOINT_BLOCKED = "endpoint_blocked"
    TLS_ERROR = "tls_error"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INVALID_RESPONSE = "invalid_response"
    CANCELLED = "cancelled"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"


class ModelProviderError(RuntimeError):
    def __init__(
        self,
        code: ProviderErrorCode,
        message: str,
        *,
        retryable: bool = False,
        provider_request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.provider_request_id = provider_request_id


class RevisionConflict(ModelProviderError):
    def __init__(self, message: str = "provider configuration revision changed") -> None:
        super().__init__(ProviderErrorCode.REVISION_CONFLICT, message)
