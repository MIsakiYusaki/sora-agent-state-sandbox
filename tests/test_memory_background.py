from __future__ import annotations

import copy
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.conversation_runtime.post_turn import MemoryPostTurnConsumer
from sora_sandbox.conversation_runtime.post_turn import PostTurnDispatcher
from sora_sandbox.conversation_runtime.journal import ConversationJournal
from sora_sandbox.conversation_runtime.routing import ModelRouteCoordinator, RoutedLLMClient
from sora_sandbox.conversation_runtime.runtime import ConversationRuntime, ConversationRuntimeConfig
from sora_sandbox.conversation_runtime.security import AtRestCipher, StaticKeyProvider
from sora_sandbox.agent import AgentRunner, build_default_subsystem_registry
from sora_sandbox.interruptibility import InterruptibilityService
from sora_sandbox.memory_embedding import DeterministicEmbeddingProvider
from sora_sandbox.memory_extraction_delivery import ExtractionDelivery, ExtractionLeaseLost
from sora_sandbox.memory_extraction_scheduler import MemoryExtractionScheduler
from sora_sandbox.memory_extractor_worker import AsyncMemoryExtractorWorker
from sora_sandbox.memory_recall_planner import HybridRecallPlanner
from sora_sandbox.memory_reranker import FeatureMemoryReranker
from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.memory_surface_policy import DeterministicSurfacePolicyProvider, SurfacePolicyEngine
from sora_sandbox.memory_system import MemorySystem
from sora_sandbox.llm import FakeLLMClient
from sora_sandbox.runtime_stack import SoraRuntimeStack
from sora_sandbox.state import seed_state


TEXT = "I prefer quiet weekend walks by the river. They help me relax after a busy week and I plan to keep this routine."


class ControlledExtractor:
    """A test-only provider with controllable delay and deterministic schema output."""

    def __init__(self, *, block: bool = False) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = block
        self.inputs = []

    def extract(self, package):
        self.calls += 1
        self.inputs.append(copy.deepcopy(package))
        self.started.set()
        if self.block and not self.release.wait(10):
            raise TimeoutError("test provider not released")
        return self.result(package)

    @staticmethod
    def result(package):
        source = next(event for event in package["events"] if event["actor"] == "user")
        return {
            "schema_version": "extractor_result.v1", "job_id": package["job"]["job_id"],
            "episode_id": package["episode"]["episode_id"], "model": {"provider": "test", "model": "controlled"},
            "candidates": [{"operation": "ADD", "memory_type": "preference",
                            "summary": "User prefers quiet weekend walks by the river to relax.",
                            "confidence": 0.99, "importance": 0.8, "stability": 0.8,
                            "specificity": 0.9, "novelty": 1.0, "conflict_score": 0,
                            "sensitivity": "low", "reason": "Explicitly stated routine",
                            "evidence_refs": [{"event_id": source["event_id"], "speaker": "user",
                                               "episode_id": package["episode"]["episode_id"]}]}],
        }


class MemoryBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.make_runtime(self.root / "memory.db")

    def make_runtime(self, path):
        runtime = MemoryRuntime(
            path, owner_user_id="user-a", agent_id="sora-a",
            embedding_provider=DeterministicEmbeddingProvider(),
            recall_planner=HybridRecallPlanner(enable_llm=False), reranker=FeatureMemoryReranker(),
            surface_policy=SurfacePolicyEngine(provider=DeterministicSurfacePolicyProvider()),
        )
        self.addCleanup(runtime.close)
        return runtime

    def event(self, *, source="event-1", conversation="conversation-1", turn="runtime-turn-1", text=TEXT):
        return {"event_id": source, "event_type": "conversation.turn_committed", "turn_id": turn,
                "payload": {"schema_version": "TurnCommittedEvent.v1", "runtime_turn_id": turn,
                            "turn_id": "1", "conversation_id": conversation, "user_id": "user-a",
                            "correlation_id": "correlation-1", "user_input": text,
                            "reply": "That sounds peaceful.", "response_plan": {}}}

    def counts(self):
        with self.runtime._lock:
            return {table: self.runtime._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("memory_event", "memory_episode", "extraction_job", "evidence",
                                  "memory_candidate", "memory_record", "memory_revision", "memory_turn_receipt")}

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("condition did not become true")

    def scheduler(self, provider=None):
        self.runtime.extractor_provider = provider
        scheduler = MemoryExtractionScheduler(self.runtime, poll_seconds=0.02)
        self.addCleanup(scheduler.stop)
        return scheduler

    def test_committed_consumer_preserves_identity_and_replay_is_noop(self):
        consumer = MemoryPostTurnConsumer(self.runtime)
        event = self.event(text="remember that " + TEXT)
        first = consumer.handle(event)
        counts = self.counts()
        self.assertEqual(first, consumer.handle(event))
        self.assertEqual(counts, self.counts())
        self.assertEqual(counts["memory_event"], 2)
        trace = self.runtime.get_episode_trace(first["episode_id"])
        self.assertEqual(trace["episode"]["session_id"], "conversation-1")
        for item in trace["events"]:
            self.assertEqual(item["turn_id"], "runtime-turn-1")
            self.assertEqual(item["raw_ref"], "event-1")
        package = self.runtime.build_extraction_input(first["job_id"])
        self.assertEqual(package["source_identity"]["correlation_id"], "correlation-1")
        self.assertNotIn("lease_token", package["job"])

    def test_identical_text_in_distinct_turns_and_conversations_is_not_deduplicated(self):
        self.runtime.observe_committed_turn(self.event())
        self.runtime.observe_committed_turn(self.event(source="event-2", conversation="conversation-2"))
        self.runtime.observe_committed_turn(self.event(source="event-3", turn="runtime-turn-2"))
        self.assertEqual(self.counts()["memory_episode"], 3)
        self.assertEqual(self.counts()["memory_event"], 6)

    def test_identity_or_payload_conflict_rejected_without_side_effects(self):
        event = self.event()
        self.runtime.observe_committed_turn(event)
        before = self.counts()
        for key, value in (("user_input", "changed content"), ("user_id", "different-user"),
                           ("agent_id", "different-sora"), ("runtime_turn_id", "different-turn"),
                           ("conversation_id", "different-conversation")):
            changed = copy.deepcopy(event)
            changed["payload"][key] = value
            with self.assertRaises(ValueError):
                self.runtime.observe_committed_turn(changed)
            self.assertEqual(before, self.counts())

    def test_episode_evidence_candidates_and_receipt_rollback_together(self):
        before = self.counts()
        with patch.object(self.runtime, "observe_response_plan", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.runtime.observe_committed_turn(self.event(text="remember that " + TEXT))
        self.assertEqual(before, self.counts())
        self.runtime.observe_committed_turn(self.event(text="remember that " + TEXT))
        self.assertEqual(self.counts()["memory_episode"], 1)

    def test_crash_after_gate_commit_replays_without_new_revision(self):
        original = self.runtime._conn.after_commit
        def crash():
            original()
            # Gate helpers now share an outer transaction. Inject after the
            # actual durable boundary, not after a nested helper returns.
            if self.runtime._conn.execute("SELECT count(*) FROM memory_record").fetchone()[0]:
                raise RuntimeError("crash after gate")
        event = self.event(text="remember that " + TEXT)
        with patch.object(self.runtime._conn, "after_commit", side_effect=crash):
            with self.assertRaises(RuntimeError):
                self.runtime.observe_committed_turn(event)
        before = self.counts()
        self.runtime.observe_committed_turn(event)
        self.assertEqual(before, self.counts())

    def test_concurrent_connections_ingest_once(self):
        other = self.make_runtime(self.runtime.path)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(runtime.observe_committed_turn, self.event(text="remember that " + TEXT))
                       for runtime in (self.runtime, other)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.counts()["memory_episode"], 1)
        self.assertEqual(self.counts()["memory_turn_receipt"], 1)

    def test_automatic_extraction_materializes_without_manual_run(self):
        provider = ControlledExtractor()
        scheduler = self.scheduler(provider)
        scheduler.start()
        result = self.runtime.observe_committed_turn(self.event())
        self.wait_for(lambda: self.runtime.ingestion.get_extraction_job(result["job_id"])["status"] == "completed")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(provider.inputs[0]["source_identity"]["turn_id"], "runtime-turn-1")
        self.assertTrue(any(e["source_type"] == "llm_extractor" for e in
                            self.runtime.get_episode_trace(result["episode_id"])["evidence"]))
        scheduler.stop()
        counts = self.counts()
        self.runtime.observe_committed_turn(self.event())
        self.assertEqual(counts, self.counts())
        self.assertEqual(self.runtime._conn.execute("SELECT COUNT(*) FROM memory_extraction_checkpoint").fetchone()[0], 0)

    def test_absent_route_waits_without_attempts_and_late_binding_resumes(self):
        scheduler = self.scheduler()
        binding = {"ready": False}
        self.runtime.model_provider_system = SimpleNamespace(
            router=SimpleNamespace(has_binding=lambda _: binding["ready"], resolve=lambda _: object()), llm=object(),
        )
        provider = ControlledExtractor()
        with patch("sora_sandbox.memory_extraction_scheduler.RoutedMemoryExtractorProvider", return_value=provider):
            result = self.runtime.observe_committed_turn(self.event())
            scheduler.start()
            self.wait_for(lambda: scheduler.snapshot()["status"] == "waiting_provider")
            self.assertEqual(self.runtime.ingestion.get_extraction_job(result["job_id"])["attempts"], 0)
            binding["ready"] = True
            scheduler.wake()
            self.wait_for(lambda: provider.calls == 1)
            scheduler.stop()

    def test_restart_recovers_expired_claim_and_fences_old_worker(self):
        result = self.runtime.observe_committed_turn(self.event())
        job = self.runtime.claim_next_extraction_job()
        with self.runtime._lock:
            self.runtime._conn.execute("UPDATE extraction_job SET lease_expires_at = '2000-01-01' WHERE id = ?",
                                       (job["job_id"],))
            self.runtime._conn.commit()
        other = self.make_runtime(self.runtime.path)
        new_job = other.claim_next_extraction_job()
        self.assertEqual(new_job["job_id"], result["job_id"])
        self.assertEqual(new_job["attempts"], 2)
        self.assertNotEqual(job["lease_token"], new_job["lease_token"])
        with self.assertRaises(ExtractionLeaseLost):
            ExtractionDelivery(self.runtime, job).complete("stale completion")

    def test_simultaneous_workers_claim_only_once(self):
        self.runtime.observe_committed_turn(self.event())
        other = self.make_runtime(self.runtime.path)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result(timeout=5) for future in
                       [pool.submit(runtime.claim_next_extraction_job) for runtime in (self.runtime, other)]]
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_slow_model_does_not_hold_memory_lock_and_shutdown_retains_store(self):
        provider = ControlledExtractor(block=True)
        scheduler = self.scheduler(provider)
        self.runtime.observe_committed_turn(self.event())
        scheduler.start()
        self.assertTrue(provider.started.wait(5))
        try:
            started = time.monotonic()
            self.runtime.observe_committed_turn(self.event(source="event-2", turn="runtime-turn-2"))
            self.assertLess(time.monotonic() - started, 1)
            with self.assertRaises(TimeoutError):
                scheduler.stop(timeout=0.01)
            self.assertEqual(self.counts()["memory_episode"], 2)
        finally:
            provider.release.set()
            scheduler.stop()

    def test_lease_heartbeat_protects_long_call_against_second_worker(self):
        provider = ControlledExtractor(block=True)
        scheduler = self.scheduler(provider)
        scheduler.lease_seconds = 1
        self.runtime.observe_committed_turn(self.event())
        other = self.make_runtime(self.runtime.path)
        scheduler.start()
        self.assertTrue(provider.started.wait(5))
        try:
            time.sleep(1.2)
            self.assertIsNone(other.claim_next_extraction_job())
        finally:
            provider.release.set()
            scheduler.stop()

    def test_cancellation_while_llm_running_prevents_materialization(self):
        provider = ControlledExtractor(block=True)
        scheduler = self.scheduler(provider)
        result = self.runtime.observe_committed_turn(self.event())
        scheduler.start()
        self.assertTrue(provider.started.wait(5))
        with self.runtime._lock:
            self.runtime.ingestion.cancel_extraction_jobs_for_episode(result["episode_id"], reason="user_forget")
            self.runtime._conn.commit()
        before = self.counts()
        provider.release.set()
        scheduler.stop()
        self.assertEqual(before, self.counts())
        self.assertEqual(self.runtime.ingestion.get_extraction_job(result["job_id"])["status"], "cancelled")

    def test_deleted_source_replay_cannot_resurrect_episode(self):
        event = self.event()
        result = self.runtime.observe_committed_turn(event)
        for item in self.runtime.list_events(episode_id=result["episode_id"]):
            self.runtime.propagate_event_delete(item["event_id"])
        before = self.counts()
        self.runtime.observe_committed_turn(event)
        self.assertEqual(before, self.counts())
        self.assertEqual(self.runtime.ingestion.get_extraction_job(result["job_id"])["status"], "cancelled")

    def test_checkpoint_retries_do_not_repeat_llm_or_duplicate_commits(self):
        provider = ControlledExtractor()
        self.runtime.observe_committed_turn(self.event())
        with patch.object(ExtractionDelivery, "complete", side_effect=RuntimeError("crash before completion")):
            result = AsyncMemoryExtractorWorker(self.runtime, provider=provider).run_once()
        self.assertEqual(result["status"], "retry")
        counts = self.counts()
        self.runtime._conn.execute("UPDATE extraction_job SET available_at = '2000-01-01' WHERE status = 'pending'")
        self.runtime._conn.commit()
        result = AsyncMemoryExtractorWorker(self.runtime, provider=provider).run_once()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(counts, self.counts())

    def test_failures_back_off_and_terminal_errors_do_not_kill_scheduler(self):
        provider = ControlledExtractor()
        provider.extract = lambda _: (_ for _ in ()).throw(RuntimeError("secret-provider-response"))
        scheduler = self.scheduler(provider)
        result = self.runtime.observe_committed_turn(self.event())
        scheduler.start()
        self.wait_for(lambda: self.runtime.ingestion.get_extraction_job(result["job_id"])["error"] is not None)
        time.sleep(0.1)
        scheduler.stop()
        job = self.runtime.ingestion.get_extraction_job(result["job_id"])
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(job["status"], "pending")
        self.assertNotIn("secret-provider-response", job["error"])

    def test_default_stack_starts_worker_and_disabled_mode_does_not(self):
        for enabled in (True, False):
            with patch.dict(os.environ, {"SORA_SANDBOX_STATE_DIR": str(self.root / f"stack-{enabled}")}), \
                    patch.object(MemoryExtractionScheduler, "provider", return_value=None):
                stack = SoraRuntimeStack.build(FakeLLMClient(), start_workers=enabled)
                try:
                    self.assertEqual(stack.memory.extraction_scheduler.snapshot()["running"], enabled)
                finally:
                    stack.close()

    def test_committed_runtime_turn_reaches_scheduler_with_real_ids(self):
        journal = ConversationJournal(self.root / "conversation.db", AtRestCipher(StaticKeyProvider(b"k" * 32)))
        routes = ModelRouteCoordinator(max_hot_path_calls=3)
        llm = RoutedLLMClient(FakeLLMClient(), routes, journal=journal)
        system = MemorySystem(self.runtime)
        self.runtime.extractor_provider = ControlledExtractor()
        self.addCleanup(system.stop)
        dispatcher = PostTurnDispatcher(journal)
        dispatcher.register(MemoryPostTurnConsumer(system))
        runner = AgentRunner(llm, subsystem_registry=build_default_subsystem_registry(persistent_executor=True),
                             memory_runtime=system, interruptibility=InterruptibilityService())
        conversation = ConversationRuntime(runner, journal=journal, model_routes=routes,
                                           post_turn=dispatcher, config=ConversationRuntimeConfig())
        state = seed_state()
        state["session"]["user_id"] = "user-a"
        system.start()
        try:
            events = list(conversation.stream_turn(state, TEXT, request_id="real-request-id",
                                                   conversation_id="real-conversation-id", correlation_id="real-correlation-id"))
            self.assertEqual(events[-1].event_type, "response.completed")
            self.wait_for(lambda: bool(self.runtime.list_extraction_jobs(status="completed")))
            episode = self.runtime.list_episodes()[0]
            self.assertEqual(episode["session_id"], "real-conversation-id")
            source = self.runtime.list_events(episode_id=episode["episode_id"])[0]
            self.assertEqual(source["turn_id"], events[0].turn_id)
            package = self.runtime.extractor_provider.inputs[0]
            self.assertEqual(package["source_identity"]["correlation_id"], "real-correlation-id")
            self.assertEqual(package["source_identity"]["source_event_id"], source["raw_ref"])
        finally:
            conversation.close()
            system.stop()

    def test_unfinished_ingestion_is_not_claimed_before_gate_recovery(self):
        with patch.object(self.runtime, "run_candidate_gate", side_effect=RuntimeError("crash before gate")):
            with self.assertRaises(RuntimeError):
                self.runtime.observe_committed_turn(self.event(text="remember that " + TEXT))
        self.assertIsNone(self.runtime.claim_next_extraction_job())
        self.runtime.observe_committed_turn(self.event(text="remember that " + TEXT))
        self.assertIsNotNone(self.runtime.claim_next_extraction_job())

    def test_invalid_extractor_output_is_terminal_and_diagnostics_are_redacted(self):
        result = self.runtime.observe_committed_turn(self.event())
        provider = ControlledExtractor()
        provider.extract = lambda _: {"candidates": [{"operation": "unknown-private-input"}]}
        outcome = AsyncMemoryExtractorWorker(self.runtime, provider=provider).run_once()
        self.assertEqual(outcome["status"], "failed")
        self.assertNotIn("unknown-private-input", outcome["error"])
        self.assertEqual(self.runtime.ingestion.get_extraction_job(result["job_id"])["status"], "failed")

    def test_sensitive_secret_never_enters_memory_queue(self):
        result = self.runtime.observe_committed_turn(self.event(text="my api_key is sk-123456789012345678901234567890"))
        self.assertIsNone(result["job_id"])
        self.assertEqual(self.counts()["memory_event"], 0)
        self.assertEqual(self.counts()["evidence"], 0)

    def test_gate_crash_before_candidate_status_does_not_leave_partial_canonical_memory(self):
        original = self.runtime._publish_memory_change
        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("crash inside canonical creation")
        event = self.event(text="remember that " + TEXT)
        with patch.object(self.runtime, "_publish_memory_change", side_effect=crash):
            with self.assertRaises(RuntimeError):
                self.runtime.observe_committed_turn(event)
        self.assertEqual(self.counts()["memory_record"], 0)
        self.assertEqual(self.counts()["memory_revision"], 0)
        self.runtime.observe_committed_turn(event)
        self.assertEqual(self.counts()["memory_record"], 1)

    def test_runtime_payload_uses_bound_owner_when_legacy_session_has_no_user(self):
        runtime = SimpleNamespace(runner=SimpleNamespace(memory_runtime=self.runtime))
        payload = ConversationRuntime._outbox_payload(
            runtime, {}, context=SimpleNamespace(turn_id="rt", conversation_id="c", correlation_id="corr"),
            working_state={"session": {"turn_id": 1}}, user_input="hello", reply="hi",
        )
        self.assertEqual(payload["user_id"], "user-a")
        self.assertEqual(payload["agent_id"], "sora-a")

    def test_restart_drains_pending_queue_without_new_turn(self):
        result = self.runtime.observe_committed_turn(self.event())
        reopened = self.make_runtime(self.runtime.path)
        reopened.extractor_provider = ControlledExtractor()
        scheduler = MemoryExtractionScheduler(reopened, poll_seconds=0.02)
        try:
            scheduler.start()
            self.wait_for(lambda: reopened.ingestion.get_extraction_job(result["job_id"])["status"] == "completed")
            self.assertEqual(reopened.extractor_provider.calls, 1)
        finally:
            scheduler.stop()


if __name__ == "__main__":
    unittest.main()
