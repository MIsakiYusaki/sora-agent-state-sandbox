"""User-owned Memory details and public response basis; never raw evidence/prompt export."""
import re
from typing import Any, Literal

from fastapi import FastAPI, Request
from pydantic import Field, field_validator

from .contracts import ProtocolModel, GatewayErrorCode, validate_identifier
from .desktop_governance import safe_summary
from .service import GatewayServiceError


class MemoryCorrectionRequest(ProtocolModel):
    summary: str = Field(min_length=1, max_length=4000)
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def confirmation(cls, value: Any):
        if value is not True:
            raise ValueError("explicit confirmation required")
        return True

    @field_validator("summary")
    @classmethod
    def summary_text(cls, value: str):
        if not value.strip() or re.search(r"(?i)(\bsk-[A-Za-z0-9_-]{12,}|\bbearer\s+\S+|-----BEGIN)", value):
            raise ValueError("summary is empty or contains protected secrets")
        return value.strip()


def install_user_memory_routes(app: FastAPI, *, memory, service, capabilities, authenticate, require_protocol, require_owner, memory_view):
    def owner(request, permission):
        require_protocol(request)
        identity = authenticate(request, capabilities, permission=permission)
        service._require_identity_scope(identity)
        require_owner(identity, memory)
        return identity

    @app.get("/v1/memories/{memory_id}/details")
    def details(request: Request, memory_id: str):
        identity = owner(request, "memory.read")
        validate_identifier(memory_id)
        try:
            snapshot = memory.memory_revision_snapshot(memory_id)
        except ValueError:
            raise GatewayServiceError(GatewayErrorCode.TURN_NOT_FOUND, "Memory unavailable", status_code=404) from None
        if snapshot["memory"]["status"] != "active":
            raise GatewayServiceError(GatewayErrorCode.TURN_NOT_FOUND, "Memory unavailable", status_code=404)
        sources = []
        for evidence in snapshot["evidence"]:
            source_type = evidence.get("source_type")
            kind = "manual" if source_type in {"manual_revision", "manual_repl", "manual"} else "conversation" if source_type in {"user_explicit", "conversation", "dialogue", "user_turn", "episode_extraction", "chat"} else "other"
            metadata = evidence.get("metadata") or {}
            source_identity = metadata.get("source_identity") or {}
            turn_id = metadata.get("runtime_turn_id") or metadata.get("turn_id") or source_identity.get("turn_id") or source_identity.get("runtime_turn_id")
            turn = service.registry.turn_for_identity(turn_id=turn_id, profile_id=identity.profile_id, subject_id=identity.subject_id, agent_id=identity.agent_id) if isinstance(turn_id, str) else None
            sources.append({"kind": "conversation" if turn else kind, "created_at": evidence.get("created_at"), "conversation_id": turn.external_conversation_id if turn else None})
        return {"schema_version": "UserMemoryDetail.v1", "memory": memory_view(snapshot["memory"]), "revision": snapshot["revision"], "sources": sources,
                "can_correct": True}

    @app.post("/v1/memories/{memory_id}/correct")
    def correct(request: Request, memory_id: str, payload: MemoryCorrectionRequest):
        owner(request, "memory.write")
        validate_identifier(memory_id)
        try:
            updated = memory.correct_memory_checked(memory_id, payload.summary, expected_revision=payload.expected_revision)
        except ValueError:
            raise GatewayServiceError(GatewayErrorCode.INVALID_REQUEST, "Correction not confirmed; reload the memory", status_code=409) from None
        return {"schema_version": "UserMemoryCorrection.v1", "previous_memory_id": memory_id, "memory": memory_view(updated)}

    @app.get("/v1/conversations/{conversation_id}/responses/{turn_id}/basis")
    def basis(request: Request, conversation_id: str, turn_id: str):
        identity = owner(request, "history.read")
        service._require_permission(identity, "memory.read")
        validate_identifier(conversation_id)
        validate_identifier(turn_id)
        binding = service.registry.conversation_for_identity(profile_id=identity.profile_id, subject_id=identity.subject_id, agent_id=identity.agent_id, external_conversation_id=conversation_id)
        turn = service.registry.turn_for_identity(turn_id=turn_id, profile_id=identity.profile_id, subject_id=identity.subject_id, agent_id=identity.agent_id)
        if not binding or not turn or turn.external_conversation_id != conversation_id:
            raise GatewayServiceError(GatewayErrorCode.TURN_NOT_FOUND, "Response unavailable", status_code=404)
        try:
            recorded = service.runtime.response_basis(binding.runtime_conversation_id, turn_id)
        except ValueError:
            raise GatewayServiceError(GatewayErrorCode.TURN_NOT_FOUND, "Response unavailable", status_code=404) from None
        result = {"schema_version": "UserResponseBasis.v1", "conversation_id": conversation_id, "turn_id": turn_id,
                  "source": "generation_sealed" if recorded else "unobserved", "recent_message_count": recorded.get("recent_message_count") if recorded else None,
                  "memories": [], "unavailable_memory_count": (recorded.get("unidentified_memory_count") or 0) if recorded else None}
        if recorded:
            for memory_id in recorded.get("memory_ids") or []:
                try:
                    item = memory.get_memory(memory_id)
                except ValueError:
                    result["unavailable_memory_count"] += 1
                    continue
                gist = safe_summary(item)
                if item.get("status") != "active" or str(item.get("surface_permission")) in {"none", "hidden", "deny"} or gist is None:
                    result["unavailable_memory_count"] += 1
                    continue
                result["memories"].append({"memory_id": memory_id, "summary": gist})
        return result
