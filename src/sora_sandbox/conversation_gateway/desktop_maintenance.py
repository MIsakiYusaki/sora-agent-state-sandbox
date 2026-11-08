"""Scoped, confirmed lifecycle controls. No arbitrary task or mode input."""
from typing import Literal

from fastapi import FastAPI, Request
from pydantic import Field, field_validator

from .contracts import GatewayErrorCode, ProtocolModel
from .service import GatewayServiceError


class MaintenanceControl(ProtocolModel):
    paused: bool = Field(strict=True)
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def confirmation(cls, value):
        if value is not True:
            raise ValueError("explicit confirmation required")
        return True


def install_maintenance_routes(app: FastAPI, *, stack, service, capabilities, authenticate, require_protocol, require_owner):
    memory = getattr(stack.memory, "runtime", stack.memory)
    scheduler = getattr(stack.memory, "lifecycle_scheduler", None)
    if scheduler is None:
        return

    def owner(request, permission):
        require_protocol(request)
        identity = authenticate(request, capabilities, permission=permission)
        service._require_identity_scope(identity)
        require_owner(identity, memory)
        return identity

    @app.get("/v1/memory/maintenance")
    def read(request: Request):
        owner(request, "memory.read")
        return scheduler.snapshot()

    @app.post("/v1/memory/maintenance")
    def change(request: Request, payload: MaintenanceControl):
        identity = owner(request, "memory.write")
        try:
            return scheduler.set_paused(payload.paused, expected_revision=payload.expected_revision,
                actor="desktop:" + identity.client_instance_id)
        except ValueError:
            raise GatewayServiceError(GatewayErrorCode.INVALID_REQUEST, "maintenance_controls_changed_reload", status_code=409) from None
