from __future__ import annotations

import tempfile
import time
import unittest
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.agent import AgentRunner, normalize_turn_interpretation
from sora_sandbox.commands import handle_command
from sora_sandbox.interruptibility import (
    CanonicalControlParser,
    ForbiddenFeatureGate,
    InterruptibilityService,
    enforce_interruptibility_output,
)
from sora_sandbox.interruptibility_contracts import (
    BoundedResolution,
    InterruptibilityFrame,
    ModelAssistTrace,
    ResponseBudgets,
    StateSurfacePermission,
)
from sora_sandbox.interruptibility_eval import run_interruptibility_eval
from sora_sandbox.llm import FakeLLMClient
from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.state import StateStore, ensure_state_migrations, seed_state


def interpretation(**overrides):
    payload = {
        "speech_act": "request",
        "conversation_move": "task_request",
        "intent": {"primary": "ask_for_action", "confidence": 0.95},
        "domain": {"primary": "general", "risk_level": "low"},
    }
    payload.update(overrides)
    return normalize_turn_interpretation(payload)


class _InvalidProvider:
    def resolve(self, request):
        return BoundedResolution(
            request_id=request.request_id,
            decisions={"response_obligation": "forbidden"},
            evidence_refs=(),
            reason_codes=("INVALID",),
            confidence=0.99,
            provider="invalid-test",
        )


class _SlowProvider:
    def resolve(self, request):
        time.sleep(0.02)
        return BoundedResolution(
            request_id=request.request_id,
            decisions={"expansion_permission": "blocked"},
            evidence_refs=(),
            reason_codes=("TIGHTEN",),
            confidence=0.99,
            provider="slow-test",
        )


class _TighteningProvider:
    def resolve(self, request):
        return BoundedResolution(
            request_id=request.request_id,
            decisions={"expansion_permission": "blocked"},
            evidence_refs=(),
            reason_codes=("TIGHTEN",),
            confidence=0.99,
            provider="bounded-test",
        )


class _MustNotRunProvider:
    def resolve(self, request):
        raise AssertionError("explicit controls must not invoke the bounded provider")


class InterruptibilityTests(unittest.TestCase):
    def setUp(self):
        self.state = seed_state()
        self.state["session"]["turn_id"] = 1
        self.service = InterruptibilityService()

    def evaluate(self, text, *, parsed=None, now=1_750_000_000_000, turn_kind="reactive", service=None, domain_features=None):
        return (service or self.service).evaluate(
            self.state,
            user_input=text,
            turn_interpretation=parsed or interpretation(),
            turn_kind=turn_kind,
            correlation_id="corr_test",
            timestamp_ms=now,
            domain_features=domain_features or {},
        )

    def test_frame_contract_is_frozen(self):
        frame = InterruptibilityFrame(
            turn_id="1",
            correlation_id="corr",
            availability="unknown",
            response_obligation="required",
            expansion_permission="limited",
            topic_shift_permission="blocked",
            follow_up_permission="none",
            initiative_permission="blocked",
            state_surface_permission=StateSurfacePermission(),
            response_shape="normal",
            decision_path="deterministic_fast_path",
            model_assist=ModelAssistTrace(),
            budgets=ResponseBudgets(5, 0, 0, 2),
            hard_overrides=(),
            evidence=(),
            reason_codes=(),
            confidence=1.0,
            policy_profile="test",
            policy_version="1",
            trace_id="trace",
        )
        with self.assertRaises(FrozenInstanceError):
            frame.response_shape = "deep"

    def test_forbidden_feature_gate_removes_pressure_features(self):
        clean, filtered = ForbiddenFeatureGate().sanitize(
            {
                "relationship": {
                    "trust": 0.8,
                    "love_score": 0.9,
                    "nested": {"dependency_pressure": 0.7},
                }
            }
        )
        self.assertEqual(clean["relationship"]["trust"], 0.8)
        self.assertNotIn("love_score", clean["relationship"])
        self.assertIn("relationship.love_score", filtered)
        self.assertIn("relationship.nested.dependency_pressure", filtered)

    def test_parser_distinguishes_quiet_from_response_mute(self):
        quiet = CanonicalControlParser().parse("先安静一下，别主动找我")
        mute = CanonicalControlParser().parse("这条不用回复")
        self.assertEqual(quiet[0].control, "quiet")
        self.assertEqual(mute[0].control, "response_mute")
        self.assertEqual(mute[0].scope, "turn")

    def test_response_mute_is_same_turn_and_leak_free(self):
        frame = self.evaluate("这条不用回复")
        self.assertEqual(frame["response_obligation"], "forbidden")
        self.assertEqual(frame["budgets"]["max_sentences"], 0)
        self.assertIn("EXPLICIT_RESPONSE_MUTE", frame["reason_codes"])

    def test_quiet_blocks_initiative_but_not_reactive_reply(self):
        frame = self.evaluate("先安静一下，别主动找我")
        self.assertEqual(frame["response_obligation"], "required")
        self.assertEqual(frame["initiative_permission"], "blocked")

    def test_persistent_mute_requires_confirmation_and_resume_is_reachable(self):
        pending = self.evaluate("以后都不要回复我")
        self.assertEqual(pending["response_obligation"], "required")
        self.assertIn("pending_confirmation", [item["status"] for item in pending["control_receipts"]])
        token = pending["control_receipts"][0]["confirmation_token"]
        self.assertNotIn(token, str(self.state["interruptibility"]["traces"][-1]))

        self.state["session"]["turn_id"] += 1
        confirmed = self.evaluate("确认设置")
        self.assertEqual(confirmed["response_obligation"], "required")

        self.state["session"]["turn_id"] += 1
        muted = self.evaluate("你在吗")
        self.assertEqual(muted["response_obligation"], "forbidden")

        self.state["session"]["turn_id"] += 1
        resumed = self.evaluate("可以说话了")
        self.assertEqual(resumed["response_obligation"], "required")

    def test_activity_lease_renews_only_from_user_activity_and_expires(self):
        start = 1_750_000_000_000
        invited = self.evaluate("陪我聊会儿", now=start)
        self.assertEqual(invited["initiative_permission"], "session_only")

        self.state["session"]["turn_id"] += 1
        proactive = self.evaluate("", now=start + 10 * 60 * 1000, turn_kind="proactive")
        self.assertEqual(proactive["initiative_permission"], "session_only")

        self.state["session"]["turn_id"] += 1
        expired = self.evaluate("", now=start + 16 * 60 * 1000, turn_kind="proactive")
        self.assertEqual(expired["initiative_permission"], "blocked")

    def test_model_cannot_return_unrequested_permission_dimension(self):
        parsed = interpretation(
            confidence=0.5,
            interruptibility={
                "ambiguous_boundary": True,
                "unresolved_dimensions": ["expansion_permission"],
            }
        )
        frame = self.evaluate("有点复杂", parsed=parsed, service=InterruptibilityService(provider=_InvalidProvider()))
        self.assertTrue(frame["model_assist"]["fallback_used"])
        self.assertEqual(frame["response_obligation"], "required")
        self.assertEqual(frame["model_assist"]["fallback_reason"], "provider_returned_unrequested_dimension")

    def test_late_provider_result_is_discarded(self):
        parsed = interpretation(
            confidence=0.5,
            interruptibility={
                "ambiguous_boundary": True,
                "unresolved_dimensions": ["expansion_permission"],
            }
        )
        service = InterruptibilityService(provider=_SlowProvider(), provider_deadline_ms=1)
        frame = self.evaluate("有点复杂", parsed=parsed, service=service)
        self.assertTrue(frame["model_assist"]["fallback_used"])
        self.assertEqual(frame["model_assist"]["fallback_reason"], "provider_deadline_exceeded")

    def test_valid_provider_can_only_tighten_within_ceiling(self):
        parsed = interpretation(
            confidence=0.5,
            interruptibility={
                "ambiguous_boundary": True,
                "unresolved_dimensions": ["expansion_permission"],
            }
        )
        frame = self.evaluate("有点复杂", parsed=parsed, service=InterruptibilityService(provider=_TighteningProvider()))
        self.assertEqual(frame["expansion_permission"], "blocked")
        self.assertEqual(frame["model_assist"]["accepted_dimensions"], ["expansion_permission"])

    def test_explicit_control_resolves_model_dimensions_without_provider(self):
        parsed = interpretation(
            interruptibility={
                "ambiguous_boundary": True,
                "unresolved_dimensions": ["expansion_permission", "response_shape"],
            }
        )
        frame = self.evaluate(
            "简短点，只说结论",
            parsed=parsed,
            service=InterruptibilityService(provider=_MustNotRunProvider()),
        )
        self.assertFalse(frame["model_assist"]["invoked"])
        self.assertEqual(frame["response_shape"], "brief")

    def test_llm_hard_control_without_text_evidence_is_rejected(self):
        parsed = interpretation(
            explicit_commands=[
                {"control": "response_mute", "value": True, "scope": "session", "confidence": 0.99}
            ]
        )
        frame = self.evaluate("请继续解释", parsed=parsed)
        self.assertEqual(frame["response_obligation"], "required")

    def test_trace_is_redacted_and_enforcement_is_auditable(self):
        raw = "这是不应该出现在 trace 里的原文"
        frame = self.evaluate(raw)
        trace = self.state["interruptibility"]["traces"][-1]
        self.assertNotIn(raw, str(trace))
        enforcement = self.service.record_enforcement(
            self.state,
            trace_id=frame["trace_id"],
            emitted=True,
            output_text="收到。",
            response_plan={"interruptibility_compliance": {"question_trimmed": False}},
            route="text",
        )
        self.assertTrue(enforcement["compliant"])

    def test_required_empty_output_is_an_enforcement_violation(self):
        frame = self.evaluate("请回答我")
        enforcement = self.service.record_enforcement(
            self.state,
            trace_id=frame["trace_id"],
            emitted=True,
            output_text="",
            response_plan={},
            route="text",
        )
        self.assertFalse(enforcement["compliant"])
        self.assertIn("required_response_missing", enforcement["violations"])

    def test_output_guard_enforces_question_and_sentence_budgets(self):
        frame = self.evaluate("简短点，别问我")
        output, compliance = enforce_interruptibility_output(
            "第一句。第二句。第三句？第四句？",
            frame,
        )
        self.assertLessEqual(output.count("？") + output.count("?"), 0)
        self.assertLessEqual(compliance["sentence_count"], 2)

    def test_agent_skips_response_planner_for_muted_turn(self):
        result = AgentRunner(
            FakeLLMClient(),
            interruptibility=InterruptibilityService(),
        ).run(self.state, "这条不用回复")
        self.assertFalse(result["emitted"])
        self.assertEqual(result["reply"], "")
        self.assertIsNone(result["response_plan"])
        self.assertEqual(result["expression_event"]["route"], "silent")
        self.assertTrue(self.state["interruptibility"]["last_enforcement"]["compliant"])

    def test_console_controls_support_status_set_and_revoke(self):
        output, should_exit = handle_command(
            "/interruptibility set follow_up none --scope session",
            self.state,
            interruptibility_admin=self.service,
        )
        self.assertFalse(should_exit)
        self.assertIn('"follow_up"', output)
        output, _ = handle_command(
            "/interruptibility revoke follow_up",
            self.state,
            interruptibility_admin=self.service,
        )
        self.assertIn('"revoked_count"', output)

    def test_state_migration_and_persistence(self):
        state = seed_state()
        state.pop("interruptibility")
        self.assertTrue(ensure_state_migrations(state))
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            store.save(state)
            loaded = store.load()
        self.assertEqual(loaded["interruptibility"]["schema_version"], 1)

    def test_golden_set_release_gate_passes(self):
        result = run_interruptibility_eval()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["failed_cases"], 0)
        self.assertTrue(all(result["summary"]["release_gate"].values()))

    def test_module10_registry_release_scope_and_export_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MemoryRuntime(Path(tmp) / "memory.db")
            try:
                runtime.register_quality_eval_suite(
                    "interruptibility",
                    run_interruptibility_eval,
                    source_module="interruptibility_system",
                )
                runtime.register_quality_export_section(
                    "interruptibility",
                    lambda: self.service.export_traces(self.state, limit=10),
                )
                run = runtime.run_quality_eval(suites=["interruptibility"])
                report = runtime.export_quality_report(run_id=run["id"])
                gate = runtime.run_release_gate(change_scope=["interruptibility"])
            finally:
                runtime.close()
        self.assertEqual(run["status"], "passed")
        self.assertIn("interruptibility", report["extensions"])
        self.assertEqual(gate["suites"], ["interruptibility"])


if __name__ == "__main__":
    unittest.main()
