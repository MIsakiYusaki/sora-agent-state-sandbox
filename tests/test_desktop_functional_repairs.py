"""Real owner-service regression tests using isolated Memory databases, no provider calls."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from sora_sandbox.conversation_gateway.desktop_commands import install_desktop_command_routes
from sora_sandbox.conversation_gateway.host import _authenticate, _require_memory_owner, _require_protocol
from sora_sandbox.conversation_gateway.security import LocalCapabilityProvider
from sora_sandbox.conversation_gateway.service import GatewayServiceError
from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.conversation_gateway.host import create_gateway_app
from sora_sandbox.interruptibility import InterruptibilityService
from sora_sandbox.turn_observability import TurnEvidence
from sora_sandbox.conversation_runtime.context import ConversationContextPacker
from tests import test_conversation_gateway as gateway_fixtures


class MemoryGovernanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.memory = MemoryRuntime(Path(temporary.name) / "memory.db", owner_user_id="user-a", agent_id="sora")
        self.addCleanup(self.memory.close)
        self.capabilities = LocalCapabilityProvider()
        app = FastAPI()

        @app.exception_handler(GatewayServiceError)
        def handle(_request: Request, error: GatewayServiceError):
            return JSONResponse({"code": error.code.value}, status_code=error.status_code)

        install_desktop_command_routes(
            app, capabilities=self.capabilities, stack=SimpleNamespace(memory=self.memory),
            service=SimpleNamespace(identity_scope=("profile-a", "user-a", "sora"), readiness=lambda: {"active_turns": 0}),
            authenticate=_authenticate, require_protocol=_require_protocol, require_owner=_require_memory_owner,
        )
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.parent = self.token()
        self.headers, self.session = self.open()

    def token(self, user="user-a", permissions=None):
        token, _ = self.capabilities.issue(
            profile_id="profile-a", subject_id=user, agent_id="sora", client_instance_id="client-a",
            permissions=permissions if permissions is not None else {"debug.session.open"},
            allowed_origins={"sora-desktop://host"},
        )
        return {"Authorization": "Bearer " + token, "X-Sora-Client-Instance": "client-a",
                "X-Sora-Protocol-Version": "1.0", "Origin": "sora-desktop://host"}

    def open(self):
        response = self.client.post("/v1/debug/reviews/session", headers=self.parent, json={"window_label": "main"})
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        return {**self.parent, "Authorization": "Bearer " + value["capability_token"]}, value["session"]

    def candidate(self, summary="用户偏好简短回答"):
        evidence = self.memory.add_evidence(source_type="test", content_summary=summary)
        return self.memory.propose(summary=summary, memory_type="preference", evidence_ids=[evidence], reason="test_candidate")

    def rows(self, **params):
        response = self.client.get("/v1/debug/memory-governance", headers=self.headers, params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def decide(self, row, action="approve", **changes):
        return self.client.post("/v1/debug/memory-governance/resolve", headers=self.headers,
                                json={"review_id": row["id"], "action": action, "expected_revision": row["revision"], "confirmed": True, **changes})

    def test_real_approval_commit_receipt_repeat_and_queue_refresh(self):
        candidate = self.candidate()
        row = self.rows()["items"][0]
        self.assertTrue(row["can_approve"])
        receipt = self.decide(row)
        self.assertEqual(receipt.status_code, 200, receipt.text)
        self.assertEqual(receipt.json()["status"], "approved")
        self.assertEqual(self.memory.get_candidate(candidate["candidate_id"])["status"], "committed")
        self.assertEqual(self.rows()["items"], [])
        count = len(self.memory.list_memories())
        self.assertEqual(self.decide(row).json()["status"], "already_resolved")
        self.assertEqual(len(self.memory.list_memories()), count)
        self.assertEqual(self.decide(row, "reject").status_code, 409)

    def test_reject_and_defer_use_owner_governance_and_auditable_actor(self):
        for action, expected in (("reject", "rejected"), ("defer", "deferred")):
            candidate = self.candidate("用户希望日常回答简洁" + action)
            row = self.rows()["items"][0]
            response = self.decide(row, action)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], expected)
            self.assertEqual(self.memory.get_candidate(candidate["candidate_id"])["status"], expected)
            review = self.memory.get_review_item(row["id"])
            self.assertTrue(review["resolved_by"].startswith("desktop_review_"))
            self.assertIsNotNone(review["resolved_at"])

    def test_revision_and_explicit_confirmation_fail_before_effects(self):
        candidate = self.candidate()
        row = self.rows()["items"][0]
        for value in (False, 1, "true"):
            self.assertEqual(self.decide(row, confirmed=value).status_code, 422)
        self.assertEqual(self.decide(row, expected_revision="0" * 64).status_code, 409)
        self.assertEqual(self.memory.get_candidate(candidate["candidate_id"])["status"], "needs_review")
        self.memory.defer_review(row["id"], actor="other-reviewer")
        self.assertEqual(self.decide(row).status_code, 409)
        self.assertEqual(self.memory.list_memories(), [])

    def test_secret_summary_is_not_disclosed_and_cannot_be_blindly_approved(self):
        candidate = self.candidate("用户偏好简短回答")
        # Failure-injection fixture only: emulate a stale classification with secret-looking text.
        with self.memory._lock:
            self.memory._conn.execute("UPDATE memory_candidate SET summary = ? WHERE id = ?",
                                      ("sk-" + "sensitive-sentinel-" * 3, candidate["candidate_id"]))
            self.memory._conn.commit()
        page = self.rows()
        self.assertNotIn("sensitive-sentinel", str(page))
        row = page["items"][0]
        self.assertIsNone(row["summary"])
        self.assertFalse(row["can_approve"])
        self.assertEqual(self.decide(row).status_code, 403)

    def test_parent_maintenance_other_owner_origin_and_closed_session_denied(self):
        path = "/v1/debug/memory-governance"
        self.assertIn(self.client.get(path, headers=self.parent).status_code, (401, 403))
        self.assertEqual(self.client.post("/v1/debug/reviews/session", headers=self.token(user="other-user"), json={"window_label": "main"}).status_code, 403)
        self.assertIn(self.client.get(path, headers={**self.headers, "Origin": "https://foreign.invalid"}).status_code, (401, 403))
        self.assertIn(self.client.post("/v1/debug/commands/preview", headers=self.headers, json={"command_id": "trace.index.sync", "params": {"limit": 1}, "idempotency_key": "no-execute"}).status_code, (401, 403))
        self.assertEqual(self.client.delete("/v1/debug/sessions/" + self.session["session_id"], headers=self.headers).status_code, 200)
        self.assertIn(self.client.get(path, headers=self.headers).status_code, (401, 403))

    def test_new_session_revokes_previous_and_only_main_window_can_open(self):
        old = self.headers
        self.headers, _ = self.open()
        self.assertIn(self.client.get("/v1/debug/memory-governance", headers=old).status_code, (401, 403))
        self.assertEqual(self.client.post("/v1/debug/reviews/session", headers=self.parent, json={"window_label": "auxiliary"}).status_code, 422)

    def test_pending_reviews_pagination_and_typed_queues(self):
        self.candidate("用户喜欢简短的日常回答")
        self.candidate("用户希望解释清晰")
        first = self.rows(limit=1)
        self.assertTrue(first["truncated"])
        second = self.rows(limit=1, offset=first["next_offset"])
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
        self.assertIsNone(second["next_offset"])
        self.assertEqual(len(self.rows(kind="candidates")["items"]), 2)
        self.assertEqual(self.rows(kind="quarantines")["items"], [])
        self.assertEqual(self.client.get("/v1/debug/memory-governance?kind=raw_sql", headers=self.headers).status_code, 422)
        self.assertEqual(self.client.get("/v1/debug/memory-governance?offset=-1", headers=self.headers).status_code, 422)

    def test_queue_failure_rows_come_from_owner_and_never_disclose_error_payload(self):
        self.memory.add_manual("合成偏好，用于索引定位")
        self.memory.add_manual("合成人格来源，用于投影定位", memory_type="profile")
        index = self.memory.list_index_outbox(limit=1)[0]
        projection = self.memory.list_projector_outbox(limit=1)[0]
        with self.memory._lock:
            self.memory._conn.execute("UPDATE memory_index_outbox SET status='failed', attempts=2, error=?, error_type='timeout' WHERE id=?", ("private-error-sentinel", index["id"]))
            self.memory._conn.execute("UPDATE memory_projector_outbox SET status='pending', attempts=1, error=?, error_type='adapter_exception' WHERE id=?", ("private-projection-sentinel", projection["id"]))
            self.memory._conn.commit()
        read = self.token(permissions={"debug.read"})
        for kind, record in (("index", index), ("projection", projection)):
            response = self.client.get(f"/v1/debug/memory-queue?kind={kind}&status=failed", headers=read)
            self.assertEqual(response.status_code, 200, response.text)
            row = response.json()["items"][0]
            self.assertEqual(row["id"], record["id"])
            self.assertEqual(len(row["error_fingerprint"]), 16)
            self.assertIsNotNone(row["error_code"])
            self.assertNotIn("private-", response.text)
            self.assertNotIn("payload", response.text)
        self.assertIn(self.client.get("/v1/debug/memory-queue?kind=index", headers=self.parent).status_code, (401, 403))
        self.assertEqual(self.client.get("/v1/debug/memory-queue?kind=arbitrary_sql", headers=read).status_code, 422)

    def test_projection_maintenance_uses_real_preview_execution_audit_and_no_automatic_replay(self):
        opened = self.client.post("/v1/debug/sessions/open", headers=self.parent, json={"window_label": "main"}).json()
        headers = {**self.parent, "Authorization": "Bearer " + opened["capability_token"]}
        registry = self.client.get("/v1/debug/commands", headers=headers).json()
        command = next(item for item in registry["commands"] if item["command_id"] == "memory.projection.drain")
        self.assertTrue(command["available"])
        preview = self.client.post("/v1/debug/commands/preview", headers=headers,
            json={"command_id": command["command_id"], "params": {"limit": 1}, "idempotency_key": "projection-regression"})
        self.assertEqual(preview.status_code, 200, preview.text)
        value = preview.json()
        self.assertIn("memory_projector_outbox", value["impact_summary"]["mutates"])
        body = {"action_id": value["action_id"], "idempotency_key": value["idempotency_key"], "expected_revision": value["expected_revision"], "confirmed": True}
        receipt = self.client.post("/v1/debug/commands/execute", headers=headers, json=body)
        self.assertEqual(receipt.status_code, 200, receipt.text)
        self.assertEqual(receipt.json()["execution_status"], "executed")
        self.assertIsNotNone(receipt.json()["audit_event_id"])
        repeat = self.client.post("/v1/debug/commands/execute", headers=headers, json=body)
        self.assertEqual(repeat.json()["audit_event_id"], receipt.json()["audit_event_id"])

    def quality_session(self):
        opened = self.client.post("/v1/debug/quality/session", headers=self.parent, json={"window_label": "main"})
        self.assertEqual(opened.status_code, 200, opened.text)
        return {**self.parent, "Authorization": "Bearer " + opened.json()["capability_token"]}, opened.json()["session"]

    def test_quality_real_cases_baseline_comparison_and_read_only_projection(self):
        ops = self.memory.quality_ops
        failed = {"value": False}
        ops.register_eval_suite("desktop_fixture", lambda: {"cases": [{"name": "baseline_case", "status": "failed" if failed["value"] else "passed", "expected": {"raw_prompt": "private-expected-sentinel"}, "actual": {"raw_prompt": "private-actual-sentinel"}, "accuracy": 0.5, "raw_prompt": "private-metric-sentinel"}]}, source_module="desktop_test_fixture")
        first = ops.run_quality_eval(suites=["desktop_fixture"])
        baseline = ops.pin_baseline(first["id"], name="private-name-sentinel")
        failed["value"] = True
        second = ops.run_quality_eval(suites=["desktop_fixture"])
        headers, _ = self.quality_session()
        before = len(ops.list_quality_events(limit=1000))
        response = self.client.get("/v1/debug/quality", headers=headers, params={"kind": "runs", "record_id": second["id"], "baseline_id": baseline["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["cases"][0]["status"], "failed")
        self.assertEqual(body["cases"][0]["metrics"], {"accuracy": 0.5})
        self.assertEqual(body["comparison"]["status"], "failed")
        self.assertGreater(body["comparison"]["unaccepted_regression_count"], 0)
        self.assertNotIn("private-", response.text)
        self.assertEqual(len(ops.list_quality_events(limit=1000)), before)
        baseline_page = self.client.get("/v1/debug/quality?kind=baselines", headers=headers)
        self.assertEqual(baseline_page.status_code, 200)
        self.assertNotIn("private-name-sentinel", baseline_page.text)

    def test_quality_resolution_is_confirmed_revision_checked_idempotent_and_audited(self):
        ops = self.memory.quality_ops
        event = ops.record_quality_event(event_type="test_failure", severity="error", message="private-event-sentinel", payload={"raw_prompt": "private-payload-sentinel"})
        headers, session = self.quality_session()
        response = self.client.get("/v1/debug/quality", headers=headers, params={"kind": "events", "record_id": event["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("private-", response.text)
        row = response.json()["selected"]
        body = {"event_id": event["id"], "expected_revision": row["revision"], "resolution": "investigated", "confirmed": True}
        for confirmation in (False, 1, "true"):
            self.assertEqual(self.client.post("/v1/debug/quality/resolve", headers=headers, json={**body, "confirmed": confirmation}).status_code, 422)
        self.assertEqual(self.client.post("/v1/debug/quality/resolve", headers=headers, json={**body, "expected_revision": "0" * 64}).status_code, 409)
        resolved = self.client.post("/v1/debug/quality/resolve", headers=headers, json=body)
        self.assertEqual(resolved.status_code, 200, resolved.text)
        audit = ops.get_quality_event(resolved.json()["audit_event_id"])
        self.assertEqual(audit["source_id"], event["id"])
        self.assertEqual(audit["payload"]["resolution"], "investigated")
        self.assertTrue(audit["payload"]["actor"].startswith("desktop_quality_"))
        self.assertEqual(ops.get_quality_event(event["id"])["status"], "resolved")
        repeat = self.client.post("/v1/debug/quality/resolve", headers=headers, json=body)
        self.assertEqual(repeat.json()["status"], "already_resolved")
        self.assertEqual(repeat.json()["audit_event_id"], resolved.json()["audit_event_id"])
        self.assertIn(self.client.get("/v1/debug/quality?kind=events", headers=self.parent).status_code, (401, 403))
        self.assertIn(self.client.post("/v1/debug/commands/execute", headers=headers, json={"action_id": "unavailable_command", "idempotency_key": "not_authorized", "expected_revision": "0" * 64, "confirmed": True}).status_code, (401, 403))
        self.client.delete("/v1/debug/sessions/" + session["session_id"], headers=headers)
        self.assertIn(self.client.get("/v1/debug/quality?kind=events", headers=headers).status_code, (401, 403))

    def test_quality_resolution_rolls_back_if_audit_cannot_be_written(self):
        ops = self.memory.quality_ops
        event = ops.record_quality_event(event_type="test_failure", severity="error", message="test-only")
        revision = ops.quality_event_revision(ops.get_quality_event(event["id"]))
        with patch.object(ops, "record_quality_event", side_effect=RuntimeError("injected-audit-failure")):
            with self.assertRaises(RuntimeError):
                ops.resolve_quality_event(event["id"], expected_revision=revision, resolution="investigated", actor="test-operator")
        self.assertEqual(ops.get_quality_event(event["id"])["status"], "open")
        self.assertEqual(len(ops.list_quality_events(limit=1000)), 1)

    def test_quality_session_enforces_expiry_owner_client_and_replacement(self):
        headers, _ = self.quality_session()
        path = "/v1/debug/quality?kind=events"
        self.assertEqual(self.client.get(path, headers=headers).status_code, 200)
        self.assertIn(self.client.get(path, headers={**headers, "X-Sora-Client-Instance": "other-client"}).status_code, (401, 403))
        self.assertIn(self.client.get(path, headers={**headers, "Origin": "app://foreign"}).status_code, (401, 403))
        for changes in ({"profile_id": "other-profile"}, {"subject_id": "other-user"}, {"agent_id": "other-agent"}):
            identity = {"profile_id": "profile-a", "subject_id": "user-a", "agent_id": "sora", **changes}
            token, _ = self.capabilities.issue(**identity, client_instance_id="client-a", permissions={"debug.quality.read"}, allowed_origins={"sora-desktop://host"})
            self.assertEqual(self.client.get(path, headers={**headers, "Authorization": "Bearer " + token}).status_code, 403)
        replacement, session = self.quality_session()
        self.assertIn(self.client.get(path, headers=headers).status_code, (401, 403))
        with patch("sora_sandbox.conversation_gateway.security.now_ms", return_value=session["expires_at_ms"] + 1):
            self.assertIn(self.client.get(path, headers=replacement).status_code, (401, 403))

    def quarantine(self):
        evidence = self.memory.add_evidence(source_type="test", content_summary="private-quarantine-sentinel", sensitivity="high")
        candidate = self.memory.propose(summary="private-quarantine-sentinel", memory_type="preference", evidence_ids=[evidence], sensitivity="high", confidence=0.95, reason="test_high_risk")
        response = self.client.get("/v1/debug/memory-governance?kind=quarantines", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("private-quarantine-sentinel", response.text)
        row = response.json()["items"][0]
        self.assertTrue(row["can_dismiss"])
        self.assertFalse(row["can_approve"])
        self.assertEqual(row["summary_state"], "protected")
        return candidate, row

    def test_quarantine_dismissal_is_atomic_audited_idempotent_and_never_commits_memory(self):
        candidate, row = self.quarantine()
        path = "/v1/debug/memory-governance/quarantines/dismiss"
        body = {"quarantine_id": row["id"], "expected_revision": row["revision"], "confirmed": True}
        for value in (False, 1, "true"):
            self.assertEqual(self.client.post(path, headers=self.headers, json={**body, "confirmed": value}).status_code, 422)
        self.assertEqual(self.client.post(path, headers=self.headers, json={**body, "expected_revision": "0" * 64}).status_code, 409)
        response = self.client.post(path, headers=self.headers, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        receipt = response.json()
        self.assertEqual(receipt["status"], "dismissed")
        self.assertEqual(self.memory.get_candidate(candidate["candidate_id"])["status"], "rejected")
        item = self.memory.get_quarantine(row["id"])
        self.assertEqual(item["status"], "dismissed")
        self.assertTrue(item["resolved_by"].startswith("desktop_quarantine_"))
        audit = self.memory.gate_pipeline.get_gate_decision(receipt["audit_id"])
        self.assertEqual(audit["decision"], "REJECT")
        self.assertEqual(audit["trace"]["quarantine_id"], row["id"])
        repeated = self.client.post(path, headers=self.headers, json=body)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["status"], "already_dismissed")
        self.assertEqual(repeated.json()["audit_id"], receipt["audit_id"])
        self.assertEqual(self.memory.list_memories(), [])
        self.assertEqual(self.memory.list_quarantines(), [])
        self.assertNotIn("private-quarantine-sentinel", response.text)

    def test_quarantine_dismissal_rolls_back_if_audit_fails(self):
        candidate, row = self.quarantine()
        with patch.object(self.memory.gate_pipeline, "_record_gate_decision", side_effect=RuntimeError("injected-audit-failure")):
            with self.assertRaises(RuntimeError):
                self.memory.dismiss_quarantine_checked(row["id"], expected_revision=row["revision"], actor="test-actor")
        self.assertEqual(self.memory.get_candidate(candidate["candidate_id"])["status"], "quarantined")
        self.assertEqual(self.memory.get_quarantine(row["id"])["status"], "active")
        self.assertFalse(self.memory._conn.in_transaction)

    def test_quality_capability_cannot_dismiss_memory_quarantine(self):
        _, row = self.quarantine()
        headers, _ = self.quality_session()
        response = self.client.post("/v1/debug/memory-governance/quarantines/dismiss", headers=headers, json={"quarantine_id": row["id"], "expected_revision": row["revision"], "confirmed": True})
        self.assertIn(response.status_code, (401, 403))
        self.assertEqual(self.memory.get_quarantine(row["id"])["status"], "active")

    def test_quality_page_caps_history_and_paginates_cases_without_payload(self):
        ops = self.memory.quality_ops
        ops.register_eval_suite("large_fixture", lambda: {"cases": [{"name": f"case_{index}", "status": "passed", "actual": {"body": "private-case-sentinel"}} for index in range(102)]}, source_module="test_fixture")
        run = ops.run_quality_eval(suites=["large_fixture"])
        for _ in range(102):
            ops.record_quality_event(event_type="test_failure", severity="error", message="private-event-sentinel", source_id="private-source-sentinel")
        headers, _ = self.quality_session()
        events = self.client.get("/v1/debug/quality?kind=events", headers=headers)
        self.assertEqual(events.status_code, 200)
        self.assertTrue(events.json()["truncated"])
        self.assertEqual(len(events.json()["items"]), 100)
        self.assertNotIn("private-", events.text)
        first = self.client.get("/v1/debug/quality", headers=headers, params={"kind": "runs", "record_id": run["id"]}).json()
        second_response = self.client.get("/v1/debug/quality", headers=headers, params={"kind": "runs", "record_id": run["id"], "case_offset": first["next_case_offset"]})
        second = second_response.json()
        self.assertEqual(first["case_count"], 102)
        self.assertEqual(len(first["cases"]), 100)
        self.assertEqual(len(second["cases"]), 2)
        self.assertIsNone(second["next_case_offset"])
        self.assertFalse({item["id"] for item in first["cases"]} & {item["id"] for item in second["cases"]})
        self.assertNotIn("private-", second_response.text)


class HistoryAndDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = gateway_fixtures.ConversationGatewayTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service = self.fixture.service
        self.interruptibility = InterruptibilityService()
        app = create_gateway_app(self.service, self.fixture.capabilities, own_service_lifecycle=False,
                                 interruptibility_service=self.interruptibility, runtime_stack=self.fixture.debug_stack)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        token, _ = self.fixture.capabilities.issue(profile_id="profile-a", subject_id="user-a", agent_id="sora", client_instance_id="desktop-a",
            permissions={"controls.read", "controls.write", "history.read", "debug.read", "debug.observe"}, allowed_origins={"app://sora"}, allow_missing_origin=True)
        self.headers = self.fixture.headers(token)

    def test_defaults_require_confirmation_persist_across_conversations_and_reset_without_memory_deletion(self):
        payload = {"action": "save", "confirmed": True, "reply_depth": "deep", "follow_up": "none", "topic_shift": "adjacent", "memory_mode": "explicit"}
        path = "/v1/conversations/one/control-defaults"
        for value in (False, 1, "true"):
            self.assertEqual(self.client.post(path, headers=self.headers, json={**payload, "confirmed": value}).status_code, 422)
        result = self.client.post(path, headers=self.headers, json=payload)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotIn("confirmation_token", result.text)
        state = self.service.state_repository.load("profile-a")
        preferences = state["interruptibility"]["profile"]["persistent_preferences"]
        self.assertEqual(preferences["reply_depth"], "deep")
        self.assertEqual(preferences["state_surface"], {"memory": "explicit"})
        self.assertTrue(state["interruptibility"]["profile"]["confirmation_proofs"])
        other = self.client.get("/v1/conversations/two/controls", headers=self.headers)
        self.assertEqual(other.json()["controls"]["reply_depth"]["value"], "deep")
        self.assertEqual(other.json()["controls"]["follow_up"]["value"], "none")
        reset = self.client.post(path, headers=self.headers, json={"action": "reset", "confirmed": True})
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(reset.json()["controls"]["reply_depth"]["value"], "balanced")
        self.assertEqual(reset.json()["controls"]["state_surface"]["value"], "implicit")

    def test_quiet_control_is_duration_scoped_and_does_not_mute_reactive_responses(self):
        response = self.client.put("/v1/conversations/one/controls", headers=self.headers,
            json={"control": "quiet", "value": True, "scope": "duration", "duration_seconds": 3600})
        self.assertEqual(response.status_code, 200, response.text)
        values = response.json()["controls"]
        self.assertTrue(values["quiet"]["value"])
        self.assertIsNotNone(values["quiet"]["expires_at"])
        self.assertFalse(values["response_mute"]["value"])
        response = self.client.put("/v1/conversations/one/controls", headers=self.headers,
            json={"control": "quiet", "value": False, "scope": "duration", "duration_seconds": 3600})
        self.assertFalse(response.json()["controls"]["quiet"]["value"])

    def test_frame_observation_status_survives_panel_reentry_without_frame_data(self):
        path = "/v1/debug/frame-observation"
        self.assertEqual(self.client.get(path, headers=self.headers).json()["active"], False)
        started = self.client.post(path, headers=self.headers, json={"enabled": True})
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(self.client.get(path, headers=self.headers).json(), started.json())
        self.client.post(path, headers=self.headers, json={"enabled": False})
        self.assertFalse(self.client.get(path, headers=self.headers).json()["active"])
        self.assertIn(self.client.get(path, headers=self.fixture.headers(self.fixture.foreign_token)).status_code, (401, 403))

    def history_fixture(self, external_id, contents, *, user="user-a"):
        binding = self.fixture.registry.ensure_conversation(profile_id="profile-a", subject_id=user, agent_id="sora", external_conversation_id=external_id)
        journal = self.fixture.runtime.journal
        journal.import_legacy_history(binding.runtime_conversation_id, [{"role": "user", "content": contents[0]}])
        # Expand encrypted test history without running hundreds of artificial provider calls.
        existing = journal.history_page(binding.runtime_conversation_id, limit=1)[0]
        with journal.transaction() as connection:
            for sequence, content in enumerate(contents[1:], 2):
                history_id = f"test-{external_id}-{sequence}"
                connection.execute("INSERT INTO runtime_short_turn (history_id, conversation_id, turn_id, sequence, role, content_cipher, content_hash, created_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (history_id, binding.runtime_conversation_id, existing["turn_id"], sequence, f"fixture-{sequence}",
                     journal.cipher.encrypt_text(content, associated_data=f"history:{history_id}"), "fixture-only", existing["created_at_ms"] + sequence))
        return binding

    def test_full_body_search_covers_beyond_last_200_messages_without_foreign_data(self):
        contents = ["最初对话标题", "唯一正文关键词"] + [f"普通正文{i}" for i in range(210)] + ["最后摘要"]
        self.history_fixture("long", contents)
        self.history_fixture("foreign", ["外部标题", "不可见私人关键词"], user="other-user")
        result = self.client.get("/v1/conversations?query=唯一正文关键词", headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        row = result.json()["items"][0]
        self.assertEqual(row["title"], "最初对话标题")
        self.assertEqual(row["preview"], "最后摘要")
        self.assertEqual(row["message_count"], len(contents))
        self.assertEqual(self.client.get("/v1/conversations?query=不可见私人关键词", headers=self.headers).json()["items"], [])
        self.assertNotIn("不可见私人关键词", result.text)

    def test_more_than_100_owned_conversations_can_all_be_paged(self):
        for index in range(103):
            self.history_fixture(f"page-{index}", [f"对话{index}"])
        first = self.client.get("/v1/conversations", headers=self.headers).json()
        self.assertEqual(len(first["items"]), 100)
        self.assertEqual(first["next_offset"], 100)
        second = self.client.get("/v1/conversations?offset=100", headers=self.headers).json()
        self.assertEqual(len(second["items"]), 3)
        self.assertIsNone(second["next_offset"])
        self.assertEqual(len({row["conversation_id"] for row in first["items"] + second["items"]}), 103)

    def test_public_basis_is_recorded_at_generation_and_never_exposed_in_debug_snapshot(self):
        collector = TurnEvidence()
        pack = {"session_context": {"recent_turns": [{"role": "user", "content": "private-recent-text"}]},
                "recollection_frame": {"items": [{"memory_id": "memory-visible", "summary": "private-memory-gist"}]}}
        with collector.activate():
            ConversationContextPacker().pack_for_generation(pack)
        basis = collector.response_basis()
        self.assertEqual(basis["source"], "generation_sealed")
        self.assertEqual(basis["memory_ids"], ["memory-visible"])
        self.assertEqual(basis["recent_message_count"], 1)
        self.assertNotIn("private-memory-gist", str(basis))
        self.assertNotIn("private-recent-text", str(basis))
        self.assertNotIn("memory-visible", str(collector.snapshot()))

    def test_silent_memory_disclosure_removes_items_as_well_as_summaries_before_basis_capture(self):
        from sora_sandbox.conversation_runtime.contracts import UnifiedDisclosureLevel
        pack = {"recollection_frame": {"items": [{"memory_id": "private-id", "summary": "private-gist"}], "memory_summaries": ["private-gist"], "retrieved_ids": ["private-id"]}}
        ConversationContextPacker._apply_disclosure(pack, {name: UnifiedDisclosureLevel.SILENT for name in ("memory", "persona", "affect", "relationship", "worldops")})
        self.assertEqual(pack["recollection_frame"]["items"], [])
        collector = TurnEvidence()
        collector.capture_response_basis(pack)
        self.assertEqual(collector.response_basis()["memory_ids"], [])


class UserMemoryBridgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = gateway_fixtures.ConversationGatewayTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.memory = MemoryRuntime(self.fixture.root / "real-memory.db", owner_user_id="user-a", agent_id="sora")
        self.addCleanup(self.memory.close)
        app = create_gateway_app(self.fixture.service, self.fixture.capabilities, own_service_lifecycle=False, memory_runtime=self.memory)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        token, _ = self.fixture.capabilities.issue(profile_id="profile-a", subject_id="user-a", agent_id="sora", client_instance_id="desktop-a",
            permissions={"memory.read", "memory.write", "history.read", "turn.submit", "turn.read", "debug.read"}, allowed_origins={"app://sora"}, allow_missing_origin=True)
        self.headers = self.fixture.headers(token)

    def test_actual_sources_revision_checked_correction_and_forbidden_secrets(self):
        memory = self.memory.add_manual("用户偏好短回复")
        path = f"/v1/memories/{memory['id']}"
        detail = self.client.get(path + "/details", headers=self.headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["sources"][0]["kind"], "manual")
        self.assertNotIn("raw_content", detail.text)
        self.assertNotIn("content_summary", detail.text)
        body = {"summary": "用户偏好清晰但不必很短的回复", "expected_revision": detail.json()["revision"], "confirmed": True}
        self.assertEqual(self.client.post(path + "/correct", headers=self.headers, json={**body, "confirmed": 1}).status_code, 422)
        self.assertEqual(self.client.post(path + "/correct", headers=self.headers, json={**body, "expected_revision": "0" * 64}).status_code, 409)
        self.assertEqual(self.client.post(path + "/correct", headers=self.headers, json={**body, "summary": "sk-" + "never-persist-this-" * 3}).status_code, 422)
        result = self.client.post(path + "/correct", headers=self.headers, json=body)
        self.assertEqual(result.status_code, 200, result.text)
        updated = result.json()["memory"]
        self.assertNotEqual(updated["id"], memory["id"])
        self.assertEqual(updated["summary"], body["summary"])
        self.assertEqual(self.memory.get_memory(memory["id"])["status"], "superseded")
        self.assertEqual(self.client.post(path + "/correct", headers=self.headers, json=body).status_code, 409)

    def test_hidden_memory_cannot_be_silently_unhidden_through_correction(self):
        memory = self.memory.add_manual("用户偏好安静的环境")
        self.memory.hide_memory(memory["id"])
        path = f"/v1/memories/{memory['id']}"
        detail = self.client.get(path + "/details", headers=self.headers).json()
        self.assertTrue(detail["can_correct"])
        result = self.client.post(path + "/correct", headers=self.headers, json={"summary": "替代摘要", "confirmed": True, "expected_revision": detail["revision"]})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()["memory"]["visible"])
        self.assertEqual(self.memory.get_memory(memory["id"])["status"], "superseded")
        self.assertEqual(len([item for item in self.memory.list_memories() if item["status"] == "active"]), 1)

    def test_response_basis_is_scoped_and_respects_current_hidden_memory(self):
        accepted = self.fixture.client.post("/v1/turns", headers=self.fixture.headers(), json=self.fixture.payload(conversation_id="basis", text="合成对话")).json()
        self.fixture.client.get(accepted["events_url"], headers=self.fixture.headers())
        memory = self.memory.add_manual("用户偏好清晰解释")
        self.fixture.runtime.journal.put_artifact(accepted["turn_id"], "user_response_basis",
            {"schema_version": "UserResponseBasisReferences.v1", "source": "generation_sealed", "recent_message_count": 2, "memory_ids": [memory["id"]], "unidentified_memory_count": 0}, schema_version="UserResponseBasisReferences.v1")
        path = f"/v1/conversations/basis/responses/{accepted['turn_id']}/basis"
        result = self.client.get(path, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["memories"][0]["memory_id"], memory["id"])
        self.memory.hide_memory(memory["id"])
        hidden = self.client.get(path, headers=self.headers).json()
        self.assertEqual(hidden["memories"], [])
        self.assertEqual(hidden["unavailable_memory_count"], 1)
        self.assertNotIn(memory["summary"], str(hidden))
        self.assertEqual(self.client.get(path.replace("/basis/responses", "/wrong/responses"), headers=self.headers).status_code, 404)
        self.assertIn(self.client.get(path, headers=self.fixture.headers(self.fixture.foreign_token)).status_code, (401, 403))
