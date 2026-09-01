from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.agent import AgentRunner, build_default_subsystem_registry
from sora_sandbox.conversation_gateway.contracts import (
    GatewayErrorCode,
    TurnSubmit,
)
from sora_sandbox.conversation_gateway.client import (
    ConversationGatewayClient,
    GatewayClientConfig,
)
from sora_sandbox.conversation_gateway.eval import (
    GatewayEvalCase,
    GatewayReleaseGate,
    run_gateway_cases,
    validate_gateway_golden_set,
)
from sora_sandbox.conversation_gateway.host import (
    _enqueue_with_timeout,
    _require_websocket_host,
    create_gateway_app,
)
from sora_sandbox.conversation_gateway.observability import GatewayMetrics
from sora_sandbox.conversation_gateway.registry import GatewayRegistry
from sora_sandbox.conversation_gateway.runtime_port import LocalConversationRuntimeAdapter
from sora_sandbox.conversation_gateway.security import LocalCapabilityProvider
from sora_sandbox.conversation_gateway.service import (
    ConversationGatewayService,
    GatewayPolicy,
    GatewayServiceError,
)
from sora_sandbox.conversation_gateway.state import ProfileStateRepository
from sora_sandbox.conversation_runtime import ConversationRuntime, ConversationRuntimeConfig
from sora_sandbox.conversation_runtime.contracts import (
    ClientEvent,
    TurnExecutionContext,
    TurnKind,
    TurnStatus,
)
from sora_sandbox.conversation_runtime.journal import ConversationJournal
from sora_sandbox.conversation_runtime.post_turn import PostTurnDispatcher
from sora_sandbox.conversation_runtime.routing import ModelRouteCoordinator, RoutedLLMClient
from sora_sandbox.conversation_runtime.security import AtRestCipher, StaticKeyProvider
from sora_sandbox.interruptibility import InterruptibilityService
from sora_sandbox.llm import FakeLLMClient
from sora_sandbox.model_provider import (
    EndpointPolicy,
    InMemorySecretStore,
    ModelProviderSystem,
    OwnerScope,
    ProviderRegistry,
)
from sora_sandbox.model_provider.admin import ModelProviderAdminService
from sora_sandbox.runtime_stack import SoraRuntimeStack


PERMISSIONS = {
    "stream.connect",
    "turn.submit",
    "turn.read",
    "turn.cancel",
    "history.read",
    "gateway.shutdown",
    "model_provider.read",
    "model_provider.write",
    "model_provider.verify",
    "model_provider.bind",
    "model_provider.delete",
}


class ConversationGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        journal = ConversationJournal(
            self.root / "runtime.db",
            AtRestCipher(StaticKeyProvider(b"g" * 32)),
        )
        routes = ModelRouteCoordinator(max_hot_path_calls=3)
        routed = RoutedLLMClient(FakeLLMClient(), routes, journal=journal)
        runner = AgentRunner(
            routed,
            subsystem_registry=build_default_subsystem_registry(persistent_executor=True),
            interruptibility=InterruptibilityService(),
        )
        self.runtime = ConversationRuntime(
            runner,
            journal=journal,
            model_routes=routes,
            post_turn=PostTurnDispatcher(journal),
            config=ConversationRuntimeConfig(start_post_turn_worker=False),
        )
        self.registry = GatewayRegistry(self.root / "gateway.db")
        self.capabilities = LocalCapabilityProvider()
        self.service = ConversationGatewayService(
            LocalConversationRuntimeAdapter(self.runtime),
            registry=self.registry,
            state_repository=ProfileStateRepository(
                self.root / "profiles", profile_scope="profile-a"
            ),
            policy=GatewayPolicy(
                event_poll_interval_ms=5,
                heartbeat_interval_ms=1_000,
                shutdown_grace_ms=2_000,
                read_requests_per_minute=20,
                stream_connections_per_minute=10,
            ),
            identity_scope=("profile-a", "user-a", "sora"),
        )
        self.model_provider_system = ModelProviderSystem(
            ProviderRegistry(self.root / "providers.db"),
            InMemorySecretStore(),
            OwnerScope(tenant_id="profile-a", user_id="user-a", agent_id="sora"),
            endpoint_policy=EndpointPolicy(
                resolver=lambda _host, _port: ("93.184.216.34",)
            ),
        )
        self.token, _ = self.capabilities.issue(
            profile_id="profile-a",
            subject_id="user-a",
            agent_id="sora",
            client_instance_id="desktop-a",
            permissions=PERMISSIONS,
            allowed_origins={"app://sora"},
            allow_missing_origin=True,
        )
        self.foreign_token, _ = self.capabilities.issue(
            profile_id="profile-b",
            subject_id="user-b",
            agent_id="sora",
            client_instance_id="desktop-b",
            permissions=PERMISSIONS,
            allowed_origins={"app://sora"},
            allow_missing_origin=True,
        )
        app = create_gateway_app(
            self.service,
            self.capabilities,
            model_provider_admin=ModelProviderAdminService(self.model_provider_system),
        )
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.registry.close()
        self.runtime.close()
        self.model_provider_system.close()
        self.temp.cleanup()

    def test_model_provider_management_is_owner_scoped_and_secret_redacted(self) -> None:
        create = self.client.post(
            "/v1/model-providers",
            headers=self.headers(),
            json={
                "display_name": "User DeepSeek",
                "provider_kind": "deepseek",
                "protocol": "openai_chat_completions",
                "base_url": "https://api.deepseek.com",
                "custom_endpoint": False,
                "auth_scheme": "bearer",
            },
        )
        self.assertEqual(create.status_code, 201, create.text)
        profile_id = create.json()["profile_id"]
        credential = self.client.put(
            f"/v1/model-providers/{profile_id}/credential",
            headers=self.headers(),
            json={"api_key": "sk-owned-by-user", "headers": {}},
        )
        self.assertEqual(credential.status_code, 200, credential.text)
        self.assertEqual(credential.json()["credential_ref"], "bound")
        self.assertNotIn("sk-owned-by-user", credential.text)

        listing = self.client.get("/v1/model-providers", headers=self.headers())
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertNotIn("sk-owned-by-user", listing.text)
        self.assertFalse(listing.json()["secrets_included"])

        health = self.client.get(
            "/v1/model-providers/health", headers=self.headers()
        )
        self.assertEqual(health.status_code, 200, health.text)
        self.assertEqual(health.json()["status"], "ok")
        self.assertEqual(health.json()["active_credential_failures"], [])

        maintained = self.client.post(
            "/v1/model-providers/maintenance", headers=self.headers()
        )
        self.assertEqual(maintained.status_code, 200, maintained.text)
        self.assertIn("provider_invocation_trace", maintained.json()["deleted"])

        foreign = self.client.get(
            "/v1/model-providers", headers=self.headers(self.foreign_token)
        )
        self.assertEqual(foreign.status_code, 403)

        deleted = self.client.request(
            "DELETE",
            f"/v1/model-providers/{profile_id}",
            headers=self.headers(),
            json={"confirmation": f"delete:{profile_id}"},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["status"], "deleted")

    def headers(self, token: str | None = None, *, origin: str | None = None):
        selected_token = token or self.token
        result = {
            "Authorization": f"Bearer {selected_token}",
            "X-Sora-Protocol-Version": "1.0",
            "X-Sora-Client-Instance": (
                "desktop-b" if selected_token == self.foreign_token else "desktop-a"
            ),
        }
        if origin:
            result["Origin"] = origin
        return result

    @staticmethod
    def payload(
        *,
        request_id: str = "request-001",
        conversation_id: str = "conversation-001",
        text: str = "hello",
        client_instance_id: str = "desktop-a",
    ) -> dict[str, object]:
        return TurnSubmit(
            request_id=request_id,
            conversation_id=conversation_id,
            client_instance_id=client_instance_id,
            text=text,
        ).model_dump(mode="json")

    def test_health_authentication_and_security_headers(self) -> None:
        live = self.client.get("/health/live")
        ready = self.client.get("/health/ready")
        denied = self.client.post("/v1/turns", json=self.payload())
        self.assertEqual(live.status_code, 200)
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.json()["code"], "AUTH_REQUIRED")
        self.assertEqual(denied.headers["cache-control"], "no-store")
        self.assertEqual(denied.headers["x-content-type-options"], "nosniff")
        self.assertEqual(denied.headers["x-sora-protocol-version"], "1.0")
        self.assertEqual(self.client.get("/docs").status_code, 404)

    def test_submit_stream_status_history_and_idempotent_replay(self) -> None:
        payload = self.payload()
        payload["correlation_id"] = "trace-correlation-001"
        first = self.client.post(
            "/v1/turns", headers=self.headers(), json=payload
        )
        self.assertEqual(first.status_code, 202, first.text)
        accepted = first.json()
        events_response = self.client.get(
            accepted["events_url"], headers=self.headers()
        )
        self.assertEqual(events_response.status_code, 200)
        events = self._sse_events(events_response.text)
        self.assertEqual(events[0]["event_type"], "turn.accepted")
        self.assertEqual(events[-1]["event_type"], "turn.committed")
        self.assertEqual(events[-1]["payload"]["conversation_version"], 1)
        self.assertEqual(sum(e["event_type"] == "turn.committed" for e in events), 1)
        self.assertEqual(
            [event["sequence"] for event in events],
            sorted({event["sequence"] for event in events}),
        )

        status = self.client.get(
            f"/v1/turns/{accepted['turn_id']}", headers=self.headers()
        )
        self.assertEqual(status.json()["product_status"], "committed")
        self.assertEqual(status.json()["correlation_id"], "trace-correlation-001")
        self.assertEqual(
            self.runtime.turn_status(accepted["turn_id"]).correlation_id,
            "trace-correlation-001",
        )
        history = self.client.get(
            "/v1/conversations/conversation-001/history", headers=self.headers()
        ).json()
        self.assertEqual([item["role"] for item in history["items"]], ["user", "sora"])
        self.assertEqual(history["conversation_version"], 1)
        profile_state = self.service.state_repository.load("profile-a")
        self.assertEqual(profile_state["session"]["user_id"], "user-a")
        self.assertEqual(profile_state["session"]["sora_id"], "sora")
        self.assertTrue(profile_state["session"]["pair_id"].startswith("pair_"))

        replay = self.client.post(
            "/v1/turns", headers=self.headers(), json=payload
        )
        self.assertEqual(replay.status_code, 202)
        self.assertTrue(replay.json()["idempotent_replay"])
        self.assertEqual(replay.json()["turn_id"], accepted["turn_id"])
        second_history = self.client.get(
            "/v1/conversations/conversation-001/history", headers=self.headers()
        ).json()
        self.assertEqual(len(second_history["items"]), 2)

    def test_first_run_history_is_an_empty_page(self) -> None:
        response = self.client.get(
            "/v1/conversations/new-conversation/history",
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "schema_version": "HistoryPage.v1",
                "protocol_version": "1.0",
                "conversation_id": "new-conversation",
                "conversation_version": 0,
                "items": [],
                "next_before_sequence": None,
            },
        )

    def test_idempotency_conflict_and_optimistic_version_conflict(self) -> None:
        accepted = self.client.post(
            "/v1/turns", headers=self.headers(), json=self.payload()
        ).json()
        self.client.get(accepted["events_url"], headers=self.headers())
        conflict = self.client.post(
            "/v1/turns",
            headers=self.headers(),
            json=self.payload(text="different text"),
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "IDEMPOTENCY_CONFLICT")
        versioned = self.payload(request_id="request-002")
        versioned["expected_conversation_version"] = 0
        version_conflict = self.client.post(
            "/v1/turns", headers=self.headers(), json=versioned
        )
        self.assertEqual(version_conflict.status_code, 409)
        self.assertEqual(version_conflict.json()["code"], "VERSION_CONFLICT")
        self.assertEqual(
            version_conflict.json()["details"]["current_conversation_version"], 1
        )

    def test_foreign_process_identity_is_rejected_without_resource_disclosure(self) -> None:
        accepted = self.client.post(
            "/v1/turns", headers=self.headers(), json=self.payload()
        ).json()
        foreign_headers = self.headers(self.foreign_token)
        turn = self.client.get(
            f"/v1/turns/{accepted['turn_id']}", headers=foreign_headers
        )
        events = self.client.get(
            f"/v1/turns/{accepted['turn_id']}/events", headers=foreign_headers
        )
        history = self.client.get(
            "/v1/conversations/conversation-001/history", headers=foreign_headers
        )
        self.assertEqual(turn.status_code, 403)
        self.assertEqual(events.status_code, 403)
        self.assertEqual(history.status_code, 403)
        self.assertNotIn("profile-a", turn.text)

    def test_origin_protocol_payload_and_strict_schema_are_enforced(self) -> None:
        bad_origin = self.client.post(
            "/v1/turns",
            headers=self.headers(origin="https://attacker.invalid"),
            json=self.payload(),
        )
        self.assertEqual(bad_origin.status_code, 403)
        bad_protocol_headers = self.headers()
        bad_protocol_headers["X-Sora-Protocol-Version"] = "2.0"
        bad_protocol = self.client.post(
            "/v1/turns", headers=bad_protocol_headers, json=self.payload()
        )
        self.assertEqual(bad_protocol.status_code, 426)
        self.assertEqual(bad_protocol.json()["code"], "PROTOCOL_UNSUPPORTED")
        extra = self.payload(request_id="request-extra")
        extra["unexpected"] = True
        strict = self.client.post("/v1/turns", headers=self.headers(), json=extra)
        self.assertEqual(strict.status_code, 422)
        self.assertEqual(strict.json()["code"], "INVALID_REQUEST")
        oversized = self.client.post(
            "/v1/turns",
            headers={**self.headers(), "Content-Length": "9999999"},
            content=b"{}",
        )
        self.assertEqual(oversized.status_code, 413)

    def test_sse_resume_is_strictly_after_cursor(self) -> None:
        accepted = self.client.post(
            "/v1/turns", headers=self.headers(), json=self.payload()
        ).json()
        all_events = self._sse_events(
            self.client.get(accepted["events_url"], headers=self.headers()).text
        )
        cursor = all_events[len(all_events) // 2]["sequence"]
        resumed = self._sse_events(
            self.client.get(
                accepted["events_url"],
                headers={**self.headers(), "Last-Event-ID": str(cursor)},
            ).text
        )
        self.assertTrue(resumed)
        self.assertTrue(all(event["sequence"] > cursor for event in resumed))
        self.assertEqual(resumed[-1]["event_type"], "turn.committed")

    def test_terminal_event_is_repaired_exactly_once(self) -> None:
        accepted = self.client.post(
            "/v1/turns", headers=self.headers(), json=self.payload()
        ).json()
        self._sse_events(
            self.client.get(accepted["events_url"], headers=self.headers()).text
        )
        with self.runtime.journal.transaction() as connection:
            connection.execute(
                "DELETE FROM runtime_client_event WHERE turn_id=? AND event_type='response.completed'",
                (accepted["turn_id"],),
            )
        repaired = self._sse_events(
            self.client.get(accepted["events_url"], headers=self.headers()).text
        )
        self.assertEqual(sum(e["event_type"] == "turn.committed" for e in repaired), 1)
        again = self._sse_events(
            self.client.get(accepted["events_url"], headers=self.headers()).text
        )
        self.assertEqual(sum(e["event_type"] == "turn.committed" for e in again), 1)

    def test_websocket_handshake_submit_and_resume(self) -> None:
        headers = {
            **self.headers(),
            "X-Sora-Client-Instance": "desktop-a",
        }
        with self.client.websocket_connect(
            "/v1/stream", headers=headers, subprotocols=["sora.v1.0"]
        ) as websocket:
            ready = websocket.receive_json()
            self.assertEqual(ready["type"], "connection.ready")
            websocket.send_json({"type": "ping", "nonce": "n1"})
            self.assertEqual(websocket.receive_json(), {"type": "pong", "nonce": "n1"})
            websocket.send_json(
                {"type": "turn.submit", "payload": self.payload(request_id="ws-request")}
            )
            accepted = websocket.receive_json()
            self.assertEqual(accepted["type"], "turn.accepted")
            terminal = None
            for _ in range(20):
                message = websocket.receive_json()
                if (
                    message.get("type") == "event"
                    and message["payload"]["event_type"] == "turn.committed"
                ):
                    terminal = message
                    break
            self.assertIsNotNone(terminal)
            websocket.send_json(
                {
                    "type": "event.ack",
                    "turn_id": accepted["payload"]["turn_id"],
                    "sequence": terminal["payload"]["sequence"],
                }
            )

    def test_client_sdk_runs_turn_over_the_real_http_contract(self) -> None:
        bridge = _TestClientTransport(self.client)
        sdk = ConversationGatewayClient(
            GatewayClientConfig(
                base_url="http://testserver",
                capability_token=self.token,
                client_instance_id="desktop-a",
            ),
            transport=bridge,
        )
        try:
            accepted, output = sdk.run_turn(
                TurnSubmit(
                    request_id="sdk-request",
                    conversation_id="sdk-conversation",
                    client_instance_id="desktop-a",
                    text="hello from sdk",
                )
            )
            self.assertTrue(output)
            self.assertEqual(sdk.status(accepted.turn_id).product_status, "committed")
            self.assertEqual(
                [item.role for item in sdk.history("sdk-conversation").items],
                ["user", "sora"],
            )
        finally:
            sdk.close()

    def test_inflight_cancellation_does_not_commit_partial_history(self) -> None:
        class SlowLLM:
            def chat(self, system, user, **kwargs):
                return {"content": "{}", "usage": {}, "model": "slow"}

            def stream_chat(self, system, user, **kwargs):
                token = kwargs["cancellation_token"]
                for _ in range(200):
                    token.raise_if_cancelled()
                    time.sleep(0.005)
                    yield {"delta": "x", "usage": {}, "model": "slow"}

        slow_root = self.root / "slow"
        slow_root.mkdir()
        journal = ConversationJournal(
            slow_root / "runtime.db",
            AtRestCipher(StaticKeyProvider(b"s" * 32)),
        )
        routes = ModelRouteCoordinator(max_hot_path_calls=3)
        runner = AgentRunner(
            RoutedLLMClient(SlowLLM(), routes, journal=journal),
            subsystem_registry=build_default_subsystem_registry(persistent_executor=True),
            interruptibility=InterruptibilityService(),
        )
        runtime = ConversationRuntime(
            runner,
            journal=journal,
            model_routes=routes,
            post_turn=PostTurnDispatcher(journal),
            config=ConversationRuntimeConfig(start_post_turn_worker=False),
        )
        registry = GatewayRegistry(slow_root / "gateway.db")
        service = ConversationGatewayService(
            LocalConversationRuntimeAdapter(runtime),
            registry=registry,
            state_repository=ProfileStateRepository(
                slow_root / "profiles", profile_scope="slow-profile"
            ),
            policy=GatewayPolicy(event_poll_interval_ms=5, shutdown_grace_ms=2_000),
            identity_scope=("slow-profile", "slow-user", "sora"),
        )
        provider = LocalCapabilityProvider()
        raw_token, _ = provider.issue(
            profile_id="slow-profile",
            subject_id="slow-user",
            agent_id="sora",
            client_instance_id="slow-desktop",
            permissions=PERMISSIONS,
            allowed_origins=set(),
            allow_missing_origin=True,
        )
        identity = provider.verify(
            raw_token, permission="turn.submit", origin=None
        )
        service.start()
        try:
            accepted = service.submit(
                identity,
                TurnSubmit(
                    request_id="cancel-request",
                    conversation_id="cancel-conversation",
                    client_instance_id="slow-desktop",
                    text="please write a long response",
                ),
            )
            service.cancel(identity, accepted.turn_id, reason="user_cancelled")
            events = list(service.iter_events(identity, accepted.turn_id))
            self.assertEqual(events[-1].event_type, "turn.cancelled")
            self.assertEqual(
                service.status(identity, accepted.turn_id).product_status,
                "cancelled",
            )
            self.assertEqual(
                runtime.history_page(
                    GatewayRegistry.runtime_conversation_id(
                        profile_id="slow-profile",
                        subject_id="slow-user",
                        agent_id="sora",
                        external_conversation_id="cancel-conversation",
                    )
                ),
                [],
            )
        finally:
            service.close()
            registry.close()
            runtime.close()

    def test_registry_replay_survives_gateway_service_restart(self) -> None:
        payload = self.payload(request_id="restart-request")
        accepted = self.client.post(
            "/v1/turns", headers=self.headers(), json=payload
        ).json()
        self.client.get(accepted["events_url"], headers=self.headers())
        second_registry = GatewayRegistry(self.registry.path)
        second_service = ConversationGatewayService(
            LocalConversationRuntimeAdapter(self.runtime),
            registry=second_registry,
            state_repository=ProfileStateRepository(
                self.root / "profiles", profile_scope="profile-a"
            ),
            policy=GatewayPolicy(shutdown_grace_ms=1_000),
            identity_scope=("profile-a", "user-a", "sora"),
        )
        identity = self.capabilities.verify(
            self.token, permission="turn.submit", origin=None
        )
        second_service.start()
        try:
            replay = second_service.submit(identity, TurnSubmit.model_validate(payload))
            self.assertTrue(replay.idempotent_replay)
            self.assertEqual(replay.turn_id, accepted["turn_id"])
        finally:
            second_service.close()
            second_registry.close()

    def test_drain_rejects_new_work_but_readiness_remains_introspectable(self) -> None:
        self.service.begin_drain()
        response = self.client.post(
            "/v1/turns", headers=self.headers(), json=self.payload()
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "DRAINING")
        self.assertEqual(self.client.get("/health/ready").status_code, 503)

    def test_process_identity_scope_rejects_foreign_capability(self) -> None:
        scoped = ConversationGatewayService(
            LocalConversationRuntimeAdapter(self.runtime),
            registry=self.registry,
            state_repository=self.service.state_repository,
            identity_scope=("profile-a", "user-a", "sora"),
        )
        foreign = self.capabilities.verify(
            self.foreign_token, permission="turn.submit", origin=None
        )
        scoped.start()
        try:
            with self.assertRaisesRegex(Exception, "outside this gateway"):
                scoped.submit(
                    foreign,
                    TurnSubmit(
                        request_id="foreign-request",
                        conversation_id="foreign-conversation",
                        client_instance_id="desktop-b",
                        text="hello",
                    ),
                )
        finally:
            scoped.close()

    def test_registry_contains_no_raw_message_content(self) -> None:
        sensitive = "private sentence that must not enter gateway registry"
        self.client.post(
            "/v1/turns",
            headers=self.headers(),
            json=self.payload(text=sensitive),
        )
        connection = sqlite3.connect(self.registry.path)
        try:
            dump = "\n".join(connection.iterdump())
        finally:
            connection.close()
        self.assertNotIn(sensitive, dump)

    def test_composition_root_propagates_canonical_identity(self) -> None:
        previous = os.environ.get("SORA_SANDBOX_STATE_DIR")
        stack_root = self.root / "identity-stack"
        os.environ["SORA_SANDBOX_STATE_DIR"] = str(stack_root)
        stack = None
        try:
            stack = SoraRuntimeStack.build(
                FakeLLMClient(),
                start_workers=False,
                owner_user_id="canonical-user",
                agent_id="canonical-sora",
                tenant_id="canonical-profile",
            )
            self.assertEqual(stack.memory.runtime.owner_user_id, "canonical-user")
            self.assertEqual(stack.memory.runtime.agent_id, "canonical-sora")
            self.assertEqual(stack.persona.store.agent_id, "canonical-sora")
            self.assertEqual(stack.affect.store.sora_id, "canonical-sora")
            self.assertEqual(stack.relationship.store.sora_id, "canonical-sora")
            self.assertEqual(stack.worldops.store.identity.user_id, "canonical-user")
            self.assertEqual(stack.worldops.store.identity.sora_id, "canonical-sora")
        finally:
            if stack is not None:
                stack.close()
            if previous is None:
                os.environ.pop("SORA_SANDBOX_STATE_DIR", None)
            else:
                os.environ["SORA_SANDBOX_STATE_DIR"] = previous

    def test_concurrent_idempotent_submit_is_single_flight(self) -> None:
        counting = _CountingRuntimePort(self.service.runtime)
        self.service.runtime = counting
        identity = self.capabilities.verify(
            self.token, permission="turn.submit", origin=None
        )
        request = TurnSubmit.model_validate(
            self.payload(request_id="concurrent-idempotency")
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(self.service.submit, identity, request)
                for _ in range(2)
            ]
            accepted = [future.result(timeout=5) for future in futures]
        self.assertEqual(counting.stream_calls, 1)
        self.assertEqual(accepted[0].turn_id, accepted[1].turn_id)
        self.assertEqual(
            sorted(item.idempotent_replay for item in accepted), [False, True]
        )
        list(self.service.iter_events(identity, accepted[0].turn_id))
        deadline = time.monotonic() + 1
        while (
            self.service.readiness()["active_turns"]
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        self.assertEqual(self.service.readiness()["active_turns"], 0)

    def test_path_ids_websocket_host_and_state_scope_are_enforced(self) -> None:
        oversized_id = "x" * 129
        invalid_turn = self.client.get(
            f"/v1/turns/{oversized_id}", headers=self.headers()
        )
        invalid_conversation = self.client.get(
            f"/v1/conversations/{oversized_id}/history", headers=self.headers()
        )
        self.assertEqual(invalid_turn.status_code, 422)
        self.assertEqual(invalid_turn.json()["code"], "INVALID_REQUEST")
        self.assertEqual(invalid_conversation.status_code, 422)

        class SocketHeaders:
            headers = {"host": "attacker.invalid"}

        with self.assertRaises(GatewayServiceError) as denied:
            _require_websocket_host(SocketHeaders(), ("localhost", "testserver"))
        self.assertEqual(denied.exception.status_code, 403)
        with self.assertRaises(PermissionError):
            self.service.state_repository.load("profile-b")

    def test_read_and_stream_connection_rate_limits_are_independent(self) -> None:
        accepted = self.client.post(
            "/v1/turns", headers=self.headers(), json=self.payload()
        ).json()
        responses = [
            self.client.get(f"/v1/turns/{accepted['turn_id']}", headers=self.headers())
            for _ in range(21)
        ]
        self.assertTrue(all(response.status_code == 200 for response in responses[:20]))
        self.assertEqual(responses[-1].status_code, 429)
        self.assertEqual(responses[-1].json()["code"], "RATE_LIMITED")

        identity = self.capabilities.verify(
            self.token, permission="turn.read", origin=None
        )
        for _ in range(10):
            self.service.authorize_turn(identity, accepted["turn_id"])
        with self.assertRaises(GatewayServiceError) as limited:
            self.service.authorize_turn(identity, accepted["turn_id"])
        self.assertEqual(limited.exception.code, GatewayErrorCode.RATE_LIMITED)
        exported = self.service.metrics.export()
        rate_limited = [
            row
            for row in exported["counters"]
            if row["name"] == "gateway_rate_limited_total"
        ]
        self.assertEqual(sum(int(row["value"]) for row in rate_limited), 2)

    def test_stream_lifetime_is_bounded_and_cancels_hanging_runtime(self) -> None:
        root = self.root / "timeout"
        runtime = _HangingRuntimePort()
        registry = GatewayRegistry(root / "gateway.db")
        service = ConversationGatewayService(
            runtime,
            registry=registry,
            state_repository=ProfileStateRepository(
                root / "profiles", profile_scope="timeout-profile"
            ),
            policy=GatewayPolicy(
                event_poll_interval_ms=1,
                heartbeat_interval_ms=1_000,
                turn_stream_timeout_ms=1,
                turn_timeout_cancel_grace_ms=5,
                shutdown_grace_ms=100,
            ),
            identity_scope=("timeout-profile", "timeout-user", "sora"),
        )
        provider = LocalCapabilityProvider()
        token, _ = provider.issue(
            profile_id="timeout-profile",
            subject_id="timeout-user",
            agent_id="sora",
            client_instance_id="timeout-desktop",
            permissions=PERMISSIONS,
            allowed_origins=set(),
            allow_missing_origin=True,
        )
        identity = provider.verify(token, permission="turn.submit", origin=None)
        service.start()
        try:
            accepted = service.submit(
                identity,
                TurnSubmit(
                    request_id="timeout-request",
                    conversation_id="timeout-conversation",
                    client_instance_id="timeout-desktop",
                    text="stay pending",
                ),
            )
            iterator = service.iter_events(identity, accepted.turn_id)
            self.assertEqual(next(iterator).event_type, "turn.progress")
            with self.assertRaises(GatewayServiceError) as timed_out:
                list(iterator)
            self.assertEqual(timed_out.exception.code, GatewayErrorCode.TURN_TIMEOUT)
            self.assertGreaterEqual(runtime.cancel_calls, 1)
        finally:
            service.close()
            registry.close()

    def test_metrics_bound_label_cardinality_and_interpolate_percentiles(self) -> None:
        metrics = GatewayMetrics(max_distinct_label_values=2)
        for label in ("a", "b", "c", "d"):
            metrics.increment("gateway_test_total", labels={"status": label})
        metrics.increment(
            "gateway_test_total",
            labels={"status": "x" * 1000, "profile_id": "must-not-export"},
        )
        for value in (0.0, 10.0):
            metrics.observe("gateway_test_ms", value)
        exported = metrics.export()
        labels = {
            row["labels"].get("status")
            for row in exported["counters"]
            if row["labels"]
        }
        self.assertEqual(labels, {"a", "b", "__overflow__"})
        self.assertNotIn("must-not-export", json.dumps(exported))
        distribution = exported["distributions"][0]
        self.assertEqual(distribution["p50"], 5.0)

    def test_client_message_id_is_persisted_without_message_content(self) -> None:
        payload = self.payload(request_id="client-message-request")
        payload["client_message_id"] = "client-message-001"
        self.client.post("/v1/turns", headers=self.headers(), json=payload).raise_for_status()
        binding = self.registry.turn_for_request(
            profile_id="profile-a",
            subject_id="user-a",
            agent_id="sora",
            external_conversation_id="conversation-001",
            request_id="client-message-request",
        )
        self.assertIsNotNone(binding)
        self.assertEqual(binding.client_message_id, "client-message-001")

    def test_backpressure_primitive_is_bounded(self) -> None:
        async def exercise() -> None:
            outgoing: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=1)
            await _enqueue_with_timeout(outgoing, {"type": "first"}, timeout_ms=10)
            with self.assertRaises(TimeoutError):
                await _enqueue_with_timeout(
                    outgoing, {"type": "second"}, timeout_ms=10
                )

        asyncio.run(exercise())

    def test_golden_set_and_signed_release_gate(self) -> None:
        dataset = (
            ROOT
            / "src"
            / "sora_sandbox"
            / "conversation_gateway"
            / "eval_data"
            / "golden_v1.jsonl"
        )
        validated = validate_gateway_golden_set(dataset)
        self.assertEqual(validated["status"], "passed")
        self.assertGreaterEqual(validated["total_cases"], 15)
        executed = run_gateway_cases(dataset, self._execute_gateway_golden_case)
        self.assertEqual(executed["status"], "passed", executed)
        self.assertEqual(executed["passed_cases"], validated["total_cases"])
        gate = GatewayReleaseGate(b"gateway-release-key" * 2)
        metrics = {
            "e2e_success_rate": 1.0,
            "authorization_isolation_rate": 1.0,
            "idempotency_rate": 1.0,
            "event_resume_integrity_rate": 1.0,
            "terminal_delivery_rate": 1.0,
            "history_consistency_rate": 1.0,
            "p95_submit_ack_ms": 100.0,
        }
        decision = gate.evaluate(
            metrics, dataset_version="golden_v1", policy_profile="local_default"
        )
        self.assertEqual(decision["status"], "passed")
        self.assertTrue(gate.verify(decision))
        decision["status"] = "blocked"
        self.assertFalse(gate.verify(decision))

    def _execute_gateway_golden_case(
        self, case: GatewayEvalCase
    ) -> dict[str, object]:
        operation = case.operation
        suffix = case.case_id.replace("_", "-")
        if operation == "submit_without_token":
            response = self.client.post("/v1/turns", json=self.payload(request_id=suffix))
            return {
                "status_code": response.status_code,
                "error_code": response.json().get("code"),
            }
        if operation == "submit_revoked_token":
            token, claims = self.capabilities.issue(
                profile_id="profile-a",
                subject_id="user-a",
                agent_id="sora",
                client_instance_id="desktop-a",
                permissions=PERMISSIONS,
                allowed_origins=set(),
                allow_missing_origin=True,
            )
            self.capabilities.revoke(claims.capability_id)
            response = self.client.post(
                "/v1/turns",
                headers=self.headers(token),
                json=self.payload(request_id=suffix),
            )
            return {
                "status_code": response.status_code,
                "error_code": response.json().get("code"),
            }
        if operation == "read_foreign_turn":
            accepted = self.client.post(
                "/v1/turns",
                headers=self.headers(),
                json=self.payload(
                    request_id=suffix, conversation_id=f"conversation-{suffix}"
                ),
            ).json()
            self.client.get(accepted["events_url"], headers=self.headers())
            response = self.client.get(
                f"/v1/turns/{accepted['turn_id']}",
                headers=self.headers(self.foreign_token),
            )
            return {
                "status_code": response.status_code,
                "content_leaked": accepted["turn_id"] in response.text,
            }
        if operation == "wrong_origin":
            response = self.client.post(
                "/v1/turns",
                headers=self.headers(origin="https://attacker.invalid"),
                json=self.payload(request_id=suffix),
            )
            return {
                "status_code": response.status_code,
                "error_code": response.json().get("code"),
            }
        if operation == "submit_protocol_2":
            headers = self.headers()
            headers["X-Sora-Protocol-Version"] = "2.0"
            response = self.client.post(
                "/v1/turns", headers=headers, json=self.payload(request_id=suffix)
            )
            return {
                "status_code": response.status_code,
                "error_code": response.json().get("code"),
            }
        if operation == "submit_extra_field":
            payload = self.payload(request_id=suffix)
            payload["unexpected"] = True
            response = self.client.post(
                "/v1/turns", headers=self.headers(), json=payload
            )
            return {
                "status_code": response.status_code,
                "error_code": response.json().get("code"),
            }
        if operation == "duplicate_submit":
            conversation_id = f"conversation-{suffix}"
            payload = self.payload(request_id=suffix, conversation_id=conversation_id)
            first = self.client.post(
                "/v1/turns", headers=self.headers(), json=payload
            ).json()
            self.client.get(first["events_url"], headers=self.headers())
            second = self.client.post(
                "/v1/turns", headers=self.headers(), json=payload
            ).json()
            history = self.client.get(
                f"/v1/conversations/{conversation_id}/history",
                headers=self.headers(),
            ).json()
            return {
                "same_turn_id": first["turn_id"] == second["turn_id"],
                "one_history_pair": len(history["items"]) == 2,
            }
        if operation == "duplicate_different_text":
            payload = self.payload(
                request_id=suffix, conversation_id=f"conversation-{suffix}"
            )
            accepted = self.client.post(
                "/v1/turns", headers=self.headers(), json=payload
            ).json()
            self.client.get(accepted["events_url"], headers=self.headers())
            payload["text"] = "different"
            response = self.client.post(
                "/v1/turns", headers=self.headers(), json=payload
            )
            return {
                "status_code": response.status_code,
                "error_code": response.json().get("code"),
            }
        if operation == "resume_after_sequence":
            accepted = self.client.post(
                "/v1/turns",
                headers=self.headers(),
                json=self.payload(
                    request_id=suffix, conversation_id=f"conversation-{suffix}"
                ),
            ).json()
            events = self._sse_events(
                self.client.get(accepted["events_url"], headers=self.headers()).text
            )
            cursor = events[len(events) // 2]["sequence"]
            resumed = self._sse_events(
                self.client.get(
                    accepted["events_url"],
                    headers={**self.headers(), "Last-Event-ID": str(cursor)},
                ).text
            )
            sequences = [int(item["sequence"]) for item in resumed]
            return {
                "strictly_increasing": all(
                    left < right for left, right in zip(sequences, sequences[1:])
                )
                and all(sequence > cursor for sequence in sequences),
                "duplicates": len(sequences) - len(set(sequences)),
            }
        if operation == "repair_missing_terminal":
            accepted = self.client.post(
                "/v1/turns",
                headers=self.headers(),
                json=self.payload(
                    request_id=suffix, conversation_id=f"conversation-{suffix}"
                ),
            ).json()
            self.client.get(accepted["events_url"], headers=self.headers())
            with self.runtime.journal.transaction() as connection:
                connection.execute(
                    "DELETE FROM runtime_client_event "
                    "WHERE turn_id=? AND event_type='response.completed'",
                    (accepted["turn_id"],),
                )
            repaired = self._sse_events(
                self.client.get(accepted["events_url"], headers=self.headers()).text
            )
            terminal = [
                item for item in repaired if item["event_type"] == "turn.committed"
            ]
            status = self.client.get(
                f"/v1/turns/{accepted['turn_id']}", headers=self.headers()
            ).json()
            return {
                "terminal_once": len(terminal) == 1,
                "terminal_matches_status": status["product_status"] == "committed",
            }
        if operation == "cancel_slow_turn":
            return self._run_controlled_runtime_case(cancel=True)
        if operation == "read_history":
            conversation_id = f"conversation-{suffix}"
            accepted = self.client.post(
                "/v1/turns",
                headers=self.headers(),
                json=self.payload(request_id=suffix, conversation_id=conversation_id),
            ).json()
            self.client.get(accepted["events_url"], headers=self.headers())
            history = self.client.get(
                f"/v1/conversations/{conversation_id}/history",
                headers=self.headers(),
            ).json()
            return {
                "roles": [item["role"] for item in history["items"]],
                "conversation_version": history["conversation_version"],
            }
        if operation == "exceed_active_limit":
            return self._run_controlled_runtime_case(cancel=False)
        if operation == "fill_outgoing_queue":
            async def fill() -> bool:
                outgoing: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=1)
                await _enqueue_with_timeout(outgoing, {"type": "first"}, timeout_ms=10)
                try:
                    await _enqueue_with_timeout(
                        outgoing, {"type": "second"}, timeout_ms=10
                    )
                except TimeoutError:
                    return True
                return False

            timed_out = asyncio.run(fill())
            return {
                "close_code": 1013 if timed_out else 1000,
                "reason": "slow_consumer" if timed_out else "completed",
            }
        if operation == "submit_while_draining":
            self.service.begin_drain()
            response = self.client.post(
                "/v1/turns",
                headers=self.headers(),
                json=self.payload(request_id=suffix),
            )
            return {
                "status_code": response.status_code,
                "error_code": response.json().get("code"),
            }
        if operation == "recover_stale_turn":
            context, _ = self.runtime.journal.create_turn(
                conversation_id="golden-recovery-conversation",
                request_id="golden-recovery-request",
                user_input="recover me",
            )
            with self.runtime.journal.transaction() as connection:
                connection.execute(
                    "UPDATE runtime_turn SET updated_at_ms=? WHERE turn_id=?",
                    (int(time.time() * 1000) - 5_000, context.turn_id),
                )
            self.runtime.recover(stale_after_ms=1_000)
            recovered = self.runtime.turn_status(context.turn_id)
            events = self.runtime.reconnect(context.turn_id)
            return {
                "runtime_status": recovered.status.value,
                "terminal_event": (
                    "turn.failed"
                    if events and events[-1].event_type == "turn.failed"
                    else events[-1].event_type if events else None
                ),
            }
        raise AssertionError(f"no gateway golden executor for operation: {operation}")

    def _run_controlled_runtime_case(self, *, cancel: bool) -> dict[str, object]:
        name = "cancel" if cancel else "concurrency"
        root = self.root / f"golden-{name}"
        runtime = _ControlledRuntimePort(block=not cancel)
        registry = GatewayRegistry(root / "gateway.db")
        service = ConversationGatewayService(
            runtime,
            registry=registry,
            state_repository=ProfileStateRepository(
                root / "profiles", profile_scope=f"{name}-profile"
            ),
            policy=GatewayPolicy(
                event_poll_interval_ms=1,
                shutdown_grace_ms=200,
                max_active_turns_per_profile=1,
            ),
            identity_scope=(f"{name}-profile", f"{name}-user", "sora"),
        )
        provider = LocalCapabilityProvider()
        token, _ = provider.issue(
            profile_id=f"{name}-profile",
            subject_id=f"{name}-user",
            agent_id="sora",
            client_instance_id=f"{name}-desktop",
            permissions=PERMISSIONS,
            allowed_origins=set(),
            allow_missing_origin=True,
        )
        identity = provider.verify(token, permission="turn.submit", origin=None)
        service.start()
        try:
            accepted = service.submit(
                identity,
                TurnSubmit(
                    request_id=f"{name}-request-1",
                    conversation_id=f"{name}-conversation",
                    client_instance_id=f"{name}-desktop",
                    text="hold this turn",
                ),
            )
            if cancel:
                status = service.cancel(identity, accepted.turn_id)
                events = [
                    event
                    for event in service.iter_events(identity, accepted.turn_id)
                    if event is not None
                ]
                return {
                    "terminal_status": status.product_status,
                    "partial_committed": bool(
                        service.history(
                            identity, f"{name}-conversation"
                        ).items
                    ),
                }
            try:
                service.submit(
                    identity,
                    TurnSubmit(
                        request_id=f"{name}-request-2",
                        conversation_id=f"{name}-conversation",
                        client_instance_id=f"{name}-desktop",
                        text="second turn",
                    ),
                )
            except GatewayServiceError as exc:
                return {"status_code": exc.status_code, "error_code": exc.code.value}
            raise AssertionError("concurrent turn was not rejected")
        finally:
            runtime.release()
            service.close()
            registry.close()

    @staticmethod
    def _sse_events(body: str) -> list[dict[str, object]]:
        return [
            json.loads(line[6:])
            for line in body.splitlines()
            if line.startswith("data: ")
        ]


class _CountingRuntimePort:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.stream_calls = 0
        self._lock = threading.Lock()

    def stream_turn(self, *args, **kwargs):
        with self._lock:
            self.stream_calls += 1
        time.sleep(0.05)
        yield from self.delegate.stream_turn(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)


class _HangingRuntimePort:
    def __init__(self) -> None:
        self.context: TurnExecutionContext | None = None
        self.event: ClientEvent | None = None
        self.cancel_calls = 0

    def stream_turn(
        self,
        state,
        user_input,
        *,
        request_id,
        conversation_id,
        branch_id,
        after_sequence=0,
        correlation_id=None,
    ):
        created = int(time.time() * 1000) - 1_000
        turn_id = "turn_hanging_runtime"
        self.context = TurnExecutionContext(
            turn_id=turn_id,
            conversation_id=conversation_id,
            request_id=request_id,
            correlation_id=correlation_id or "corr_hanging_runtime",
            branch_id=branch_id,
            turn_kind=TurnKind.REACTIVE,
            status=TurnStatus.PREPARING,
            stage="preparing",
            created_at_ms=created,
            updated_at_ms=created,
        )
        self.event = ClientEvent(
            turn_id=turn_id,
            sequence=1,
            event_type="turn.preparing",
            payload={},
            created_at_ms=created,
        )
        if after_sequence < 1:
            yield self.event

    def reconnect(self, turn_id, *, after_sequence=0):
        if self.context is None or turn_id != self.context.turn_id:
            raise KeyError(turn_id)
        return [self.event] if self.event is not None and after_sequence < 1 else []

    def cancel(self, conversation_id, *, reason):
        self.cancel_calls += 1
        return False

    def turn_status(self, turn_id):
        if self.context is None or turn_id != self.context.turn_id:
            raise KeyError(turn_id)
        return self.context

    def turn_by_request(self, conversation_id, request_id):
        if (
            self.context is not None
            and self.context.conversation_id == conversation_id
            and self.context.request_id == request_id
        ):
            return self.context
        return None

    def history_page(self, conversation_id, *, limit, before_sequence):
        return []

    def conversation_version(self, conversation_id):
        return 0

    def health(self):
        return {"status": "ok"}

    def recover(self, *, stale_after_ms=300_000):
        return {"status": "ok", "recovered": 0}

    def maintain(self, *, at_ms=None):
        return {"status": "ok"}


class _ControlledRuntimePort(_HangingRuntimePort):
    def __init__(self, *, block: bool) -> None:
        super().__init__()
        self.block = block
        self._release = threading.Event()

    def stream_turn(self, *args, **kwargs):
        yield from super().stream_turn(*args, **kwargs)
        if self.block:
            self._release.wait(timeout=2)

    def cancel(self, conversation_id, *, reason):
        self.cancel_calls += 1
        if self.context is None or self.event is None:
            return False
        timestamp = int(time.time() * 1000)
        self.context = replace(
            self.context,
            status=TurnStatus.CANCELLED,
            stage="cancelled",
            updated_at_ms=timestamp,
            revision=self.context.revision + 1,
        )
        self.event = ClientEvent(
            turn_id=self.context.turn_id,
            sequence=2,
            event_type="turn.cancelled",
            payload={"reason": reason},
            created_at_ms=timestamp,
        )
        self._release.set()
        return True

    def reconnect(self, turn_id, *, after_sequence=0):
        if self.context is None or turn_id != self.context.turn_id:
            raise KeyError(turn_id)
        preparing = ClientEvent(
            turn_id=turn_id,
            sequence=1,
            event_type="turn.preparing",
            payload={},
            created_at_ms=self.context.created_at_ms,
        )
        events = [preparing]
        if self.event is not None and self.event.sequence == 2:
            events.append(self.event)
        return [event for event in events if event.sequence > after_sequence]

    def release(self) -> None:
        self._release.set()


class _TestClientTransport(httpx.BaseTransport):
    def __init__(self, client: TestClient) -> None:
        self.client = client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.client.request(
            request.method,
            request.url.raw_path.decode("ascii"),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )


if __name__ == "__main__":
    unittest.main()
