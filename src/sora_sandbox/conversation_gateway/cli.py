from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
from pathlib import Path

import uvicorn

from ..app import configure_stdio, make_llm
from ..credentials import CredentialError
from ..model_provider import OwnerScope
from ..model_provider.admin import ModelProviderAdminService
from ..paths import user_config_dir, workspace_state_dir
from ..runtime_stack import SoraRuntimeStack
from .host import create_gateway_app
from .registry import GatewayRegistry
from .runtime_port import LocalConversationRuntimeAdapter
from .security import LocalCapabilityProvider
from .service import ConversationGatewayService, GatewayPolicy
from .state import ProfileStateRepository


ALL_PERMISSIONS = frozenset(
    {
        "stream.connect",
        "turn.submit",
        "turn.read",
        "turn.cancel",
        "history.read",
        "memory.read",
        "memory.write",
        "controls.read",
        "controls.write",
        "debug.read",
        "debug.observe",
        "debug.session.open",
        "gateway.shutdown",
        "model_provider.read",
        "model_provider.write",
        "model_provider.verify",
        "model_provider.bind",
        "model_provider.delete",
    }
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Project Sora local conversation gateway"
    )
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1"])
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--profile-id", default=os.environ.get("SORA_PROFILE_ID"))
    parser.add_argument("--subject-id", default=os.environ.get("SORA_SUBJECT_ID"))
    parser.add_argument("--agent-id", default=os.environ.get("SORA_AGENT_ID", "sora"))
    parser.add_argument(
        "--client-instance-id", default=os.environ.get("SORA_CLIENT_INSTANCE_ID")
    )
    parser.add_argument("--origin", action="append", default=[])
    parser.add_argument("--allow-missing-origin", action="store_true")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "deepseek-v4-flash"))
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--no-legacy-migration", action="store_true", help="Require explicit model setup for managed desktop spaces")
    parser.add_argument("--erase-stopped-profile", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = build_parser().parse_args(argv)
    if args.erase_stopped_profile:
        from .privacy_cli import erase_stopped_profile
        try:
            payload = json.loads(sys.stdin.readline(4097))
            result = erase_stopped_profile(args.profile_id, args.subject_id, args.agent_id, payload)
            print(json.dumps(result), flush=True)
            return 0
        except Exception as error:
            # Exception messages can contain private paths or credential details.
            frame = error.__traceback__
            while frame is not None and frame.tb_next is not None:
                frame = frame.tb_next
            failure = {"status": "erase_unconfirmed", "error_type": type(error).__name__}
            if frame is not None:
                failure["operation"] = frame.tb_frame.f_code.co_name
                failure["source"] = Path(frame.tb_frame.f_code.co_filename).name
                failure["line"] = frame.tb_lineno
            print(json.dumps(failure), flush=True)
            return 3
    missing = [
        name
        for name, value in {
            "profile-id": args.profile_id,
            "subject-id": args.subject_id,
            "client-instance-id": args.client_instance_id,
        }.items()
        if not value
    ]
    if missing:
        print(f"missing required identity options: {', '.join(missing)}", file=sys.stderr)
        return 2
    if not args.origin and not args.allow_missing_origin:
        print("at least one --origin or --allow-missing-origin is required", file=sys.stderr)
        return 2
    identity_material = "\x1f".join(
        (args.profile_id, args.subject_id, args.agent_id)
    )
    identity_digest = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()
    configured_root = os.environ.get("SORA_GATEWAY_STATE_ROOT")
    if configured_root:
        root_state_dir = Path(configured_root).expanduser().resolve()
    elif os.environ.get("SORA_SANDBOX_STATE_DIR"):
        root_state_dir = workspace_state_dir()
    else:
        root_state_dir = user_config_dir() / "runtime"
    profile_state_dir = root_state_dir / "gateway-identities" / identity_digest
    from .profile_lock import acquire_profile_lock
    try:
        runtime_lock = acquire_profile_lock(profile_state_dir)
    except ValueError:
        print("profile is already in use; close its other client first", file=sys.stderr)
        return 4
    from .privacy_cli import require_openable_profile
    try:
        require_openable_profile(profile_state_dir)
    except (ValueError, OSError):
        runtime_lock.close()
        print("profile erasure requires recovery in desktop privacy settings", file=sys.stderr)
        return 5
    os.environ["SORA_SANDBOX_STATE_DIR"] = str(profile_state_dir)

    try:
        llm = make_llm(
            "deepseek",
            args.model,
            True,
            owner=OwnerScope(
                tenant_id=args.profile_id,
                user_id=args.subject_id,
                agent_id=args.agent_id,
            ),
            allow_unconfigured=True,
            migrate_legacy=not args.no_legacy_migration,
        )
    except CredentialError as exc:
        runtime_lock.close()
        print(str(exc), file=sys.stderr)
        return 2

    stack = SoraRuntimeStack.build(
        llm,
        owner_user_id=args.subject_id,
        agent_id=args.agent_id,
        tenant_id=args.profile_id,
    )
    state_dir = workspace_state_dir()
    registry = GatewayRegistry(state_dir / "conversation_gateway.db")
    capabilities = LocalCapabilityProvider()
    policy = GatewayPolicy()
    service = ConversationGatewayService(
        LocalConversationRuntimeAdapter(stack.conversation),
        registry=registry,
        state_repository=ProfileStateRepository(
            state_dir / "gateway_profiles", profile_scope=args.profile_id
        ),
        policy=policy,
        identity_scope=(args.profile_id, args.subject_id, args.agent_id),
    )
    service.register_quality_ops(stack.memory)
    token, claims = capabilities.issue(
        profile_id=args.profile_id,
        subject_id=args.subject_id,
        agent_id=args.agent_id,
        client_instance_id=args.client_instance_id,
        permissions=ALL_PERMISSIONS,
        allowed_origins=args.origin,
        ttl_ms=8 * 60 * 60 * 1000,
        allow_missing_origin=args.allow_missing_origin,
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args.host, args.port))
    listener.listen(128)
    actual_port = int(listener.getsockname()[1])
    server_holder: dict[str, uvicorn.Server] = {}
    app = create_gateway_app(
        service,
        capabilities,
        shutdown_callback=lambda: setattr(server_holder["server"], "should_exit", True),
        model_provider_admin=(
            ModelProviderAdminService(stack.model_provider_system)
            if stack.model_provider_system is not None
            else None
        ),
        memory_runtime=stack.memory,
        interruptibility_service=stack.interruptibility,
        runtime_stack=stack,
    )
    config = uvicorn.Config(
        app,
        host=args.host,
        port=actual_port,
        log_level=args.log_level,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        ws_max_size=policy.max_frame_bytes,
        ws_ping_interval=policy.heartbeat_interval_ms / 1000,
        timeout_keep_alive=5,
    )
    server = uvicorn.Server(config)
    server_holder["server"] = server
    print(
        json.dumps(
            {
                "schema_version": "ConversationGatewayBootstrap.v1",
                "base_url": f"http://{args.host}:{actual_port}",
                "capability_token": token,
                "capability_id": claims.capability_id,
                "protocol_version": "1.0",
                "expires_at_ms": claims.expires_at_ms,
                "client_instance_id": claims.client_instance_id,
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    try:
        try:
            server.run(sockets=[listener])
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        listener.close()
        try:
            registry.close()
        finally:
            stack.close()
            runtime_lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
