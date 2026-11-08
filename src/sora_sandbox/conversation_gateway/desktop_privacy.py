"""Allowlisted privacy exports for the trusted native file-save flow."""
import hashlib
import json
import platform
from typing import Literal

from fastapi import FastAPI, Request
from pydantic import field_validator

from .contracts import GatewayErrorCode, ProtocolModel
from .desktop_governance import safe_summary
from .service import GatewayServiceError
from ..memory_schema import utc_now

MAX_EXPORT_BYTES = 2 * 1024 * 1024
MAX_EXPORT_MEMORIES = 10_000


class PrivacyExportRequest(ProtocolModel):
    kind: Literal["memory_summary", "diagnostic"]
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def confirmation(cls, value):
        if value is not True:
            raise ValueError("explicit export confirmation required")
        return True


def build_privacy_export(memory, identity, kind: str) -> dict:
    with memory._lock:
        records = memory.list_memories()
        if len(records) > MAX_EXPORT_MEMORIES:
            raise GatewayServiceError(GatewayErrorCode.INVALID_REQUEST, "Export exceeds the supported record limit", status_code=413)
        if kind == "memory_summary":
            items = []
            for record in records:
                if (record.get("status") != "active"
                        or record.get("surface_permission") != "auto"
                        or record.get("disclosure_permission") != "gist_only"):
                    continue
                summary = safe_summary(record)
                if summary:
                    items.append({"summary": summary})
            content = {"memories": items, "exported_count": len(items), "excluded_count": len(records) - len(items)}
        elif kind == "diagnostic":
            content = {
                "protocol_version": "1.0",
                "python_version": platform.python_version(),
                "memory_records": len(records),
                "memory_states": {state: sum(record.get("status") == state for record in records) for state in ("active", "disabled", "superseded")},
                "coverage": ["protocol_version", "python_version", "memory_record_counts"],
            }
        else:
            raise ValueError("unsupported privacy export")
    identity_material = "\x1f".join((identity.profile_id, identity.subject_id, identity.agent_id))
    result = {
        "schema_version": "DesktopPrivacyExport.v1",
        "kind": kind,
        "created_at": utc_now(),
        "space_fingerprint": hashlib.sha256(identity_material.encode()).hexdigest()[:16],
        "content": content,
    }
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_EXPORT_BYTES:
        raise GatewayServiceError(GatewayErrorCode.INVALID_REQUEST, "Export exceeds the supported file size", status_code=413)
    return result


def install_privacy_routes(app: FastAPI, *, memory, service, capabilities, authenticate, require_protocol, require_owner):
    @app.post("/v1/privacy/export")
    def export(request: Request, payload: PrivacyExportRequest):
        require_protocol(request)
        identity = authenticate(request, capabilities, permission="memory.read")
        service._require_identity_scope(identity)
        require_owner(identity, memory)
        return build_privacy_export(memory, identity, payload.kind)
