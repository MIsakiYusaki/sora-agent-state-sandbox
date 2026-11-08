"""Literal authored narrative participation, never a simulated story dashboard."""
import time
from typing import Literal

from fastapi import FastAPI, Request
from pydantic import Field, field_validator

from .contracts import GatewayErrorCode, ProtocolModel, validate_identifier
from .service import GatewayServiceError
from ..worldops.contracts import WorldOpsConflictError, WorldOpsContractError, WorldOpsSecurityError


class NarrativeAction(ProtocolModel):
    request_id: str = Field(max_length=64)
    instance_id: str
    expected_revision: int = Field(ge=0, strict=True)
    bundle_id: str
    canon_version: str = Field(min_length=1, max_length=64)
    action: Literal["invite", "choice"]
    choice_id: str | None = None

    @field_validator("request_id", "instance_id", "bundle_id", "choice_id")
    @classmethod
    def identifiers(cls, value):
        return None if value is None else validate_identifier(value)


def install_narrative_routes(app: FastAPI, *, stack, service, capabilities, authenticate, require_protocol, require_owner):
    worldops = getattr(stack, "worldops", None)
    if worldops is None:
        return
    memory = getattr(stack.memory, "runtime", stack.memory)

    def owner(request, permission):
        require_protocol(request)
        identity = authenticate(request, capabilities, permission=permission)
        scope = worldops.store.identity
        if service.identity_scope != (identity.profile_id, identity.subject_id, identity.agent_id) or scope.user_id != identity.subject_id or scope.sora_id != identity.agent_id:
            raise GatewayServiceError(GatewayErrorCode.FORBIDDEN, "narrative_owner_mismatch", status_code=403)
        require_owner(identity, memory)
        return identity

    def view():
        active = worldops.store.active_bundle()
        source = "local_preview" if worldops.deployment_mode == "local_split_development" else "signed_content"
        result = {"schema_version": "DesktopNarrative.v1", "source": source, "available": False,
                  "world_name": None, "bundle_id": None, "canon_version": None,
                  "events": [], "truncated": False, "unavailable_reason": "no_signed_content"}
        if active is None:
            return result
        try:
            worldops.store.verify_bundle(active)
        except (WorldOpsContractError, WorldOpsSecurityError, ValueError):
            result["unavailable_reason"] = "content_verification_failed"
            return result
        manifest, content = active["manifest"], active["bundle"]
        result.update(available=True, world_name=content["world"]["name"], bundle_id=manifest["bundle_id"],
                      canon_version=manifest["canon_version"], unavailable_reason=None)
        instances = worldops.store.list_event_instances()
        result["truncated"] = len(instances) > 100
        eligibility_context = {"facts": worldops.store.active_facts(), "world_clock": worldops.store.timeline()["world_clock"],
            "event_states": {x["event_id"]: x["state"] for x in instances}}
        for item in instances[:100]:
            event = worldops.store.event_definition(item["event_id"], definition_version=item["definition_version"])
            if event is None or item["bundle_id"] != manifest["bundle_id"]:
                result["truncated"] = True
                continue
            evidence = worldops.store.list_task_evidence(item["instance_id"])
            allowed_ids = set()
            for transition in event.get("transitions") or []:
                if transition.get("from") == item["state"] and transition.get("trigger") == "choice":
                    allowed_ids.update(transition.get("choice_ids") or [x["choice_id"] for x in event.get("choices") or []])
            tasks = []
            for task in event.get("tasks") or []:
                try:
                    done = worldops.runtime.predicate_engine.evaluate(task.get("completion"),
                        {"task_id": task["task_id"], "task_evidence": evidence})
                except WorldOpsContractError:
                    done = None
                tasks.append({"id": task["task_id"], "title": str(task["title"])[:300], "completed": done})
            chosen = next((x for x in event.get("choices") or [] if x["choice_id"] == item.get("selected_choice_id")), None)
            can_invite = item["state"] == "hinted"
            if item["state"] == "dormant":
                try:
                    can_invite = worldops.runtime.predicate_engine.evaluate(event.get("eligibility"), eligibility_context)
                except WorldOpsContractError:
                    can_invite = False
            history = worldops.store.event_history(item["instance_id"], limit=20)
            memory_state = "not_requested"
            completed = next((x for x in history if x["to_state"] == "completed"), None)
            if completed:
                memory_state = "not_observed"
                source_evidence = memory.find_evidence_by_source(source_type="worldops_transition", source_ref=completed["transition_id"])
                if source_evidence:
                    candidate = memory.find_candidate_by_evidence(source_evidence["id"])
                    memory_state = candidate["status"] if candidate else "evidence_only"
            result["events"].append({"id": item["instance_id"], "event_id": item["event_id"], "revision": item["revision"],
                "title": str(event["title"])[:300], "summary": str(event.get("summary") or "")[:1500], "state": item["state"],
                "updated_at": item["updated_at"], "selected_choice": str(chosen.get("label") or chosen["choice_id"])[:500] if chosen else None,
                "choices": [{"id": x["choice_id"], "label": str(x.get("label") or x["choice_id"])[:500]} for x in event.get("choices") or [] if x["choice_id"] in allowed_ids],
                "can_invite": can_invite, "tasks": tasks, "memory_state": memory_state,
                "history": [{"id": x["transition_id"], "state": x["to_state"], "occurred_at": x["occurred_at"]} for x in history]})
        return result

    @app.get("/v1/narrative")
    def read(request: Request):
        owner(request, "controls.read")
        return view()

    @app.post("/v1/narrative/actions")
    def act(request: Request, payload: NarrativeAction):
        owner(request, "controls.write")
        started = time.perf_counter()
        try:
            receipt = worldops.write_coordinator.submit(
                lambda: worldops.runtime.apply_authored_action(**payload.model_dump()),
                priority=1, name="desktop_authored_narrative_action")
        except (WorldOpsContractError, WorldOpsConflictError, WorldOpsSecurityError, ValueError, KeyError):
            raise GatewayServiceError(GatewayErrorCode.INVALID_REQUEST, "narrative_state_changed_reload_before_retry", status_code=409) from None
        return {"receipt": receipt, "view": view(), "duration_ms": round((time.perf_counter() - started) * 1000, 3)}
