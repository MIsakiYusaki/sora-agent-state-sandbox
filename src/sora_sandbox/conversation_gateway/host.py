from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Header, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from ..model_provider.admin import (
    BindWorkloadRequest,
    CreateProfileRequest,
    CredentialInput,
    DeleteProfileRequest,
    ModelProviderAdminService,
    RotateCredentialRequest,
    SetupModelRoutesRequest,
    VerifyModelRequest,
)
from ..model_provider.errors import ModelProviderError, RevisionConflict

from .contracts import (
    AckCommand,
    CancelCommand,
    CancelRequest,
    ConnectionReady,
    GatewayError,
    GatewayErrorCode,
    PingCommand,
    PROTOCOL_VERSION,
    ResumeCommand,
    SubmitCommand,
    TurnSubmit,
    parse_connection_command,
    protocol_minor,
    validate_correlation_identifier,
    validate_identifier,
)
from .security import CapabilityError, LocalCapabilityProvider, VerifiedIdentity
from .service import ConversationGatewayService, GatewayServiceError


class MemoryVisibilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visible: bool


class ConversationControlUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    control: str
    value: Any
    scope: str = "session"
    duration_seconds: int | None = None


def create_gateway_app(
    service: ConversationGatewayService,
    capability_provider: LocalCapabilityProvider,
    *,
    own_service_lifecycle: bool = True,
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "testserver"),
    shutdown_callback: Callable[[], None] | None = None,
    model_provider_admin: ModelProviderAdminService | None = None,
    memory_runtime: Any | None = None,
    interruptibility_service: Any | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if own_service_lifecycle:
            service.start()
        try:
            yield
        finally:
            if own_service_lifecycle:
                service.close()

    app = FastAPI(
        title="Project Sora Conversation Gateway",
        version=PROTOCOL_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.gateway_service = service
    app.state.capability_provider = capability_provider
    app.state.model_provider_admin = model_provider_admin
    app.state.memory_runtime = memory_runtime
    app.state.interruptibility_service = interruptibility_service
    trusted_hosts = tuple(host.lower() for host in allowed_hosts)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(trusted_hosts))

    @app.middleware("http")
    async def security_boundary(
        request: Request, call_next: Callable[[Request], Awaitable[object]]
    ):
        started = time.perf_counter()
        if request.url.path.startswith("/v1/"):
            length = request.headers.get("content-length")
            try:
                oversized = length is not None and int(length) > service.policy.max_frame_bytes
            except ValueError:
                oversized = True
            if oversized:
                response = _error_response(
                    GatewayServiceError(
                        GatewayErrorCode.PAYLOAD_TOO_LARGE,
                        "request body exceeds the gateway limit",
                        status_code=413,
                    ),
                    correlation_id=_correlation_id(request),
                )
                return _finalize_http_response(
                    response,
                    request=request,
                    service=service,
                    started=started,
                )
        response = await call_next(request)
        return _finalize_http_response(
            response,
            request=request,
            service=service,
            started=started,
        )

    @app.exception_handler(GatewayServiceError)
    async def service_error_handler(request: Request, exc: GatewayServiceError):
        service.metrics.increment(
            "gateway_error_total", labels={"code": exc.code.value}
        )
        return _error_response(exc, correlation_id=_correlation_id(request))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        service.metrics.increment(
            "gateway_error_total", labels={"code": "INVALID_REQUEST"}
        )
        error = GatewayServiceError(
            GatewayErrorCode.INVALID_REQUEST,
            "request does not satisfy the protocol contract",
            status_code=422,
            details={"errors": _safe_validation_errors(exc.errors())},
        )
        return _error_response(error, correlation_id=_correlation_id(request))

    @app.get("/health/live")
    async def liveness() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    async def readiness() -> JSONResponse:
        result = service.readiness()
        return JSONResponse(result, status_code=200 if result["status"] == "ready" else 503)

    if model_provider_admin is not None:
        @app.get("/v1/model-providers")
        async def list_model_providers(request: Request):
            _require_protocol(request)
            identity = _authenticate(
                request, capability_provider, permission="model_provider.read"
            )
            _require_provider_owner(identity, model_provider_admin)
            return await _run_provider_admin(model_provider_admin.list_configuration)

        @app.get("/v1/model-providers/health")
        async def model_provider_health(request: Request):
            _require_protocol(request)
            identity = _authenticate(
                request, capability_provider, permission="model_provider.read"
            )
            _require_provider_owner(identity, model_provider_admin)
            return await _run_provider_admin(model_provider_admin.health)

        @app.post("/v1/model-providers/maintenance")
        async def maintain_model_providers(request: Request):
            _require_protocol(request)
            identity = _authenticate(
                request, capability_provider, permission="model_provider.write"
            )
            _require_provider_owner(identity, model_provider_admin)
            return await _run_provider_admin(model_provider_admin.maintain)

        @app.post("/v1/model-providers", status_code=201)
        async def create_model_provider(request: Request, payload: CreateProfileRequest):
            _require_protocol(request)
            identity = _authenticate(
                request, capability_provider, permission="model_provider.write"
            )
            _require_provider_owner(identity, model_provider_admin)
            return await _run_provider_admin(model_provider_admin.create_profile, payload)

        @app.put("/v1/model-providers/{profile_id}/credential")
        async def save_model_provider_credential(
            request: Request, profile_id: str, payload: CredentialInput
        ):
            _require_protocol(request)
            identity = _authenticate(
                request, capability_provider, permission="model_provider.write"
            )
            _require_provider_owner(identity, model_provider_admin)
            _require_path_identifier(profile_id, field="profile_id")
            return await _run_provider_admin(
                model_provider_admin.save_credential, profile_id, payload
            )

        @app.post("/v1/model-providers/{profile_id}/verify")
        async def verify_model_provider(
            request: Request, profile_id: str, payload: VerifyModelRequest
        ):
            _require_protocol(request)
            identity = _authenticate(
                request, capability_provider, permission="model_provider.verify"
            )
            _require_provider_owner(identity, model_provider_admin)
            _require_path_identifier(profile_id, field="profile_id")
            return await _run_provider_admin(
                model_provider_admin.verify_model, profile_id, payload
            )

        @app.post("/v1/model-providers/{profile_id}/setup-default-routes")
        async def setup_model_provider_routes(
            request: Request, profile_id: str, payload: SetupModelRoutesRequest
        ):
            _require_protocol(request)
            identity = _authenticate(
                request, capability_provider, permission="model_provider.bind"
            )
            _require_provider_owner(identity, model_provider_admin)
            _require_path_identifier(profile_id, field="profile_id")
            return await _run_provider_admin(
                model_provider_admin.setup_model_routes, profile_id, payload
            )

        @app.post("/v1/model-providers/{profile_id}/credential/rotate")
        async def rotate_model_provider_credential(
            request: Request, profile_id: str, payload: RotateCredentialRequest
        ):
            _require_protocol(request)
            identity = _authenticate(
                request, capability_provider, permission="model_provider.write"
            )
            _require_provider_owner(identity, model_provider_admin)
            _require_path_identifier(profile_id, field="profile_id")
            return await _run_provider_admin(
                model_provider_admin.rotate_credential, profile_id, payload
            )

        @app.put("/v1/model-provider-bindings/{workload}")
        async def bind_model_provider_workload(
            request: Request, workload: str, payload: BindWorkloadRequest
        ):
            _require_protocol(request)
            identity = _authenticate(
                request, capability_provider, permission="model_provider.bind"
            )
            _require_provider_owner(identity, model_provider_admin)
            if workload != payload.workload.value:
                raise GatewayServiceError(
                    GatewayErrorCode.INVALID_REQUEST,
                    "path and body workloads differ",
                    status_code=400,
                )
            return await _run_provider_admin(model_provider_admin.bind_workload, payload)

        @app.delete("/v1/model-providers/{profile_id}")
        async def delete_model_provider(
            request: Request, profile_id: str, payload: DeleteProfileRequest
        ):
            _require_protocol(request)
            identity = _authenticate(
                request, capability_provider, permission="model_provider.delete"
            )
            _require_provider_owner(identity, model_provider_admin)
            _require_path_identifier(profile_id, field="profile_id")
            return await _run_provider_admin(
                model_provider_admin.delete_profile, profile_id, payload
            )

    @app.post("/v1/turns", status_code=202)
    async def submit_turn(request: Request, payload: TurnSubmit):
        _require_protocol(request, payload.protocol_version)
        identity = _authenticate(
            request,
            capability_provider,
            permission="turn.submit",
            client_instance_id=payload.client_instance_id,
        )
        return service.submit(identity, payload)

    @app.get("/v1/turns/{turn_id}")
    async def get_turn(request: Request, turn_id: str):
        _require_protocol(request)
        identity = _authenticate(request, capability_provider, permission="turn.read")
        _require_path_identifier(turn_id, field="turn_id")
        return service.status(identity, turn_id)

    @app.get("/v1/turns/{turn_id}/events")
    async def stream_turn_events(
        request: Request,
        turn_id: str,
        after_sequence: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ):
        _require_protocol(request)
        identity = _authenticate(request, capability_provider, permission="turn.read")
        _require_path_identifier(turn_id, field="turn_id")
        cursor = after_sequence
        if last_event_id:
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError as exc:
                raise GatewayServiceError(
                    GatewayErrorCode.INVALID_REQUEST,
                    "Last-Event-ID must be an integer sequence",
                    status_code=400,
                ) from exc
        service.authorize_turn(identity, turn_id)
        if cursor:
            service.metrics.increment(
                "gateway_stream_resume_total", labels={"transport": "sse"}
            )

        async def generate() -> AsyncIterator[bytes]:
            iterator = service.iter_events(identity, turn_id, after_sequence=cursor)
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        has_item, event = await asyncio.to_thread(_next_item, iterator)
                    except GeneratorExit:
                        return
                    if not has_item:
                        return
                    if event is None:
                        yield b": heartbeat\n\n"
                    else:
                        payload = event.model_dump(mode="json")
                        body = json.dumps(
                            payload, ensure_ascii=False, separators=(",", ":")
                        )
                        yield (
                            f"id: {event.sequence}\nevent: {event.event_type}\n"
                            f"data: {body}\n\n"
                        ).encode("utf-8")
            except GatewayServiceError as exc:
                payload = _gateway_error(exc, _correlation_id(request)).model_dump(
                    mode="json"
                )
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                yield f"event: error\ndata: {body}\n\n".encode("utf-8")

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.post("/v1/turns/{turn_id}/cancel")
    async def cancel_turn(request: Request, turn_id: str, payload: CancelRequest):
        _require_protocol(request, payload.protocol_version)
        identity = _authenticate(request, capability_provider, permission="turn.cancel")
        _require_path_identifier(turn_id, field="turn_id")
        return service.cancel(identity, turn_id, reason=payload.reason)

    @app.get("/v1/conversations/{conversation_id}/history")
    async def get_history(
        request: Request,
        conversation_id: str,
        limit: int | None = Query(default=None, ge=1, le=200),
        before_sequence: int | None = Query(default=None, ge=1),
    ):
        _require_protocol(request)
        identity = _authenticate(request, capability_provider, permission="history.read")
        _require_path_identifier(conversation_id, field="conversation_id")
        return service.history(
            identity,
            conversation_id,
            limit=limit,
            before_sequence=before_sequence,
        )

    @app.get("/v1/conversations")
    async def list_conversations(
        request: Request,
        limit: int = Query(default=100, ge=1, le=200),
    ):
        _require_protocol(request)
        identity = _authenticate(request, capability_provider, permission="history.read")
        return service.conversations(identity, limit=limit)

    if memory_runtime is not None:
        @app.get("/v1/memories")
        async def list_memories(
            request: Request,
            query: str = Query(default="", max_length=200),
        ):
            _require_protocol(request)
            identity = _authenticate(request, capability_provider, permission="memory.read")
            _require_memory_owner(identity, memory_runtime)
            raw = (
                memory_runtime.search(query, top_k=100)
                if query.strip()
                else memory_runtime.list_memories()
            )
            items = [_user_memory(item) for item in raw if str(item.get("status")) == "active"]
            return {
                "schema_version": "UserMemoryPage.v1",
                "items": items,
                "summary": {
                    "available": len(items),
                    "visible": sum(1 for item in items if item["visible"]),
                    "hidden": sum(1 for item in items if not item["visible"]),
                    "archived": sum(
                        1
                        for item in items
                        if str((item.get("lifecycle") or {}).get("tier")) == "archived"
                    ),
                },
            }

        @app.put("/v1/memories/{memory_id}/visibility")
        async def set_memory_visibility(
            request: Request,
            memory_id: str,
            payload: MemoryVisibilityRequest,
        ):
            _require_protocol(request)
            identity = _authenticate(request, capability_provider, permission="memory.write")
            _require_memory_owner(identity, memory_runtime)
            _require_path_identifier(memory_id, field="memory_id")
            try:
                result = (
                    memory_runtime.clear_surface_override(memory_id)
                    if payload.visible
                    else memory_runtime.hide_memory(memory_id)
                )
            except ValueError as exc:
                raise GatewayServiceError(
                    GatewayErrorCode.INVALID_REQUEST,
                    "memory could not be updated",
                    status_code=404,
                ) from exc
            return {
                "schema_version": "UserMemoryMutation.v1",
                "memory": _user_memory(result["memory"]),
            }

        @app.delete("/v1/memories/{memory_id}")
        async def forget_memory(request: Request, memory_id: str):
            _require_protocol(request)
            identity = _authenticate(request, capability_provider, permission="memory.write")
            _require_memory_owner(identity, memory_runtime)
            _require_path_identifier(memory_id, field="memory_id")
            try:
                result = memory_runtime.forget_memory(
                    memory_id,
                    reason="desktop_user_forget",
                    actor="desktop_user",
                )
            except ValueError as exc:
                raise GatewayServiceError(
                    GatewayErrorCode.INVALID_REQUEST,
                    "memory could not be forgotten",
                    status_code=404,
                ) from exc
            return {
                "schema_version": "UserMemoryForgetReceipt.v1",
                "memory_id": memory_id,
                "status": str(result.get("status") or "accepted"),
            }

    if interruptibility_service is not None:
        @app.get("/v1/conversations/{conversation_id}/controls")
        async def get_conversation_controls(request: Request, conversation_id: str):
            _require_protocol(request)
            identity = _authenticate(
                request, capability_provider, permission="controls.read"
            )
            _require_path_identifier(conversation_id, field="conversation_id")
            with service.state_repository.turn_state(
                identity.profile_id,
                conversation_id,
                user_id=identity.subject_id,
                agent_id=identity.agent_id,
                pair_id=identity.pair_id,
            ) as state:
                status = interruptibility_service.status(state)
            return {
                "schema_version": "ConversationControls.v1",
                "conversation_id": conversation_id,
                "controls": status.get("controls", {}),
                "initiative_lease": status.get("initiative_lease"),
            }

        @app.put("/v1/conversations/{conversation_id}/controls")
        async def update_conversation_controls(
            request: Request,
            conversation_id: str,
            payload: ConversationControlUpdate,
        ):
            _require_protocol(request)
            identity = _authenticate(
                request, capability_provider, permission="controls.write"
            )
            _require_path_identifier(conversation_id, field="conversation_id")
            with service.state_repository.turn_state(
                identity.profile_id,
                conversation_id,
                user_id=identity.subject_id,
                agent_id=identity.agent_id,
                pair_id=identity.pair_id,
            ) as state:
                try:
                    result = interruptibility_service.set_control(
                        state,
                        control=payload.control,
                        value=payload.value,
                        scope=payload.scope,
                        duration_seconds=payload.duration_seconds,
                    )
                except ValueError as exc:
                    raise GatewayServiceError(
                        GatewayErrorCode.INVALID_REQUEST,
                        "conversation control is not supported",
                        status_code=400,
                    ) from exc
            return {
                "schema_version": "ConversationControls.v1",
                "conversation_id": conversation_id,
                "controls": result["status"].get("controls", {}),
                "initiative_lease": result["status"].get("initiative_lease"),
            }

        @app.delete("/v1/conversations/{conversation_id}/controls")
        async def reset_conversation_controls(request: Request, conversation_id: str):
            _require_protocol(request)
            identity = _authenticate(
                request, capability_provider, permission="controls.write"
            )
            _require_path_identifier(conversation_id, field="conversation_id")
            with service.state_repository.turn_state(
                identity.profile_id,
                conversation_id,
                user_id=identity.subject_id,
                agent_id=identity.agent_id,
                pair_id=identity.pair_id,
            ) as state:
                interruptibility_service.revoke(state, "all")
                status = interruptibility_service.status(state)
            return {
                "schema_version": "ConversationControls.v1",
                "conversation_id": conversation_id,
                "controls": status.get("controls", {}),
                "initiative_lease": status.get("initiative_lease"),
            }

    @app.websocket("/v1/stream")
    async def websocket_stream(websocket: WebSocket) -> None:
        identity: VerifiedIdentity | None = None
        try:
            _require_websocket_host(websocket, trusted_hosts)
            _require_websocket_protocol(websocket)
            identity = _authenticate_websocket(
                websocket, capability_provider, permission="stream.connect"
            )
            service.reserve_connection(identity)
        except GatewayServiceError as exc:
            await websocket.close(code=_websocket_close_code(exc.code), reason=str(exc)[:120])
            return
        await websocket.accept(subprotocol=f"sora.v{PROTOCOL_VERSION}")
        connection_id = f"connection_{uuid.uuid4().hex}"
        outgoing: asyncio.Queue[dict[str, object]] = asyncio.Queue(
            maxsize=service.policy.websocket_queue_size
        )
        pumps: set[asyncio.Task[None]] = set()
        pumps_by_turn: dict[str, asyncio.Task[None]] = {}
        closed = asyncio.Event()

        async def enqueue(payload: dict[str, object]) -> None:
            try:
                await _enqueue_with_timeout(
                    outgoing,
                    payload,
                    timeout_ms=service.policy.slow_consumer_grace_ms,
                )
            except TimeoutError:
                service.metrics.increment(
                    "gateway_stream_disconnect_total",
                    labels={"reason": "slow_consumer"},
                )
                closed.set()
                raise

        async def sender() -> None:
            heartbeat = service.policy.heartbeat_interval_ms / 1000
            while not closed.is_set():
                try:
                    payload = await asyncio.wait_for(outgoing.get(), timeout=heartbeat)
                except TimeoutError:
                    payload = {"type": "heartbeat", "at_ms": int(time.time() * 1000)}
                await websocket.send_json(payload)

        async def pump(turn_id: str, after_sequence: int) -> None:
            try:
                iterator = service.iter_events(
                    identity, turn_id, after_sequence=after_sequence
                )
                while not closed.is_set():
                    try:
                        has_item, event = await asyncio.to_thread(_next_item, iterator)
                    except GeneratorExit:
                        return
                    if not has_item:
                        return
                    if event is not None:
                        await enqueue(
                            {"type": "event", "payload": event.model_dump(mode="json")}
                        )
            except GatewayServiceError as exc:
                await enqueue(
                    {
                        "type": "error",
                        "payload": _gateway_error(exc, uuid.uuid4().hex).model_dump(
                            mode="json"
                        ),
                    }
                )
            finally:
                pumps_by_turn.pop(turn_id, None)

        def start_pump(turn_id: str, after_sequence: int) -> None:
            previous = pumps_by_turn.get(turn_id)
            if previous is not None:
                previous.cancel()
            task = asyncio.create_task(pump(turn_id, after_sequence))
            pumps.add(task)
            pumps_by_turn[turn_id] = task
            task.add_done_callback(pumps.discard)

        sender_task = asyncio.create_task(sender())
        try:
            await enqueue(
                {
                    "type": "connection.ready",
                    "payload": ConnectionReady(
                        connection_id=connection_id,
                        heartbeat_interval_ms=service.policy.heartbeat_interval_ms,
                        max_frame_bytes=service.policy.max_frame_bytes,
                    ).model_dump(mode="json"),
                }
            )
            while not closed.is_set():
                raw = await websocket.receive_text()
                if len(raw.encode("utf-8")) > service.policy.max_frame_bytes:
                    raise GatewayServiceError(
                        GatewayErrorCode.PAYLOAD_TOO_LARGE,
                        "websocket frame exceeds the gateway limit",
                        status_code=413,
                    )
                try:
                    command = parse_connection_command(json.loads(raw))
                except (json.JSONDecodeError, ValidationError) as exc:
                    await enqueue(
                        {
                            "type": "error",
                            "payload": GatewayError(
                                code=GatewayErrorCode.INVALID_REQUEST,
                                message="websocket command is invalid",
                                correlation_id=uuid.uuid4().hex,
                            ).model_dump(mode="json"),
                        }
                    )
                    continue
                if isinstance(command, SubmitCommand):
                    accepted = await asyncio.to_thread(
                        service.submit, identity, command.payload
                    )
                    await enqueue(
                        {
                            "type": "turn.accepted",
                            "payload": accepted.model_dump(mode="json"),
                        }
                    )
                    start_pump(accepted.turn_id, 0)
                elif isinstance(command, ResumeCommand):
                    await asyncio.to_thread(
                        service.authorize_turn, identity, command.turn_id
                    )
                    start_pump(command.turn_id, command.after_sequence)
                elif isinstance(command, CancelCommand):
                    status = await asyncio.to_thread(
                        service.cancel,
                        identity,
                        command.turn_id,
                        reason=command.reason,
                    )
                    await enqueue(
                        {"type": "turn.status", "payload": status.model_dump(mode="json")}
                    )
                elif isinstance(command, AckCommand):
                    await asyncio.to_thread(
                        service.acknowledge,
                        identity,
                        command.turn_id,
                        command.sequence,
                    )
                elif isinstance(command, PingCommand):
                    await enqueue({"type": "pong", "nonce": command.nonce})
        except WebSocketDisconnect:
            pass
        except GatewayServiceError as exc:
            try:
                await websocket.send_json(
                    {
                        "type": "error",
                        "payload": _gateway_error(exc, uuid.uuid4().hex).model_dump(
                            mode="json"
                        ),
                    }
                )
            finally:
                await websocket.close(code=_websocket_close_code(exc.code))
        except TimeoutError:
            await websocket.close(code=1013, reason="slow consumer")
        finally:
            closed.set()
            sender_task.cancel()
            for task in tuple(pumps):
                task.cancel()
            await asyncio.gather(sender_task, *pumps, return_exceptions=True)
            if identity is not None:
                service.release_connection(identity)

    @app.post("/v1/admin/shutdown", status_code=202)
    async def shutdown_gateway(request: Request):
        _require_protocol(request)
        _authenticate(request, capability_provider, permission="gateway.shutdown")
        service.begin_drain()
        if shutdown_callback is not None:
            shutdown_callback()
        return {"status": "draining"}

    return app


def _authenticate(
    request: Request,
    provider: LocalCapabilityProvider,
    *,
    permission: str,
    client_instance_id: str | None = None,
) -> VerifiedIdentity:
    token = _bearer_token(request.headers.get("authorization"))
    header_instance = request.headers.get("x-sora-client-instance")
    if not header_instance:
        raise GatewayServiceError(
            GatewayErrorCode.AUTH_REQUIRED,
            "X-Sora-Client-Instance is required",
            status_code=401,
        )
    if client_instance_id is not None and header_instance != client_instance_id:
        raise GatewayServiceError(
            GatewayErrorCode.FORBIDDEN,
            "body and header client instances differ",
            status_code=403,
        )
    try:
        return provider.verify(
            token,
            permission=permission,
            origin=request.headers.get("origin"),
            client_instance_id=header_instance,
        )
    except CapabilityError as exc:
        raise GatewayServiceError(
            GatewayErrorCode.FORBIDDEN,
            "capability is invalid for this operation",
            status_code=403,
        ) from exc


def _require_provider_owner(
    identity: VerifiedIdentity,
    admin: ModelProviderAdminService,
) -> None:
    owner = admin.system.owner
    if (
        identity.profile_id != owner.tenant_id
        or identity.subject_id != owner.user_id
        or identity.agent_id != owner.agent_id
    ):
        raise GatewayServiceError(
            GatewayErrorCode.FORBIDDEN,
            "capability identity does not own this provider configuration",
            status_code=403,
        )


def _require_memory_owner(identity: VerifiedIdentity, memory_runtime: Any) -> None:
    if (
        identity.subject_id != str(memory_runtime.owner_user_id)
        or identity.agent_id != str(memory_runtime.agent_id)
    ):
        raise GatewayServiceError(
            GatewayErrorCode.FORBIDDEN,
            "capability identity does not own this memory store",
            status_code=403,
        )


def _user_memory(item: dict[str, Any]) -> dict[str, Any]:
    permission = str(item.get("surface_permission") or "auto")
    lifecycle = item.get("lifecycle")
    return {
        "id": str(item.get("id") or ""),
        "type": str(item.get("type") or "memory"),
        "summary": str(item.get("summary") or item.get("content") or ""),
        "tags": [str(value) for value in item.get("tags") or []][:20],
        "confidence": float(item.get("confidence") or 0.0),
        "sensitivity": str(item.get("sensitivity") or "low"),
        "evidence_count": len(item.get("evidence_ids") or []),
        "surface_permission": permission,
        "visible": permission not in {"none", "hidden", "deny"},
        "lifecycle": lifecycle if isinstance(lifecycle, dict) else {},
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


async def _run_provider_admin(operation: Callable[..., Any], *args: Any):
    try:
        return await asyncio.to_thread(operation, *args)
    except RevisionConflict as exc:
        raise GatewayServiceError(
            GatewayErrorCode.VERSION_CONFLICT,
            "provider configuration changed; refresh and retry",
            status_code=409,
            details={"provider_error_code": exc.code.value},
        ) from exc
    except ModelProviderError as exc:
        raise GatewayServiceError(
            GatewayErrorCode.INVALID_REQUEST,
            str(exc)[:300],
            status_code=422,
            retryable=exc.retryable,
            details={"provider_error_code": exc.code.value},
        ) from exc
    except ValueError as exc:
        raise GatewayServiceError(
            GatewayErrorCode.INVALID_REQUEST,
            str(exc)[:300],
            status_code=422,
        ) from exc


def _authenticate_websocket(
    websocket: WebSocket,
    provider: LocalCapabilityProvider,
    *,
    permission: str,
) -> VerifiedIdentity:
    token = _bearer_token(websocket.headers.get("authorization"))
    client_instance_id = websocket.headers.get("x-sora-client-instance")
    if not client_instance_id:
        raise GatewayServiceError(
            GatewayErrorCode.AUTH_REQUIRED,
            "X-Sora-Client-Instance is required",
            status_code=401,
        )
    try:
        return provider.verify(
            token,
            permission=permission,
            origin=websocket.headers.get("origin"),
            client_instance_id=client_instance_id,
        )
    except CapabilityError as exc:
        raise GatewayServiceError(
            GatewayErrorCode.FORBIDDEN,
            "capability is invalid for this stream",
            status_code=403,
        ) from exc


def _bearer_token(value: str | None) -> str:
    if not value or not value.startswith("Bearer "):
        raise GatewayServiceError(
            GatewayErrorCode.AUTH_REQUIRED,
            "bearer capability is required",
            status_code=401,
        )
    token = value[7:].strip()
    if not token:
        raise GatewayServiceError(
            GatewayErrorCode.AUTH_REQUIRED,
            "bearer capability is required",
            status_code=401,
        )
    return token


def _require_protocol(request: Request, body_version: str | None = None) -> None:
    supplied = request.headers.get("x-sora-protocol-version") or body_version
    try:
        protocol_minor(supplied or "")
    except ValueError as exc:
        raise GatewayServiceError(
            GatewayErrorCode.PROTOCOL_UNSUPPORTED,
            f"supported protocol version is {PROTOCOL_VERSION}",
            status_code=426,
        ) from exc
    if body_version is not None and supplied != body_version:
        raise GatewayServiceError(
            GatewayErrorCode.PROTOCOL_UNSUPPORTED,
            "header and body protocol versions differ",
            status_code=400,
        )


def _require_websocket_protocol(websocket: WebSocket) -> None:
    requested = websocket.headers.get("sec-websocket-protocol", "")
    offered = {item.strip() for item in requested.split(",") if item.strip()}
    if f"sora.v{PROTOCOL_VERSION}" not in offered:
        raise GatewayServiceError(
            GatewayErrorCode.PROTOCOL_UNSUPPORTED,
            f"websocket subprotocol sora.v{PROTOCOL_VERSION} is required",
            status_code=426,
        )


def _require_websocket_host(
    websocket: WebSocket, allowed_hosts: tuple[str, ...]
) -> None:
    supplied = (websocket.headers.get("host") or "").strip().lower()
    if not supplied or any(ord(character) < 32 for character in supplied):
        raise GatewayServiceError(
            GatewayErrorCode.FORBIDDEN,
            "websocket host is not allowed",
            status_code=403,
        )
    if supplied.startswith("["):
        end = supplied.find("]")
        host = supplied[1:end] if end > 0 else ""
    else:
        host = supplied.rsplit(":", 1)[0] if supplied.count(":") == 1 else supplied
    accepted = any(
        pattern == "*"
        or host == pattern
        or (pattern.startswith("*.") and host.endswith(pattern[1:]))
        for pattern in allowed_hosts
    )
    if not accepted:
        raise GatewayServiceError(
            GatewayErrorCode.FORBIDDEN,
            "websocket host is not allowed",
            status_code=403,
        )


def _require_path_identifier(value: str, *, field: str) -> str:
    try:
        return validate_identifier(value)
    except ValueError as exc:
        raise GatewayServiceError(
            GatewayErrorCode.INVALID_REQUEST,
            f"{field} is invalid",
            status_code=422,
        ) from exc


def _gateway_error(exc: GatewayServiceError, correlation_id: str) -> GatewayError:
    return GatewayError(
        code=exc.code,
        message=str(exc),
        retryable=exc.retryable,
        retry_after_ms=exc.retry_after_ms,
        correlation_id=correlation_id,
        details=exc.details,
    )


def _error_response(exc: GatewayServiceError, *, correlation_id: str) -> JSONResponse:
    headers: dict[str, str] = {}
    if exc.retry_after_ms is not None:
        headers["Retry-After"] = str(max(1, exc.retry_after_ms // 1000))
    return JSONResponse(
        _gateway_error(exc, correlation_id).model_dump(mode="json"),
        status_code=exc.status_code,
        headers=headers,
    )


def _correlation_id(request: Request) -> str:
    existing = getattr(request.state, "correlation_id", None)
    if existing:
        return str(existing)
    supplied = request.headers.get("x-sora-correlation-id")
    try:
        value = validate_correlation_identifier(supplied or "")
    except ValueError:
        value = uuid.uuid4().hex
    request.state.correlation_id = value
    return value


def _safe_validation_errors(errors: list[dict[str, object]]) -> dict[str, object]:
    return {
        "fields": [
            ".".join(str(item) for item in error.get("loc", ()))
            for error in errors[:20]
        ],
        "count": len(errors),
    }


def _websocket_close_code(code: GatewayErrorCode) -> int:
    if code in {GatewayErrorCode.AUTH_REQUIRED, GatewayErrorCode.FORBIDDEN}:
        return 4403
    if code == GatewayErrorCode.PROTOCOL_UNSUPPORTED:
        return 4406
    if code in {GatewayErrorCode.RATE_LIMITED, GatewayErrorCode.DRAINING}:
        return 1013
    return 4400


def _finalize_http_response(response, *, request: Request, service, started: float):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Sora-Protocol-Version"] = PROTOCOL_VERSION
    response.headers["X-Sora-Correlation-Id"] = _correlation_id(request)
    route = request.scope.get("route")
    route_path = str(getattr(route, "path", "unmatched"))
    service.metrics.increment(
        "gateway_http_request_total",
        labels={
            "method": request.method,
            "route": route_path,
            "status": response.status_code,
        },
    )
    service.metrics.observe(
        "gateway_http_request_ms",
        (time.perf_counter() - started) * 1000,
        labels={"method": request.method, "route": route_path},
    )
    return response


def _next_item(iterator):
    try:
        return True, next(iterator)
    except StopIteration:
        return False, None


async def _enqueue_with_timeout(
    outgoing: asyncio.Queue[dict[str, object]],
    payload: dict[str, object],
    *,
    timeout_ms: int,
) -> None:
    await asyncio.wait_for(outgoing.put(payload), timeout=timeout_ms / 1000)
