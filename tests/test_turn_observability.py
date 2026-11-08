from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sora_sandbox.agent import AgentRunner, build_default_subsystem_registry
from sora_sandbox.conversation_runtime.contracts import TurnCancelled
from sora_sandbox.conversation_runtime.journal import ConversationJournal
from sora_sandbox.conversation_runtime.post_turn import FunctionPostTurnConsumer, PostTurnDispatcher
from sora_sandbox.conversation_runtime.routing import ModelRouteCoordinator, RoutedLLMClient
from sora_sandbox.conversation_runtime.runtime import ConversationRuntime, ConversationRuntimeConfig
from sora_sandbox.conversation_runtime.security import AtRestCipher, StaticKeyProvider
from sora_sandbox.interruptibility import InterruptibilityService
from sora_sandbox.llm import FakeLLMClient
from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.state import seed_state
from sora_sandbox.turn_observability import TurnEvidence, legacy_evidence, observe_call


class TurnEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        journal = ConversationJournal(self.root / "turns.db", AtRestCipher(StaticKeyProvider(b"t" * 32)))
        routes = ModelRouteCoordinator()
        runner = AgentRunner(RoutedLLMClient(FakeLLMClient(), routes, journal=journal),
                             subsystem_registry=build_default_subsystem_registry(persistent_executor=True),
                             interruptibility=InterruptibilityService())
        self.runtime = ConversationRuntime(runner, journal=journal, model_routes=routes,
                                          post_turn=PostTurnDispatcher(journal),
                                          config=ConversationRuntimeConfig(start_post_turn_worker=False))

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def test_nested_spans_and_parallel_signals_survive_commit_without_content(self):
        result = self.runtime.run(seed_state(), "hello private-canary-684", conversation_id="observed")
        diagnostic = self.runtime.diagnostic_turns("observed")[0]
        spans = diagnostic["execution"]["spans"]
        names = {item["name"] for item in spans}
        self.assertTrue({"agent.run", "turn.interpretation", "signals.collect", "arbitration.resolve", "runtime.atomic_commit"} <= names)
        by_id = {item["span_id"]: item for item in spans}
        for span in spans:
            self.assertGreaterEqual(span["duration_ms"], 0)
            if span["name"].startswith("signal."):
                self.assertEqual(by_id[span["parent_id"]]["name"], "signals.collect")
        self.assertNotIn("private-canary-684", json.dumps(diagnostic))
        self.assertEqual(diagnostic["legacy_evidence"]["status"], "available")
        self.assertTrue(diagnostic["legacy_evidence"]["signals"])
        self.assertTrue(diagnostic["legacy_evidence"]["signal_decisions"])
        self.assertNotIn("subsystem_health", result["context_pack"])
        self.assertNotIn("score_breakdown", result["context_pack"]["arbitration_result"])
        self.assertEqual(diagnostic["post_turn"][0]["status"], "pending")
        self.assertEqual(self.runtime.diagnostic_turns("another-conversation"), [])
        self.assertIsNotNone(self.runtime.journal.read_result(result["runtime"]["turn_id"]))

    def test_failure_and_cancellation_preserve_partial_spans(self):
        for exception, expected in [(ValueError("private-error-canary"), "failed"), (TurnCancelled("private-cancel-canary"), "cancelled")]:
            with patch.object(self.runtime.runner, "run", side_effect=exception):
                with self.assertRaises(type(exception)):
                    self.runtime.run(seed_state(), "private-input", conversation_id=expected)
            diagnostic = self.runtime.diagnostic_turns(expected)[0]
            self.assertEqual(diagnostic["status"], expected)
            self.assertEqual(diagnostic["execution"]["spans"][0]["status"], expected)
            self.assertEqual(diagnostic["legacy_evidence"]["status"], "unobserved")
            self.assertEqual(diagnostic["post_turn"], [])
            self.assertNotIn("private-", json.dumps(diagnostic))

    def test_generation_gate_failure_keeps_observed_signal_decisions(self):
        with patch.object(self.runtime.runner, "context_pack_transformer", side_effect=ValueError("private-gate-error")):
            with self.assertRaises(ValueError):
                self.runtime.run(seed_state(), "hello private-gate-input", conversation_id="gate-failure")
        diagnostic = self.runtime.diagnostic_turns("gate-failure")[0]
        self.assertEqual(diagnostic["status"], "failed")
        self.assertTrue(diagnostic["legacy_evidence"]["signals"])
        self.assertTrue(diagnostic["legacy_evidence"]["signal_decisions"])
        self.assertIsNone(diagnostic["legacy_evidence"]["decisions"]["memory_hook_created"])
        self.assertEqual(diagnostic["post_turn"], [])
        self.assertNotIn("private-", json.dumps(diagnostic))

    def test_pre_generation_capture_is_allowlisted_and_snapshot_is_independent(self):
        evidence = TurnEvidence()
        pack = {"subsystem_health": [{"subsystem": "memory", "status": "ok", "detail": "private-error"}],
                "arbitration_result": {"score_breakdown": [{"source": "memory", "decision": "background", "reason": "private-reason"}]},
                "turn_interpretation": {"input_summary": "private-input"}}
        evidence.capture_decisions(pack)
        pack["subsystem_health"][0]["status"] = "failed"
        snapshot = evidence.snapshot()
        self.assertNotIn("private-", json.dumps(snapshot))
        self.assertEqual(snapshot["pre_generation"]["signals"][0]["status"], "ok")
        snapshot["pre_generation"]["signals"].clear()
        self.assertTrue(evidence.snapshot()["pre_generation"]["signals"])

    def test_active_span_visible_before_runner_returns(self):
        entered, release = threading.Event(), threading.Event()
        original = self.runtime.runner.run
        errors = []
        def wait_run(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test wait")
            return original(*args, **kwargs)
        def run():
            try:
                self.runtime.run(seed_state(), "hello", conversation_id="live")
            except Exception as error:
                errors.append(error)
        with patch.object(self.runtime.runner, "run", side_effect=wait_run):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                spans = self.runtime.diagnostic_turns("live")[0]["execution"]["spans"]
                active = next(item for item in spans if item["name"] == "agent.run")
                self.assertEqual(active["status"], "running")
                self.assertIsNone(active["duration_ms"])
                self.assertIsNone(active["ended_at_ms"])
            finally:
                release.set()
                thread.join(5)
        self.assertEqual(errors, [])

    def test_async_failures_and_receipts_are_separate_and_redacted(self):
        def fail(event):
            raise ValueError("private-consumer-error")
        self.runtime.post_turn.register(FunctionPostTurnConsumer("memory", fail))
        self.runtime.post_turn.register(FunctionPostTurnConsumer("affect", lambda event: {"secret": "private-consumer-result"}))
        self.runtime.run(seed_state(), "hello", conversation_id="async")
        self.runtime.post_turn.process_once()
        diagnostic = self.runtime.diagnostic_turns("async")[0]
        self.assertEqual(diagnostic["status"], "committed")
        self.assertEqual(len(diagnostic["async_executions"]), 2)
        self.assertEqual({item["spans"][0]["status"] for item in diagnostic["async_executions"]}, {"failed", "completed"})
        self.assertNotIn("private-", json.dumps(diagnostic))
        self.assertEqual(diagnostic["post_turn"][0]["attempts"], 1)

    def test_recall_internal_spans_are_real_calls(self):
        memory = MemoryRuntime(self.root / "memory.db")
        try:
            evidence = TurnEvidence()
            with evidence.activate():
                observe_call("memory.recall", memory.recall, "我以前说过喜欢什么", top_k=3)
            names = {span["name"] for span in evidence.snapshot()["spans"]}
            self.assertIn("recall.plan", names)
            self.assertIn("recall.retrieve", names)
            self.assertIn("recall.surface", names)
            self.assertIn("recall.persist_trace", names)
        finally:
            memory.close()

    def test_duration_is_monotonic_and_collection_is_bounded(self):
        evidence = TurnEvidence(limit=2)
        with patch("sora_sandbox.turn_observability.time.time_ns", side_effect=[20_000_000, 10_000_000]):
            with evidence.activate():
                observe_call("test", lambda: None)
        span = evidence.snapshot()["spans"][0]
        self.assertLess(span["ended_at_ms"], span["started_at_ms"])
        self.assertGreaterEqual(span["duration_ms"], 0)
        with evidence.activate():
            observe_call("test2", lambda: None)
            observe_call("test3", lambda: None)
        self.assertEqual(len(evidence.snapshot()["spans"]), 2)
        self.assertEqual(evidence.snapshot()["dropped_count"], 1)

    def test_legacy_allowlist_preserves_unknown_and_zero_without_payloads(self):
        result = {"timing": {"memory_recall": 0, "response_planner": None, "secret": 42},
                  "trace": [{"kind": "user.input.received", "at": 123, "payload": {"text": "private-trace"}}],
                  "context_pack": {"turn_interpretation": {"mode": "private-mode"},
                                   "subsystem_health": [{"subsystem": "memory", "status": "degraded", "detail": "private-error"}]}}
        projected = legacy_evidence(result)
        self.assertNotIn("private", json.dumps(projected))
        self.assertEqual(projected["durations"][0]["duration_ms"], 0)
        self.assertIsNone(projected["durations"][1]["duration_ms"])
        self.assertNotIn("start_at_ms", json.dumps(projected))

    def test_final_snapshot_marks_unclosed_work_without_mutating_live_collector(self):
        evidence = TurnEvidence()
        with evidence.activate(), evidence.span("signal.memory"):
            final = evidence.snapshot(final=True)
            self.assertEqual(final["snapshot_state"], "final")
            self.assertEqual(final["spans"][0]["status"], "end_unobserved")
            self.assertIsNone(final["spans"][0]["duration_ms"])
            self.assertEqual(evidence.snapshot()["spans"][0]["status"], "running")
        self.assertEqual(evidence.snapshot(final=True)["spans"][0]["status"], "completed")

    def test_current_decision_enums_and_candidate_booleans_remain_visible(self):
        projected = legacy_evidence({"context_pack": {
            "turn_interpretation": {"mode": "deterministic_fast_path"},
            "recollection_frame": {"surface_action": "gist"},
            "arbitration_result": {"semantic_judgment": {"mode": "llm_degraded"},
                                   "response_directives": {"worldops_expression": "active"}}},
            "state_diff": {"growth_event_created": False, "worldops_event_expressed": True,
                           "memory_hook_created": "private-content"}})["decisions"]
        self.assertEqual(projected["interpretation_mode"], "deterministic_fast_path")
        self.assertEqual(projected["memory_surface"], "gist")
        self.assertEqual(projected["semantic_judgment_mode"], "llm_degraded")
        self.assertEqual(projected["worldops_expression"], "active")
        self.assertIs(projected["growth_event_created"], False)
        self.assertIs(projected["worldops_event_expressed"], True)
        self.assertIsNone(projected["memory_hook_created"])

    def test_signal_decisions_expose_only_allowlisted_rules_not_model_reasons(self):
        projected = legacy_evidence({"context_pack": {"arbitration_result": {"score_breakdown": [
            {"source": "worldops", "id": "private-id", "decision": "suppressed",
             "reason": "本轮可以回答 Sora 世界设定，但未明确命中当前 WorldOps 事件，事件保持背景。"},
            {"source": "memory", "decision": "background", "reason": "private-model-explanation"},
            {"source": "private-source", "decision": "selected"},
        ]}}})
        self.assertEqual(projected["signal_decisions"], [
            {"source": "worldops", "decision": "suppressed", "reason_code": "world_lore_not_event"},
            {"source": "memory", "decision": "background", "reason_code": None},
        ])
        self.assertNotIn("private", json.dumps(projected))


if __name__ == "__main__":
    unittest.main()
