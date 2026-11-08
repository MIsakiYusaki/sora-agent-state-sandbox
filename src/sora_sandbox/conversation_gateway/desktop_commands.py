"""Typed, maintenance-only command adapter for a short native debug session."""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal

from fastapi import FastAPI, Request
from pydantic import Field, field_validator
from starlette.concurrency import run_in_threadpool

from .contracts import ProtocolModel, GatewayErrorCode, validate_identifier
from .security import LocalCapabilityProvider, VerifiedIdentity
from .service import GatewayServiceError


class DesktopDebugSessionOpenRequest(ProtocolModel):
    schema_version: Literal["DesktopDebugSessionOpenRequest.v1"] = "DesktopDebugSessionOpenRequest.v1"
    window_label: Literal["main"]


class DesktopDebugSession(ProtocolModel):
    schema_version: Literal["DesktopDebugSession.v1"] = "DesktopDebugSession.v1"
    session_id: str
    window_label: Literal["main"]
    expires_at_ms: int
    scopes: list[str]


class DesktopAdminPreviewRequest(ProtocolModel):
    schema_version: Literal["DesktopAdminPreviewRequest.v1"] = "DesktopAdminPreviewRequest.v1"
    command_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str

    @field_validator("command_id", "idempotency_key")
    @classmethod
    def identifier(cls, value: str) -> str:
        return validate_identifier(value)


class DesktopAdminExecuteRequest(ProtocolModel):
    schema_version: Literal["DesktopAdminExecuteRequest.v1"] = "DesktopAdminExecuteRequest.v1"
    action_id: str
    idempotency_key: str
    expected_revision: str = Field(min_length=64, max_length=64)
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("explicit boolean confirmation is required")
        return True

    @field_validator("action_id", "idempotency_key")
    @classmethod
    def identifier(cls, value: str) -> str:
        return validate_identifier(value)

    @field_validator("expected_revision")
    @classmethod
    def revision_hash(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("expected revision must be a lowercase SHA256 digest")
        return value


class DesktopAdminCommandSpec(ProtocolModel):
    command_id: str
    action_name: str
    description: str
    risk_level: str
    level: Literal["L0", "L1", "L2", "L3", "L4"] | None
    available: bool
    unavailable_reason: str | None = None
    parameter_schema: dict[str, Any] | None
    default_params: dict[str, Any] | None
    supports_dry_run: bool
    client_wait_timeout_ms: int = 90_000


class DesktopAdminRegistry(ProtocolModel):
    schema_version: Literal["DesktopAdminRegistry.v1"] = "DesktopAdminRegistry.v1"
    commands: list[DesktopAdminCommandSpec]
    history: list[DesktopAdminActionView] = Field(default_factory=list)


class DesktopAdminActionView(ProtocolModel):
    schema_version: Literal["DesktopAdminActionView.v1"] = "DesktopAdminActionView.v1"
    action_id: str
    command_id: str
    params: dict[str, Any]
    idempotency_key: str
    expected_revision: str
    execution_status: Literal["previewed", "executing", "executed", "execution_unknown", "expired", "dry_run_blocked"]
    risk_level: str
    active_turns: int | None
    owned_by_current_client: bool | None = None
    impact_summary: dict[str, Any]
    rollback_type: str | None
    result_summary: dict[str, Any]
    audit_event_id: str | None
    created_at: str
    expires_at: str | None
    executed_at: str | None
    dry_run: bool


class EmptyParams(ProtocolModel):
    pass


class LimitParams(ProtocolModel):
    limit: int = Field(default=100, ge=1, le=500)


class ReleaseParams(ProtocolModel):
    change_scope: list[Literal["all", "canonical", "gate", "recall", "projection", "forget", "persona_growth"]] = Field(default_factory=lambda: ["all"], min_length=1, max_length=7)


class QueueRequeueParams(ProtocolModel):
    kind: Literal["index", "projection"]
    event_id: str = Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


# Explicit adapter boundaries. Unsupported actions remain visible but cannot
# enter execution, including filesystem exports, forced commits and L3/L4 writes.
ADAPTERS = {
    "run_quality_eval": ("eval.run", "运行已注册评测", EmptyParams),
    "run_scenario_replay": ("eval.replay", "运行默认场景回放", EmptyParams),
    "collect_quality_metrics": ("eval.metrics.collect", "采集质量指标", EmptyParams),
    "run_release_gate": ("eval.release_gate", "运行 Module 10 变更门禁", ReleaseParams),
    "sync_trace_index": ("trace.index.sync", "同步诊断索引", LimitParams),
    "process_index_outbox": ("memory.index.drain", "处理可重建索引队列", LimitParams),
    "process_projector_outbox": ("memory.projection.drain", "处理幂等记忆投影队列", LimitParams),
    "requeue_memory_event": ("memory.event.requeue", "重新入队单条失败或死信事件", QueueRequeueParams),
    "worldops_rebuild_verify": ("worldops.rebuild.verify", "核对叙事状态重建一致性", EmptyParams),
}
SAFE_IMPACT_KEYS = {"limit", "pending", "suites", "package_name", "change_scope", "dry_run", "queue_kind", "event_id", "expected_revision", "status", "attempts", "additional_attempts"}
SAFE_COUNT_KEYS = {"total_cases", "passed_cases", "failed_cases", "processed", "count", "synced", "points", "pending", "failed", "skipped", "requeued_count"}
SAFE_STATUSES = {"passed", "failed", "blocked", "completed", "ok", "dry_run", "execution_unknown", "requeued"}
SAFE_MUTATIONS = {"memory_quality_run", "memory_quality_case_result", "memory_trace_index", "memory_quality_metric_point", "memory_quality_event", "memory_index_outbox", "memory_fts", "memory_index_document", "memory_projector_outbox", "memory_projection_snapshot"}


class DesktopCommandBridge:
    def __init__(self, ops: Any, service: Any):
        self.ops, self.service = ops, service

    @staticmethod
    def actor(identity: VerifiedIdentity) -> str:
        parts = (identity.profile_id, identity.subject_id, identity.agent_id, identity.client_instance_id, "main")
        return "desktop_" + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()

    def registry(self) -> DesktopAdminRegistry:
        commands = []
        for action in self.ops.list_admin_action_specs():
            name, risk = action["action_name"], action["risk_level"]
            adapter = ADAPTERS.get(name)
            available = adapter is not None and risk in {"read_only", "low", "medium"}
            model = adapter[2] if adapter else EmptyParams
            commands.append(DesktopAdminCommandSpec(
                command_id=adapter[0] if adapter else name, action_name=name,
                description=adapter[1] if adapter else action["description"], risk_level=risk,
                level="L1" if available else "L4" if risk == "break_glass" else "L3" if risk == "high" else None,
                available=available,
                unavailable_reason=None if available else "此动作尚缺少桌面目标修订、数据确认或安全输出适配；当前禁止执行。",
                parameter_schema=model.model_json_schema() if available else None,
                default_params=model().model_dump() if available and not any(field.is_required() for field in model.model_fields.values()) else None,
                supports_dry_run=available,
            ))
        return DesktopAdminRegistry(commands=commands)

    def _adapter(self, command_id: str):
        for action in self.registry().commands:
            if action.command_id == command_id and action.available:
                return action.action_name, ADAPTERS[action.action_name][2]
        raise ValueError("command is not available through the desktop adapter")

    def preview(self, identity: VerifiedIdentity, request: DesktopAdminPreviewRequest) -> DesktopAdminActionView:
        name, model = self._adapter(request.command_id)
        params = model.model_validate(request.params, strict=True).model_dump()
        action = self.ops.preview_admin_action(
            name, params, actor=self.actor(identity), role="operator", reason="desktop_debug_preview",
            ttl_seconds=300, idempotency_key=request.idempotency_key,
        )
        return self.view(identity, action)

    def view(self, identity: VerifiedIdentity, action: dict[str, Any], *, audit_read: bool = False) -> DesktopAdminActionView:
        owned = action["actor"] == self.actor(identity)
        if not action.get("idempotency_key") or action["reason"] != "desktop_debug_preview" or (not owned and not audit_read):
            raise ValueError("command preview belongs to another debug client")
        adapter = ADAPTERS.get(action["action_name"])
        if adapter is None:
            raise ValueError("unsupported command action")
        raw = action.get("impact_summary") or {}
        impact = {}
        for key in SAFE_IMPACT_KEYS:
            value = raw.get(key)
            if isinstance(value, (int, bool)):
                impact[key] = value
            elif key in {"queue_kind", "event_id", "expected_revision", "status"} and isinstance(value, str) and len(value) <= 128:
                impact[key] = value
            elif key == "change_scope" and isinstance(value, list):
                impact[key] = [item for item in value if isinstance(item, str) and item in {"all", "canonical", "gate", "recall", "projection", "forget", "persona_growth"}]
            elif key == "suites" and value == "all":
                impact[key] = "all"
            elif key == "package_name" and value == "default_memory_scenario_replay":
                impact[key] = value
        result = action.get("result") or {}
        summary = {}
        if isinstance(result, dict):
            if isinstance(result.get("status"), str) and result["status"] in SAFE_STATUSES:
                summary["status"] = result["status"]
            for key in SAFE_COUNT_KEYS:
                value = result.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    summary[key] = value
        # Table/field names are a closed schema from the selected provider's
        # preview. Keep only identifier-like strings; never return raw results.
        mutates = raw.get("mutates") or []
        impact["mutates"] = [item for item in mutates if isinstance(item, str) and item in SAFE_MUTATIONS][:20]
        active = self.service.readiness().get("active_turns")
        rollback = (action.get("rollback_plan") or {}).get("type")
        allowed_rollback = {"rebuild_indexes", "projection_rebuild", "none", "outbox_idempotent_replay", "inspect_queue_receipt"}
        return DesktopAdminActionView(
            action_id=action["id"], command_id=adapter[0], idempotency_key=action["idempotency_key"],
            params=adapter[2].model_validate(action["params"], strict=True).model_dump(),
            expected_revision=self.ops.preview_revision(action), execution_status=action["execution_status"],
            risk_level=action["risk_level"], active_turns=active if isinstance(active, int) else None,
            owned_by_current_client=owned,
            impact_summary=impact, rollback_type=rollback if isinstance(rollback, str) and rollback in allowed_rollback else None,
            result_summary=summary, audit_event_id=action["audit_event_id"],
            created_at=action["created_at"], expires_at=action["expires_at"], executed_at=action["executed_at"], dry_run=action["dry_run"],
        )

    def execute(self, identity: VerifiedIdentity, request: DesktopAdminExecuteRequest) -> DesktopAdminActionView:
        action = self.ops.get_admin_action(request.action_id)
        self.view(identity, action)
        self._adapter(ADAPTERS[action["action_name"]][0])
        if request.idempotency_key != action["idempotency_key"] or request.expected_revision != self.ops.preview_revision(action):
            raise ValueError("command request does not match its preview")
        result = self.ops.execute_admin_action(
            request.action_id, actor=self.actor(identity), expected_actor=self.actor(identity),
            expected_preview_hash=request.expected_revision,
            audit_scope={"profile_id_hash": hashlib.sha256(identity.profile_id.encode()).hexdigest()[:16],
                         "client_instance_hash": hashlib.sha256(identity.client_instance_id.encode()).hexdigest()[:16],
                         "session_hash": hashlib.sha256(identity.capability_id.encode()).hexdigest()[:16],
                         "window_label": "main", "permissions": ["commands.maintenance"]},
        )
        return self.view(identity, result)

    def history(self, identity: VerifiedIdentity) -> list[DesktopAdminActionView]:
        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        result = []
        for action in self.ops.list_admin_actions(limit=20, since=since, reason="desktop_debug_preview"):
            if action.get("idempotency_key") and action["action_name"] in ADAPTERS:
                result.append(self.view(identity, action, audit_read=True))
        return result


def install_desktop_command_routes(
    app: FastAPI, *, capabilities: LocalCapabilityProvider, stack: Any, service: Any,
    authenticate: Callable, require_protocol: Callable, require_owner: Callable,
) -> None:
    memory = getattr(stack.memory, "runtime", stack.memory)
    ops = getattr(memory, "quality_ops", None)
    if ops is None or not callable(getattr(ops, "preview_admin_action", None)):
        return
    bridge = DesktopCommandBridge(ops, service)
    session_lock = threading.Lock()
    active_sessions: dict[str, str] = {}

    def owner(request: Request, permission: str) -> VerifiedIdentity:
        require_protocol(request)
        identity = authenticate(request, capabilities, permission=permission)
        scope = getattr(service, "identity_scope", None)
        if scope != (identity.profile_id, identity.subject_id, identity.agent_id):
            raise GatewayServiceError(GatewayErrorCode.FORBIDDEN, "debug identity does not own this profile", status_code=403)
        require_owner(identity, memory)
        return identity

    def invalid():
        return GatewayServiceError(GatewayErrorCode.INVALID_REQUEST, "debug command could not be applied; reload its preview", status_code=409)

    from .desktop_governance import install_governance_routes
    install_governance_routes(app, memory=memory, owner=owner)
    from .desktop_queues import install_memory_queue_routes
    install_memory_queue_routes(app, memory=memory, owner=owner)
    from .desktop_quality import install_quality_routes
    install_quality_routes(app, ops=ops, owner=owner)

    @app.post("/v1/debug/quality/session")
    def quality_session(request: Request, payload: DesktopDebugSessionOpenRequest):
        identity = owner(request, "debug.session.open")
        origin = request.headers.get("origin")
        actor = bridge.actor(identity)
        with session_lock:
            previous = active_sessions.pop(actor, None)
            if previous:
                capabilities.revoke(previous)
            token, claims = capabilities.issue(profile_id=identity.profile_id, subject_id=identity.subject_id,
                agent_id=identity.agent_id, client_instance_id=identity.client_instance_id,
                permissions={"debug.commands.read", "debug.quality.read", "debug.quality.resolve"},
                allowed_origins=[origin] if origin else [], allow_missing_origin=origin is None, ttl_ms=300_000)
            active_sessions[actor] = claims.capability_id
        session = DesktopDebugSession(session_id="debug_" + hashlib.sha256(claims.capability_id.encode()).hexdigest()[:32],
            window_label="main", expires_at_ms=claims.expires_at_ms, scopes=["quality.read", "quality.resolve"])
        return {"session": session, "capability_token": token, "capability_id": claims.capability_id}

    @app.post("/v1/debug/reviews/session")
    def review_session(request: Request, payload: DesktopDebugSessionOpenRequest):
        identity = owner(request, "debug.session.open")
        origin = request.headers.get("origin")
        actor = bridge.actor(identity)
        with session_lock:
            old = active_sessions.pop(actor, None)
            if old:
                capabilities.revoke(old)
            token, claims = capabilities.issue(
                profile_id=identity.profile_id, subject_id=identity.subject_id, agent_id=identity.agent_id,
                client_instance_id=identity.client_instance_id,
                permissions={"debug.commands.read", "debug.review.read", "debug.review.resolve", "debug.quarantine.resolve"},
                allowed_origins=[origin] if origin else [], allow_missing_origin=origin is None, ttl_ms=300_000,
            )
            active_sessions[actor] = claims.capability_id
        session = DesktopDebugSession(session_id="debug_" + hashlib.sha256(claims.capability_id.encode()).hexdigest()[:32], window_label="main", expires_at_ms=claims.expires_at_ms, scopes=["memory.review.read", "memory.review.resolve", "memory.quarantine.resolve"])
        return {"session": session, "capability_token": token, "capability_id": claims.capability_id}

    @app.post("/v1/debug/companion/session")
    def companion_session(request: Request, payload: DesktopDebugSessionOpenRequest):
        identity = owner(request, "debug.session.open")
        if getattr(stack, "persona", None) is None or getattr(stack, "relationship", None) is None:
            raise invalid()
        origin = request.headers.get("origin")
        with session_lock:
            actor = bridge.actor(identity)
            old = active_sessions.get(actor)
            if old:
                capabilities.revoke(old)
            token, claims = capabilities.issue(
                profile_id=identity.profile_id, subject_id=identity.subject_id, agent_id=identity.agent_id,
                client_instance_id=identity.client_instance_id,
                permissions={"debug.commands.read", "debug.companion.manage"},
                allowed_origins=[origin] if origin else [], allow_missing_origin=origin is None, ttl_ms=300_000,
            )
            active_sessions[actor] = claims.capability_id
        session = DesktopDebugSession(session_id="debug_" + hashlib.sha256(claims.capability_id.encode()).hexdigest()[:32],
            window_label="main", expires_at_ms=claims.expires_at_ms, scopes=["companion.manage"])
        return {"session": session, "capability_token": token, "capability_id": claims.capability_id}

    @app.post("/v1/debug/sessions/open")
    def open_session(request: Request, payload: DesktopDebugSessionOpenRequest):
        identity = owner(request, "debug.session.open")
        origin = request.headers.get("origin")
        with session_lock:
            actor = bridge.actor(identity)
            previous = active_sessions.get(actor)
            if previous:
                capabilities.revoke(previous)
            token, claims = capabilities.issue(
                profile_id=identity.profile_id, subject_id=identity.subject_id, agent_id=identity.agent_id,
                client_instance_id=identity.client_instance_id,
                permissions={"debug.commands.read", "debug.commands.preview", "debug.commands.execute"},
                allowed_origins=[origin] if origin else [], allow_missing_origin=origin is None, ttl_ms=300_000,
            )
            active_sessions[actor] = claims.capability_id
        session = DesktopDebugSession(
            session_id="debug_" + hashlib.sha256(claims.capability_id.encode()).hexdigest()[:32],
            window_label=payload.window_label, expires_at_ms=claims.expires_at_ms,
            scopes=["commands.read", "commands.maintenance"],
        )
        # Native broker consumes the credential. This response must never be
        # forwarded verbatim into the WebView.
        return {"session": session, "capability_token": token, "capability_id": claims.capability_id}

    @app.delete("/v1/debug/sessions/{session_id}")
    def close_session(request: Request, session_id: str):
        identity = owner(request, "debug.commands.read")
        expected = "debug_" + hashlib.sha256(identity.capability_id.encode()).hexdigest()[:32]
        if session_id != expected:
            raise invalid()
        capabilities.revoke(identity.capability_id)
        with session_lock:
            if active_sessions.get(bridge.actor(identity)) == identity.capability_id:
                active_sessions.pop(bridge.actor(identity), None)
        return {"closed": True}

    @app.get("/v1/debug/commands")
    def commands(request: Request):
        identity = owner(request, "debug.commands.read")
        return bridge.registry().model_copy(update={"history": bridge.history(identity)})

    @app.post("/v1/debug/commands/preview")
    def preview(request: Request, payload: DesktopAdminPreviewRequest):
        identity = owner(request, "debug.commands.preview")
        try:
            return bridge.preview(identity, payload)
        except ValueError:
            raise invalid() from None

    @app.post("/v1/debug/commands/execute")
    async def execute(request: Request, payload: DesktopAdminExecuteRequest):
        identity = owner(request, "debug.commands.execute")
        try:
            return await run_in_threadpool(bridge.execute, identity, payload)
        except ValueError:
            raise invalid() from None

    @app.get("/v1/debug/commands/actions/{action_id}")
    def read_action(request: Request, action_id: str):
        identity = owner(request, "debug.commands.read")
        try:
            validate_identifier(action_id)
            return bridge.view(identity, bridge.ops.get_admin_action(action_id), audit_read=True)
        except ValueError:
            raise invalid() from None
