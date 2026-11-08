"""Explicit atomic preference changes through Interruptibility and profile persistence."""
from typing import Any, Literal

from fastapi import FastAPI, Request
from pydantic import field_validator

from .contracts import ProtocolModel, GatewayErrorCode, validate_identifier
from .service import GatewayServiceError


class DefaultControlsRequest(ProtocolModel):
    action: Literal["save", "reset"]
    confirmed: Literal[True]
    reply_depth: Literal["brief", "balanced", "deep"] = "balanced"
    follow_up: Literal["none", "one"] = "one"
    topic_shift: Literal["same_topic", "adjacent"] = "same_topic"
    memory_mode: Literal["natural", "explicit", "off"] = "natural"

    @field_validator("confirmed", mode="before")
    @classmethod
    def confirmation(cls, value: Any):
        if value is not True:
            raise ValueError("explicit confirmation required")
        return True


def install_default_control_routes(app: FastAPI, *, service, capabilities, interruptibility, authenticate, require_protocol):
    @app.post("/v1/conversations/{conversation_id}/control-defaults")
    def defaults(request: Request, conversation_id: str, payload: DefaultControlsRequest):
        require_protocol(request)
        identity = authenticate(request, capabilities, permission="controls.write")
        service._require_identity_scope(identity)
        validate_identifier(conversation_id)
        values = {"reply_depth": payload.reply_depth, "follow_up": payload.follow_up,
                  "topic_shift": payload.topic_shift,
                  "state_surface": {"natural": "implicit", "explicit": {"memory": "explicit"}, "off": {"memory": "none"}}[payload.memory_mode]}
        # The repository lock/save encloses all four pure-state changes: no partial saved defaults.
        with service.state_repository.turn_state(identity.profile_id, conversation_id,
                user_id=identity.subject_id, agent_id=identity.agent_id, pair_id=identity.pair_id) as state:
            for control, value in values.items():
                if payload.action == "reset":
                    interruptibility.revoke(state, control)
                    continue
                result = interruptibility.set_control(state, control=control, value=value, scope="persistent")
                # Confirm exactly the event created by this explicitly confirmed request.
                # Tokens never cross into the WebView and older pending consents stay untouched.
                for receipt in result["receipts"]:
                    if receipt.get("status") == "pending_confirmation":
                        confirmed = interruptibility.confirm(state, receipt["confirmation_token"])
                        if confirmed.get("status") != "applied":
                            raise GatewayServiceError(GatewayErrorCode.INVALID_REQUEST, "Default consent not applied", status_code=409)
            status = interruptibility.status(state)
        return {"schema_version": "ConversationControls.v1", "conversation_id": conversation_id,
                "controls": status.get("controls", {}), "initiative_lease": status.get("initiative_lease")}
