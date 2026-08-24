from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Header, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

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


def create_gateway_app(
    service: ConversationGatewayService,
    capability_provider: LocalCapabilityProvider,
    *,
    own_service_lifecycle: bool = True,
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "testserver"),
    shutdown_callback: Callable[[], None] | None = None,
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
