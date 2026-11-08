import unittest

from sora_sandbox.conversation_gateway.host import create_gateway_app
from sora_sandbox.conversation_runtime.debug_frames import DesktopDebugFrameDetail
from tests import test_conversation_gateway as gateway_fixtures


class FrameGatewayTests(unittest.TestCase):
    def setUp(self):
        self.fixture = gateway_fixtures.ConversationGatewayTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client = self.fixture.client
        self.headers = self.fixture.headers()
        token, _ = self.fixture.capabilities.issue(profile_id="profile-a", subject_id="user-a", agent_id="sora", client_instance_id="desktop-a", permissions={"debug.observe"}, allowed_origins={"app://sora"}, allow_missing_origin=True)
        self.observe_headers = self.fixture.headers(token)
        self.assertEqual(self.client.post("/v1/debug/frame-observation", headers=self.observe_headers, json={"enabled": True}).status_code, 200)
        accepted = self.client.post("/v1/turns", headers=self.headers, json=self.fixture.payload(conversation_id="frame-conversation", text="private-frame-user-text")).json()
        self.client.get(accepted["events_url"], headers=self.headers)
        self.body = {"conversation_id": "frame-conversation", "turn_id": accepted["turn_id"], "frame_id": "interruptibility_frame"}

    def test_real_owner_scoped_detail_is_closed_and_private_text_never_exported(self):
        result = self.client.post("/v1/debug/frame", headers=self.headers, json=self.body)
        self.assertEqual(result.status_code, 200, result.text)
        detail = DesktopDebugFrameDetail.model_validate(result.json(), strict=True)
        self.assertEqual(detail.projection.source, "generation_sealed")
        self.assertNotIn("private-frame-user-text", result.text)
        self.assertLess(len(result.content), 64 * 1024)

    def test_authentication_scope_origin_and_turn_binding_fail_closed(self):
        self.assertIn(self.client.post("/v1/debug/frame", headers={key: value for key, value in self.headers.items() if key != "Authorization"}, json=self.body).status_code, (401, 403))
        self.assertIn(self.client.post("/v1/debug/frame", headers={**self.headers, "Origin": "https://untrusted.example"}, json=self.body).status_code, (401, 403))
        self.assertEqual(self.client.post("/v1/debug/frame", headers=self.headers, json={**self.body, "conversation_id": "unknown"}).status_code, 404)
        self.assertEqual(self.client.post("/v1/debug/frame", headers=self.headers, json={**self.body, "frame_id": "raw_prompt"}).status_code, 422)
        self.assertEqual(self.client.post("/v1/debug/frame", headers=self.headers, json={**self.body, "raw": True}).status_code, 422)
        self.assertEqual(self.client.post("/v1/debug/frame", headers=self.fixture.headers(self.fixture.foreign_token), json=self.body).status_code, 403)
        token, _ = self.fixture.capabilities.issue(profile_id="profile-a", subject_id="user-a", agent_id="sora", client_instance_id="desktop-a", permissions={"history.read"}, allowed_origins={"app://sora"}, allow_missing_origin=True)
        self.assertIn(self.client.post("/v1/debug/frame", headers=self.fixture.headers(token), json=self.body).status_code, (401, 403))

    def test_read_permission_cannot_enable_observation_and_typed_stop_is_real(self):
        self.assertEqual(self.client.post("/v1/debug/frame-observation", headers=self.headers, json={"enabled": True}).status_code, 403)
        self.assertEqual(self.client.post("/v1/debug/frame-observation", headers=self.observe_headers, json={"enabled": "true"}).status_code, 422)
        stopped = self.client.post("/v1/debug/frame-observation", headers=self.observe_headers, json={"enabled": False})
        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(stopped.json(), {"active": False, "expires_at_ms": None})

    def test_turn_id_cannot_be_read_through_another_owned_conversation(self):
        accepted = self.client.post("/v1/turns", headers=self.headers, json=self.fixture.payload(request_id="other", conversation_id="other-owned")).json()
        self.client.get(accepted["events_url"], headers=self.headers)
        self.assertEqual(self.client.post("/v1/debug/frame", headers=self.headers, json={**self.body, "conversation_id": "other-owned"}).status_code, 404)
