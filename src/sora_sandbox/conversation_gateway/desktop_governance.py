"""Scoped Memory review views; never a recursive raw evidence export."""
from __future__ import annotations

import hashlib
import re
from typing import Any, Callable, Literal

from fastapi import FastAPI, Query, Request
from pydantic import Field, field_validator
from starlette.concurrency import run_in_threadpool

from .contracts import ProtocolModel, GatewayErrorCode, validate_identifier
from .service import GatewayServiceError
from ..memory_gate import detect_sensitivity

class GovernanceRow(ProtocolModel):
    id: str
    kind: Literal["reviews", "candidates", "quarantines"]
    status: str
    memory_type: str | None = None
    summary: str | None = None
    summary_state: Literal["available", "protected"]
    reason_codes: list[str]
    created_at: str | None = None
    evidence_count: int | None = None
    revision: str | None = None
    can_approve: bool = False
    can_dismiss: bool = False
    severity: str | None = None
    risk_type: str | None = None

class GovernancePage(ProtocolModel):
    schema_version: Literal["DesktopGovernancePage.v1"] = "DesktopGovernancePage.v1"
    items: list[GovernanceRow]
    truncated: bool
    next_offset: int | None = None

class ReviewResolution(ProtocolModel):
    review_id: str
    action: Literal["approve", "reject", "defer"]
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("explicit confirmation required")
        return True

    @field_validator("review_id")
    @classmethod
    def identifier(cls, value: str) -> str:
        return validate_identifier(value)

class QuarantineDismissal(ProtocolModel):
    quarantine_id: str
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("explicit confirmation required")
        return True

    @field_validator("quarantine_id")
    @classmethod
    def identifier(cls, value: str) -> str:
        return validate_identifier(value)

class QuarantineReceipt(ProtocolModel):
    status: Literal["dismissed", "already_dismissed"]
    quarantine_id: str
    audit_id: str
    resolved_at: str

def safe_summary(candidate: dict[str, Any]) -> str | None:
    text = str(candidate.get("summary") or "")
    # Read permission is not permission to disclose sensitive/secret evidence.
    if candidate.get("sensitivity") not in {"low", "public"} or detect_sensitivity(text) != "low":
        return None
    if re.search(r"(?i)(\bsk-[A-Za-z0-9_-]{12,}|\bbearer\s+\S+|\bAKIA[A-Z0-9]{16}\b|-----BEGIN)", text):
        return None
    return text[:512]

def _codes(value: Any) -> list[str]:
    return [part for part in str(value or "").split(";") if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", part)][:12]

def install_governance_routes(app: FastAPI, *, memory: Any, owner: Callable) -> None:
    @app.get("/v1/debug/memory-governance", response_model=GovernancePage)
    def listing(request: Request, kind: Literal["reviews", "candidates", "quarantines"] = "reviews", limit: int = Query(default=100, ge=1, le=100), offset: int = Query(default=0, ge=0, le=1_000_000)):
        owner(request, "debug.review.read")
        if kind == "reviews":
            records = memory.list_review_items(status="open", limit=limit + 1, offset=offset)
        elif kind == "candidates":
            records = memory.list_candidates(limit=limit + 1, offset=offset)
        else:
            records = memory.list_quarantines(status="active", limit=limit + 1, offset=offset)
        rows = []
        for item in records[:limit]:
            snapshot = memory.review_snapshot(item["review_id"]) if kind == "reviews" else memory.quarantine_snapshot(item["quarantine_id"]) if kind == "quarantines" else None
            if snapshot is not None:
                item = snapshot["review" if kind == "reviews" else "quarantine"]
            candidate = (snapshot["candidate"] or {}) if snapshot else item if kind == "candidates" else {}
            gist = safe_summary(candidate)
            identifier = str(item.get("review_id") or item.get("candidate_id") or item.get("quarantine_id") or item["id"])
            rows.append(GovernanceRow(
                id=identifier, kind=kind, status=str(item.get("status") or "unknown"),
                memory_type=str(candidate["type"]) if candidate.get("type") else None,
                summary=gist, summary_state="available" if gist is not None else "protected",
                reason_codes=_codes(item.get("reason")), created_at=item.get("created_at"),
                evidence_count=len(candidate.get("evidence_ids", [])) if candidate else None,
                revision=snapshot["revision"] if snapshot else None,
                can_approve=kind == "reviews" and item["status"] == "open" and gist is not None,
                can_dismiss=kind == "quarantines" and item["status"] == "active" and candidate.get("status") == "quarantined" and candidate.get("quarantine_id") == identifier,
                severity=(_codes(item.get("severity")) or [None])[0] if kind == "quarantines" else None,
                risk_type=(_codes(item.get("risk_type")) or [None])[0] if kind == "quarantines" else None,
            ))
        more = len(records) > limit
        return GovernancePage(items=rows, truncated=more, next_offset=offset + limit if more else None)

    @app.post("/v1/debug/memory-governance/resolve")
    async def resolve(request: Request, payload: ReviewResolution):
        identity = owner(request, "debug.review.resolve")
        def perform():
            try:
                review = memory.get_review_item(payload.review_id)
                candidate = memory.get_candidate(review["candidate_id"])
                if payload.action == "approve" and safe_summary(candidate) is None:
                    raise GatewayServiceError(GatewayErrorCode.FORBIDDEN, "Sensitive review needs its own disclosure workflow", status_code=403)
                actor = "desktop_review_" + hashlib.sha256("\x1f".join((identity.profile_id, identity.subject_id, identity.agent_id, identity.client_instance_id)).encode()).hexdigest()
                result = memory.resolve_review_checked(payload.review_id, action=payload.action, expected_revision=payload.expected_revision, actor=actor)
                current = result["review"]
                return {"schema_version": "DesktopReviewReceipt.v1", "review_id": payload.review_id, "status": result["status"], "resolution": current.get("resolution"), "resolved_at": current.get("resolved_at")}
            except ValueError:
                raise GatewayServiceError(GatewayErrorCode.INVALID_REQUEST, "Review changed or is unavailable; reload before deciding", status_code=409) from None
        return await run_in_threadpool(perform)

    @app.post("/v1/debug/memory-governance/quarantines/dismiss", response_model=QuarantineReceipt)
    async def dismiss(request: Request, payload: QuarantineDismissal):
        identity = owner(request, "debug.quarantine.resolve")
        actor = "desktop_quarantine_" + hashlib.sha256("\x1f".join((identity.profile_id, identity.subject_id, identity.agent_id, identity.client_instance_id)).encode()).hexdigest()
        def perform():
            try:
                return memory.dismiss_quarantine_checked(payload.quarantine_id, expected_revision=payload.expected_revision, actor=actor)
            except ValueError:
                raise GatewayServiceError(GatewayErrorCode.INVALID_REQUEST, "Quarantine changed or is unavailable; reload before deciding", status_code=409) from None
        return await run_in_threadpool(perform)
