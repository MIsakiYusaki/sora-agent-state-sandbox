from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from sora_sandbox.conversation_gateway.desktop_commands import install_desktop_command_routes
from sora_sandbox.conversation_gateway.host import _authenticate, _require_memory_owner, _require_protocol
from sora_sandbox.conversation_gateway.security import LocalCapabilityProvider
from sora_sandbox.conversation_gateway.service import GatewayServiceError
from sora_sandbox.memory_quality_ops import AdminActionSpec
from sora_sandbox.memory_runtime import MemoryRuntime


class DesktopCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.memory = MemoryRuntime(Path(directory.name) / "memory.db", owner_user_id="user-a", agent_id="sora")
        self.addCleanup(self.memory.close)
        self.capabilities = LocalCapabilityProvider()
        app = FastAPI()

        @app.exception_handler(GatewayServiceError)
        def gateway_error(_request: Request, error: GatewayServiceError):
            return JSONResponse({"code": error.code.value}, status_code=error.status_code)

        install_desktop_command_routes(
            app, capabilities=self.capabilities, stack=SimpleNamespace(memory=SimpleNamespace(runtime=self.memory)),
            service=SimpleNamespace(identity_scope=("profile-a", "user-a", "sora"), readiness=lambda: {"active_turns": 0}),
            authenticate=_authenticate, require_protocol=_require_protocol, require_owner=_require_memory_owner,
        )
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.parent = self.token({"debug.read", "debug.session.open"})
        self.headers, self.session = self.open_session(self.parent)

    def token(self, permissions, *, client="client-a", user="user-a", profile="profile-a"):
        token, _ = self.capabilities.issue(
            profile_id=profile, subject_id=user, agent_id="sora", client_instance_id=client,
            permissions=permissions, allowed_origins={"sora-desktop://host"},
        )
        return {"Authorization": "Bearer " + token, "X-Sora-Client-Instance": client,
                "X-Sora-Protocol-Version": "1.0", "Origin": "sora-desktop://host"}

    def open_session(self, parent):
        response = self.client.post("/v1/debug/sessions/open", headers=parent,
                                    json={"schema_version": "DesktopDebugSessionOpenRequest.v1", "window_label": "main"})
        self.assertEqual(response.status_code, 200, response.text)
        reply = response.json()
        headers = {**parent, "Authorization": "Bearer " + reply["capability_token"]}
        self.assertNotIn("capability_token", reply["session"])
        return headers, reply["session"]

    def preview(self, command="trace.index.sync", params=None, key="request-one"):
        return self.client.post("/v1/debug/commands/preview", headers=self.headers, json={
            "schema_version": "DesktopAdminPreviewRequest.v1", "command_id": command,
            "params": params if params is not None else {"limit": 1}, "idempotency_key": key,
        })

    def execute(self, preview, **changes):
        body = {"schema_version": "DesktopAdminExecuteRequest.v1", "action_id": preview["action_id"],
                "idempotency_key": preview["idempotency_key"], "expected_revision": preview["expected_revision"], "confirmed": True}
        return self.client.post("/v1/debug/commands/execute", headers=self.headers, json={**body, **changes})

    def test_bootstrap_and_read_scope_cannot_execute_or_read_commands(self):
        self.assertIn(self.client.get("/v1/debug/commands", headers=self.parent).status_code, (401, 403))
        observer = self.token({"debug.read"}, client="observer-a")
        response = self.client.post("/v1/debug/sessions/open", headers=observer, json={"window_label": "main"})
        self.assertIn(response.status_code, (401, 403))

    def test_preview_execute_receipt_and_idempotency_are_real(self):
        first = self.preview().json()
        duplicate = self.preview().json()
        self.assertEqual(first["action_id"], duplicate["action_id"])
        self.assertEqual(first["execution_status"], "previewed")
        self.assertNotIn("confirmation_token", first)
        outcome = self.execute(first)
        self.assertEqual(outcome.status_code, 200, outcome.text)
        result = outcome.json()
        self.assertEqual(result["execution_status"], "executed")
        self.assertIsNotNone(result["audit_event_id"])
        self.assertEqual(self.execute(first).json()["audit_event_id"], result["audit_event_id"])
        self.assertEqual(len(self.memory.quality_ops.list_admin_actions(actor=self.memory.quality_ops.get_admin_action(first["action_id"])["actor"])), 1)
        registry = self.client.get("/v1/debug/commands", headers=self.headers).json()
        self.assertEqual(registry["history"][0]["action_id"], first["action_id"])

    def test_invalid_params_key_reuse_confirmation_and_revision_fail_closed(self):
        self.assertEqual(self.preview(params={"limit": 0}).status_code, 409)
        self.assertEqual(self.preview(params={"limit": "1"}).status_code, 409)
        self.assertEqual(self.preview(params={"limit": 1, "sql": "private-sentinel"}).status_code, 409)
        first = self.preview().json()
        self.assertEqual(self.preview(params={"limit": 2}).status_code, 409)
        self.assertEqual(self.execute(first, confirmed=False).status_code, 422)
        self.assertEqual(self.execute(first, confirmed=1).status_code, 422)
        self.assertEqual(self.execute(first, confirmed="true").status_code, 422)
        self.assertEqual(self.execute(first, expected_revision="a" * 64).status_code, 409)
        self.assertEqual(self.memory.quality_ops.get_admin_action(first["action_id"])["execution_status"], "previewed")

    def test_impact_revision_is_checked_again_before_effects(self):
        state = {"pending": 1}
        calls = []
        self.memory.quality_ops.register_admin_action(AdminActionSpec(
            name="sync_trace_index", risk_level="low",
            preview=lambda params: {"impact_summary": {"pending": state["pending"]}},
            execute=lambda params: calls.append(params) or {"processed": 1},
        ))
        first = self.preview().json()
        state["pending"] = 2
        self.assertEqual(self.execute(first).status_code, 409)
        self.assertEqual(calls, [])

    def test_clients_owner_origin_and_revocation_are_fenced(self):
        first = self.preview().json()
        second_parent = self.token({"debug.session.open"}, client="client-b")
        second, _ = self.open_session(second_parent)
        audit = self.client.get("/v1/debug/commands/actions/" + first["action_id"], headers=second)
        self.assertEqual(audit.status_code, 200)
        self.assertFalse(audit.json()["owned_by_current_client"])
        history = self.client.get("/v1/debug/commands", headers=second).json()["history"]
        self.assertEqual(history[0]["action_id"], first["action_id"])
        denied_execute = self.client.post("/v1/debug/commands/execute", headers=second, json={
            "action_id": first["action_id"], "idempotency_key": first["idempotency_key"],
            "expected_revision": first["expected_revision"], "confirmed": True,
        })
        self.assertEqual(denied_execute.status_code, 409)
        self.assertIn(self.client.get("/v1/debug/commands", headers={**self.headers, "Origin": "https://foreign.example"}).status_code, (401, 403))
        foreign = self.token({"debug.session.open"}, user="user-b", client="foreign-client")
        response = self.client.post("/v1/debug/sessions/open", headers=foreign, json={"window_label": "main"})
        self.assertEqual(response.status_code, 403)
        foreign_profile = self.token({"debug.session.open"}, profile="profile-b", client="foreign-profile")
        response = self.client.post("/v1/debug/sessions/open", headers=foreign_profile, json={"window_label": "main"})
        self.assertEqual(response.status_code, 403)
        old_headers = self.headers
        self.headers, self.session = self.open_session(self.parent)
        self.assertIn(self.client.get("/v1/debug/commands", headers=old_headers).status_code, (401, 403))
        self.assertEqual(self.client.delete("/v1/debug/sessions/" + self.session["session_id"], headers=self.headers).status_code, 200)
        self.assertIn(self.client.get("/v1/debug/commands", headers=self.headers).status_code, (401, 403))

    def test_forbidden_actions_and_raw_outputs_are_not_exposed(self):
        for command in ("export_quality_report", "forget_memory", "force_commit_candidate", "python.eval"):
            self.assertEqual(self.preview(command=command, params={}).status_code, 409)
        self.memory.quality_ops.register_admin_action(AdminActionSpec(
            name="collect_quality_metrics", risk_level="low",
            preview=lambda params: {"impact_summary": {"mutates": ["private-sentinel"], "raw_prompt": "private-sentinel"}},
            execute=lambda params: {"status": "passed", "processed": 2, "api_key": "private-sentinel", "raw_prompt": "private-sentinel"},
        ))
        preview = self.preview(command="eval.metrics.collect", params={}).json()
        self.assertNotIn("private-sentinel", str(preview))
        outcome = self.execute(preview).json()
        self.assertNotIn("private-sentinel", str(outcome))
        self.assertEqual(outcome["result_summary"], {"status": "passed", "processed": 2})
