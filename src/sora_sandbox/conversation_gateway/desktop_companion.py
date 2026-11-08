"""Owner-scoped companion controls; domain systems remain canonical owners."""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Literal

from fastapi import FastAPI, Request
from pydantic import Field, StrictBool, field_validator

from ..memory_schema import json_dumps
from ..relationship.contracts import REPAIR_TRANSITIONS
from .contracts import GatewayErrorCode, ProtocolModel, validate_identifier
from .desktop_governance import safe_summary
from .service import GatewayServiceError


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json_dumps(value).encode("utf-8")).hexdigest()


class CompanionUpdate(ProtocolModel):
    control: Literal["persona_growth", "persona_surface", "relationship_learning", "relationship_surface"]
    value: StrictBool | Literal["silent", "implicit", "explicit"]
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def confirm(cls, value):
        if value is not True:
            raise ValueError("explicit confirmation required")
        return True


class AgreementDecision(ProtocolModel):
    candidate_id: str
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]

    _confirm = field_validator("confirmed", mode="before")(CompanionUpdate.confirm.__func__)

    @field_validator("candidate_id")
    @classmethod
    def identifier(cls, value):
        return validate_identifier(value)


class RepairDecision(ProtocolModel):
    repair_id: str
    expected_status: str = Field(max_length=40)
    to_state: Literal["acknowledged", "cooling_off", "repair_in_progress", "repaired", "accepted_difference", "unresolved"]
    confirmed: Literal[True]

    _confirm = field_validator("confirmed", mode="before")(CompanionUpdate.confirm.__func__)

    @field_validator("repair_id")
    @classmethod
    def identifier(cls, value):
        return validate_identifier(value)


class AgreementProposal(ProtocolModel):
    rhythm: Literal["gentle_low_assumption", "warm_low_pressure", "warm_flexible"]


class GovernanceDecision(ProtocolModel):
    domain: Literal["persona", "relationship"]
    action: Literal["reject_candidate", "rollback_contribution"]
    target_id: str
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]

    _confirm = field_validator("confirmed", mode="before")(CompanionUpdate.confirm.__func__)

    @field_validator("target_id")
    @classmethod
    def identifier(cls, value):
        return validate_identifier(value)


class AgreementRevocation(ProtocolModel):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]

    _confirm = field_validator("confirmed", mode="before")(CompanionUpdate.confirm.__func__)


def _code(value: Any) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", value) else "protected"


def _safe_text(value: Any) -> str | None:
    if not isinstance(value, (str, bool, int, float)):
        return None
    return safe_summary({"summary": str(value), "sensitivity": "low"})


def install_companion_routes(app: FastAPI, *, stack: Any, service: Any, capabilities: Any,
                             authenticate: Any, require_protocol: Any, require_owner: Any) -> None:
    persona, relationship = getattr(stack, "persona", None), getattr(stack, "relationship", None)
    if persona is None or relationship is None:
        return
    memory = getattr(stack.memory, "runtime", stack.memory)

    def owner(request: Request, permission: str):
        require_protocol(request)
        identity = authenticate(request, capabilities, permission=permission)
        if service.identity_scope != (identity.profile_id, identity.subject_id, identity.agent_id):
            raise GatewayServiceError(GatewayErrorCode.FORBIDDEN, "companion_owner_mismatch", status_code=403)
        require_owner(identity, memory)
        return identity

    def invalid():
        return GatewayServiceError(GatewayErrorCode.INVALID_REQUEST, "companion_state_changed_reload_before_retry", status_code=409)

    def agreement_view(item):
        path = str(item.get("term_path") or "")
        value = _safe_text(item.get("proposed_value"))
        # Only present readable terms. Never return domain confirmation secrets.
        readable = path == "terms.comfort_rhythm" and value in {"gentle_low_assumption", "warm_low_pressure", "warm_flexible"}
        return {"id": item["candidate_id"], "term": path if readable else None,
                "value": value if readable else None, "status": item["status"],
                "expires_at": item.get("expires_at"), "revision": fingerprint({k: v for k, v in item.items() if k != "confirmation_token"}),
                "can_confirm": readable and bool(item.get("confirmation_token"))}

    def view(user):
        control = persona.store.get_control(user_id=user)
        pair = relationship.pair(user_id=user)
        repair = relationship.store.open_repair(pair["pair_id"])
        agreement = relationship.store.active_agreement(pair["pair_id"])
        return {"schema_version": "DesktopCompanion.v1",
                "persona": {"growth_paused": control["growth_paused"],
                            "global_growth_paused": persona.store.get_control()["growth_paused"],
                            "surface_permission": control["self_reference_permission"], "revision": fingerprint(control)},
                "relationship": {"learning_paused": pair["learning_paused"], "surface_permission": pair["surface_permission"],
                                 "revision": relationship.store.control_fingerprint(pair)},
                "agreements": [agreement_view(x) for x in relationship.consent_requests(user_id=user, limit=100)],
                "active_agreement": {"status": agreement.get("status", "none"), "revision": fingerprint(agreement),
                    "user_confirmed": (agreement.get("consent") or {}).get("accepted_by_user") is True,
                    "comfort_rhythm": (agreement.get("terms") or {}).get("comfort_rhythm") if (agreement.get("terms") or {}).get("comfort_rhythm") in {"gentle_low_assumption", "warm_low_pressure", "warm_flexible"} else None},
                "repair": None if repair is None else {"id": repair["repair_episode_id"], "status": repair["status"],
                    "allowed_transitions": sorted(REPAIR_TRANSITIONS.get(repair["status"], set()) - {"stable", "rupture_detected"})}}

    @app.get("/v1/companion")
    def read(request: Request):
        identity = owner(request, "controls.read")
        return view(identity.subject_id)

    @app.post("/v1/companion/controls")
    def update(request: Request, payload: CompanionUpdate):
        identity = owner(request, "controls.write")
        boolean = payload.control in {"persona_growth", "relationship_learning"}
        if (boolean and type(payload.value) is not bool) or (not boolean and type(payload.value) is not str):
            raise invalid()
        user = identity.subject_id
        try:
            if payload.control.startswith("persona_"):
                changes = {"growth_paused": payload.value} if boolean else {"self_reference_permission": payload.value}
                persona.store.set_control(user_id=user, actor="user", expected_revision=payload.expected_revision, **changes)
            elif boolean:
                relationship.set_learning_paused(user_id=user, paused=payload.value, expected_revision=payload.expected_revision)
            else:
                relationship.set_surface_permission(user_id=user, permission=payload.value, expected_revision=payload.expected_revision)
        except (ValueError, KeyError):
            raise invalid() from None
        return view(user)

    @app.post("/v1/companion/agreements/confirm")
    def confirm_agreement(request: Request, payload: AgreementDecision):
        identity = owner(request, "controls.write")
        try:
            rows = relationship.consent_requests(user_id=identity.subject_id, limit=100)
            row = next(x for x in rows if x["candidate_id"] == payload.candidate_id)
            shown = agreement_view(row)
            if not shown["can_confirm"] or shown["revision"] != payload.expected_revision:
                raise ValueError("stale_or_protected_agreement")
            relationship.confirm_agreement(user_id=identity.subject_id, candidate_id=payload.candidate_id,
                                           confirmation_token=row["confirmation_token"], actor="user")
        except (ValueError, KeyError, StopIteration):
            raise invalid() from None
        return view(identity.subject_id)

    @app.post("/v1/companion/agreements/propose")
    def propose_agreement(request: Request, payload: AgreementProposal):
        identity = owner(request, "controls.write")
        relationship.propose_agreement(user_id=identity.subject_id, term_path="terms.comfort_rhythm",
                                       proposed_value=payload.rhythm, evidence_refs=["manual:user_communication_preference"])
        return view(identity.subject_id)

    @app.post("/v1/companion/agreements/revoke")
    def revoke_agreement(request: Request, payload: AgreementRevocation):
        identity = owner(request, "controls.write")
        try:
            pair = relationship.pair(user_id=identity.subject_id)
            relationship.coordinator.submit(lambda: relationship.store.revoke_agreement(pair["pair_id"],
                engine=relationship.engine, actor="user", expected_revision=payload.expected_revision),
                priority=0, name="desktop_user_revoke_agreement")
        except (ValueError, KeyError):
            raise invalid() from None
        return view(identity.subject_id)

    @app.post("/v1/companion/repair")
    def repair(request: Request, payload: RepairDecision):
        identity = owner(request, "controls.write")
        try:
            pair = relationship.pair(user_id=identity.subject_id)
            relationship.coordinator.submit(lambda: relationship.store.transition_repair(
                payload.repair_id, payload.to_state, engine=relationship.engine, actor="user",
                expected_pair_id=pair["pair_id"], expected_status=payload.expected_status,
                user_outcome="accepted" if payload.to_state == "repaired" else payload.to_state,
            ), priority=0, name="desktop_user_repair_decision")
        except (ValueError, KeyError):
            raise invalid() from None
        return view(identity.subject_id)

    @app.get("/v1/debug/companion")
    def governance(request: Request, domain: Literal["persona", "relationship"] = "persona"):
        identity = owner(request, "debug.read")
        started = time.perf_counter()
        user = identity.subject_id
        source_truncated = False
        if domain == "persona":
            candidates = persona.store.list_candidates(limit=501)
            contributions = persona.store.list_contributions(limit=501)
            audits = persona.store.list_audit(limit=101)
            source_truncated = any(len(x) > 100 for x in (candidates, contributions, audits))
            candidates = [x for x in candidates if x.get("user_id") in {None, user}]
            contributions = [x for x in contributions if x.get("user_id") in {None, user}]
            owned_entities = {user, "global"} | {x["candidate_id"] for x in candidates} | {x["contribution_id"] for x in contributions}
            audits = [x for x in audits if x.get("entity_id") in owned_entities]
        else:
            candidates = relationship.list_candidates(user_id=user, limit=501)
            contributions = relationship.list_contributions(user_id=user, limit=501)
            pair = relationship.pair(user_id=user)
            audits = relationship.store.list_audit(pair_id=pair["pair_id"], limit=101)
        def row(x, kind):
            candidate = x.get("candidate") or {}
            refs = x.get("evidence_refs") or candidate.get("evidence_refs") or []
            return {"id": x.get("candidate_id") if kind == "candidate" else x.get("contribution_id"),
                    "kind": kind, "status": _code(x.get("status") or ("active" if x.get("active") else "inactive")),
                    "target": _code(x.get("target_key") or x.get("dimension") or candidate.get("target_key") or candidate.get("dimension")),
                    "created_at": x.get("created_at"), "candidate_id": x.get("candidate_id"),
                    "source_fingerprints": [fingerprint(str(ref))[:16] for ref in refs[:20]],
                    "reason": _code(x.get("gate_reason") or "not_reported"), "revision": fingerprint(x)}
        return {"schema_version": "DesktopCompanionGovernance.v1", "domain": domain,
                "candidates": [row(x, "candidate") for x in candidates[:100]],
                "contributions": [row(x, "contribution") for x in contributions[:100]],
                "audits": [{"id": x.get("audit_id") or x.get("id"), "action": _code(x.get("action")),
                            "entity_id": _code(x.get("entity_id")), "created_at": x.get("created_at")} for x in audits[:100]],
                "truncated": source_truncated or any(len(x) > 100 for x in (candidates, contributions, audits)),
                "duration_ms": round((time.perf_counter() - started) * 1000, 3)}

    @app.post("/v1/debug/companion/resolve")
    def resolve(request: Request, payload: GovernanceDecision):
        identity = owner(request, "debug.companion.manage")
        started = time.perf_counter()
        actor = "desktop_companion_" + fingerprint([identity.profile_id, identity.subject_id, identity.agent_id, identity.client_instance_id])[:32]
        try:
            if payload.domain == "persona":
                method = persona.reject_candidate if payload.action == "reject_candidate" else persona.rollback_contribution
                method(payload.target_id, reason="desktop_confirmed_governance", actor=actor,
                       expected_revision=payload.expected_revision, expected_user_id=identity.subject_id)
                audits = persona.store.list_audit(limit=100)
            else:
                pair = relationship.pair(user_id=identity.subject_id)
                if payload.action == "reject_candidate":
                    operation = lambda: relationship.store.set_candidate_status(payload.target_id, "rejected",
                        reason="desktop_confirmed_governance", actor=actor,
                        expected_revision=payload.expected_revision, expected_pair_id=pair["pair_id"])
                else:
                    operation = lambda: relationship.store.rollback_contribution(payload.target_id, engine=relationship.engine,
                        reason="desktop_confirmed_governance", actor=actor,
                        expected_revision=payload.expected_revision, expected_pair_id=pair["pair_id"])
                relationship.coordinator.submit(operation, priority=0, name="desktop_domain_governance")
                audits = relationship.store.list_audit(pair_id=pair["pair_id"], limit=100)
            audit = next(x for x in audits if x.get("entity_id") == payload.target_id and x.get("actor") == actor)
        except (ValueError, KeyError, StopIteration):
            raise invalid() from None
        return {"status": "applied", "target_id": payload.target_id, "action": payload.action,
                "audit_id": audit.get("id") or audit.get("audit_id"), "duration_ms": round((time.perf_counter() - started) * 1000, 3)}
