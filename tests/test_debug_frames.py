import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError
from sora_sandbox.conversation_runtime.context import ConversationContextPacker, generation_context_hash, GENERATION_CONTEXT_MARKER
from sora_sandbox.conversation_runtime.debug_frames import DebugFrameProjection, FrameFieldView, project_frame
from sora_sandbox.turn_observability import TurnEvidence


class DebugFrameTests(unittest.TestCase):
    def test_sampling_defaults_off_even_with_an_active_timeline_collector(self):
        collector = TurnEvidence()
        with collector.activate():
            ConversationContextPacker().pack_for_generation({"persona_frame": {"growth_paused": False}})
        self.assertIsNone(collector.snapshot()["pre_generation"])

    def test_sampling_has_a_five_minute_deadline_and_stops_immediately(self):
        from tests.test_conversation_runtime import ConversationRuntimeTests
        helper = ConversationRuntimeTests()
        helper.setUp()
        self.addCleanup(helper.tearDown)
        runtime = helper.runtime()
        self.addCleanup(runtime.close)
        with patch("sora_sandbox.conversation_runtime.runtime.now_ms", return_value=1000):
            self.assertEqual(runtime.frame_observation(), {"active": False, "expires_at_ms": None})
            self.assertEqual(runtime.set_frame_observation(True), {"active": True, "expires_at_ms": 301000})
        with patch("sora_sandbox.conversation_runtime.runtime.now_ms", return_value=301001):
            self.assertEqual(runtime.frame_observation(), {"active": False, "expires_at_ms": None})
            runtime.set_frame_observation(True)
            self.assertEqual(runtime.set_frame_observation(False), {"active": False, "expires_at_ms": None})

    def test_allowlisted_values_keep_false_zero_and_explicit_null_distinct(self):
        view = project_frame("persona_frame", {"schema_version": "PersonaFrame.v1", "growth_paused": False,
            "snapshot_version": 0, "trait_bands": {"warmth": None, "curiosity": "high"}}, source="generation_sealed")
        fields = {item.path: item for item in view.fields}
        self.assertIs(fields["growth_paused"].value, False)
        self.assertEqual(fields["snapshot_version"].value, 0)
        self.assertEqual(fields["trait_bands.warmth"].state, "observed")
        self.assertIsNone(fields["trait_bands.warmth"].value)
        self.assertEqual(fields["trait_bands.caution"].state, "missing")

    def test_prompt_evidence_rationale_paths_and_unknown_keys_never_export(self):
        secret = "private-token-sentinel"
        view = project_frame("persona_frame", {"runtime_mode": secret, "growth_paused": secret,
            "trait_bands": {"warmth": secret, secret: "high"}, "narrative_gist": secret,
            "snapshot_id": secret, secret: secret, "chain_of_thought": secret,
            "expression_bias": {"warmth": .81}}, source="delivery_snapshot")
        exported = view.model_dump_json()
        self.assertNotIn(secret, exported)
        self.assertNotIn("chain_of_thought", exported)
        self.assertNotIn("expression_bias", [field.path for field in view.fields])
        self.assertNotIn("narrative_gist", [field.path for field in view.fields])
        self.assertNotIn("0.81", exported)
        self.assertGreater(view.protected_field_count, 0)
        self.assertEqual(next(field for field in view.fields if field.path == "runtime_mode").state, "invalid")

    def test_collection_is_counted_not_exported_and_number_strings_fail_closed(self):
        frame = {"items": [{"summary": "private-text"}], "degraded": False, "retrieved_ids": ["secret-ref"]}
        view = project_frame("recollection_frame", frame, source="generation_sealed")
        self.assertNotIn("private-text", view.model_dump_json())
        self.assertNotIn("secret-ref", view.model_dump_json())
        fields = {field.path: field for field in view.fields}
        self.assertEqual(fields["items"].value, 1)
        invalid = project_frame("dialogue_control_frame", {"question_budget": "0"}, source="generation_sealed")
        self.assertEqual(next(row for row in invalid.fields if row.path == "question_budget").state, "invalid")

    def test_persisted_projection_is_validated_again_not_trusted_as_arbitrary_json(self):
        view = project_frame("persona_frame", {"trait_bands": {"warmth": "high"}}, source="generation_sealed")
        payload = view.model_dump()
        row = next(item for item in payload["fields"] if item["path"] == "trait_bands.warmth")
        row["value"] = "private-token-sentinel"
        with self.assertRaises(ValidationError):
            DebugFrameProjection.model_validate(payload)
        with self.assertRaises(ValidationError):
            FrameFieldView(path="a", state="missing", value=0)

    def test_sealed_capture_is_after_disclosure_and_does_not_change_generation_hash(self):
        collector = TurnEvidence()
        source = {"interruptibility_frame": {"state_surface_permission": {"memory": "silent", "persona": "silent"}},
                  "persona_frame": {"narrative_gist": "private-gist", "growth_paused": False, "trait_bands": {"warmth": "high"}},
                  "recollection_frame": {"memory_summaries": ["private-memory"], "retrieved_ids": ["memory-private"], "surface_action": "explicit"}}
        packer = ConversationContextPacker()
        expected = packer.pack_for_generation(source)
        with collector.activate():
            actual = packer.pack_for_generation(source, capture_frame_views=True)
        self.assertEqual(actual, expected)
        gate = actual.pop(GENERATION_CONTEXT_MARKER)
        self.assertEqual(gate["context_hash"], generation_context_hash(actual))
        captured = collector.snapshot()["pre_generation"]["frame_projection"]
        self.assertNotIn("private", json.dumps(captured))
        memory = next(item for item in captured["frames"] if item["frame_id"] == "recollection_frame")
        fields = {field["path"]: field for field in memory["fields"]}
        self.assertEqual(fields["retrieved_ids"]["value"], 0)
        self.assertEqual(memory["source"], "generation_sealed")

    def test_projection_failure_never_changes_context_or_generation_gate(self):
        packer = ConversationContextPacker()
        source = {"persona_frame": {"growth_paused": False}}
        expected = packer.pack_for_generation(source)
        with TurnEvidence().activate(), patch("sora_sandbox.conversation_runtime.debug_frames.project_generation", side_effect=RuntimeError("private-exception")):
            self.assertEqual(packer.pack_for_generation(source, capture_frame_views=True), expected)

    def test_runtime_detail_uses_sealed_record_and_rejects_another_conversation(self):
        from tests.test_conversation_runtime import ConversationRuntimeTests
        from sora_sandbox.state import seed_state
        helper = ConversationRuntimeTests()
        helper.setUp()
        self.addCleanup(helper.tearDown)
        runtime = helper.runtime()
        self.addCleanup(runtime.close)
        runtime.set_frame_observation(True)
        result = runtime.run(seed_state(), "private-input-sentinel", request_id="frame-request")
        turn_id = result["runtime"]["turn_id"]
        detail = runtime.diagnostic_frame("local-sandbox", turn_id, "interruptibility_frame")
        self.assertEqual(detail["projection"]["source"], "generation_sealed")
        self.assertNotIn("private-input-sentinel", json.dumps(detail))
        with self.assertRaises(ValueError):
            runtime.diagnostic_frame("another-conversation", turn_id, "interruptibility_frame")
        self.assertNotIn("frame_projection", json.dumps(runtime.diagnostic_turns("local-sandbox")))

    def test_historical_frame_is_labelled_as_delivery_not_generation(self):
        from tests.test_conversation_runtime import ConversationRuntimeTests
        helper = ConversationRuntimeTests()
        helper.setUp()
        self.addCleanup(helper.tearDown)
        runtime = helper.runtime()
        self.addCleanup(runtime.close)
        context, _ = runtime.journal.create_turn(conversation_id="historical", request_id="old", user_input="private")
        runtime.journal.put_artifact(context.turn_id, "wave_b", {"context": {"persona_frame": {"growth_paused": True}}, "token_estimate": 7}, schema_version="PackedConversationContext.v1")
        detail = runtime.diagnostic_frame("historical", context.turn_id, "persona_frame")
        self.assertEqual(detail["projection"]["source"], "delivery_snapshot")
        self.assertIsNone(detail["projection"]["captured_at_ms"])
