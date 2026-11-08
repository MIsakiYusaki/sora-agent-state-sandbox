from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_memory_background import ControlledExtractor, TEXT
from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.memory_extractor_worker import AsyncMemoryExtractorWorker
from sora_sandbox.memory_job_observability import MemoryJobObserver, memory_job_observer
from sora_sandbox.conversation_gateway.debug_snapshot import _attach_memory_jobs


class MemoryJobObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = MemoryRuntime(Path(self.temp.name) / "memory.db", owner_user_id="owner", agent_id="agent")
        self.addCleanup(self.runtime.close)
        self.observer = memory_job_observer(self.runtime)

    def enqueue(self, turn="turn", conversation="conversation"):
        return self.runtime.observe_committed_turn({
            "event_id": "event-" + turn, "turn_id": turn, "event_type": "conversation.turn_committed",
            "payload": {"runtime_turn_id": turn, "conversation_id": conversation,
                        "user_id": "owner", "agent_id": "agent", "correlation_id": "corr-" + turn,
                        "user_input": TEXT + " private-content-canary", "reply": "That sounds peaceful."},
        })

    def snapshot(self):
        return self.observer.snapshot("conversation", ["turn"])

    def test_queued_completed_and_scoped_attempt_tree(self):
        self.enqueue()
        queued = self.snapshot()["jobs"][0]
        self.assertEqual(queued["wait_reason"], "queued")
        self.assertEqual(queued["attempt_count"], 0)
        result = AsyncMemoryExtractorWorker(self.runtime, provider=ControlledExtractor()).run_once()
        self.assertEqual(result["status"], "completed")
        job = self.snapshot()["jobs"][0]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["attempts"][0]["outcome"], "completed")
        names = {span["name"] for span in job["attempts"][0]["execution"]["spans"]}
        self.assertTrue({"memory.extractor.provider", "memory.extractor.validate", "memory.extractor.materialize_and_gate"} <= names)
        self.assertNotIn("private-content-canary", json.dumps(job))
        self.assertEqual(self.observer.snapshot("other", ["turn"])["jobs"], [])
        self.assertEqual(self.observer.snapshot("conversation", ["other"])["jobs"], [])
        self.runtime.owner_user_id = "other-owner"
        self.assertEqual(self.snapshot()["jobs"], [])

    def test_retry_retains_failed_phase_and_future_availability(self):
        self.enqueue()
        class Failure:
            def extract(self, package):
                raise RuntimeError("private-provider-error")
        result = AsyncMemoryExtractorWorker(self.runtime, provider=Failure()).run_once()
        self.assertEqual(result["status"], "retry")
        job = self.snapshot()["jobs"][0]
        self.assertEqual(job["wait_reason"], "retry_backoff")
        self.assertEqual(job["error_code"], "extractor_error:RuntimeError")
        attempt = job["attempts"][0]
        self.assertEqual(attempt["outcome"], "retry")
        provider = next(s for s in attempt["execution"]["spans"] if s["name"] == "memory.extractor.provider")
        self.assertEqual(provider["status"], "failed")
        self.assertIsNotNone(provider["duration_ms"])
        self.assertNotIn("private-", json.dumps(job))
        self.assertEqual(AsyncMemoryExtractorWorker(self.runtime, provider=Failure()).run_once()["status"], "idle")
        self.assertEqual(self.snapshot()["jobs"][0]["attempt_count"], 1)

    def test_live_attempt_is_visible_before_provider_returns(self):
        self.enqueue()
        provider = ControlledExtractor(block=True)
        worker = AsyncMemoryExtractorWorker(self.runtime, provider=provider)
        thread = threading.Thread(target=worker.run_once)
        thread.start()
        try:
            self.assertTrue(provider.started.wait(5))
            job = self.snapshot()["jobs"][0]
            self.assertEqual(job["status"], "running")
            attempt = job["attempts"][0]
            self.assertEqual(attempt["execution"]["snapshot_state"], "live")
            span = next(s for s in attempt["execution"]["spans"] if s["name"] == "memory.extractor.provider")
            self.assertEqual(span["status"], "running")
            self.assertIsNone(span["duration_ms"])
        finally:
            provider.release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())

    def test_diagnostic_storage_failure_does_not_retry_canonical_work(self):
        self.enqueue()
        with self.runtime._lock:
            self.runtime._conn.execute("DROP TABLE memory_extraction_attempt")
            self.runtime._conn.commit()
        result = AsyncMemoryExtractorWorker(self.runtime, provider=ControlledExtractor()).run_once()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["job"]["attempts"], 1)
        self.assertEqual(self.observer.write_failures, 2)

    def test_unknown_error_text_is_not_exposed_and_gateway_keeps_turn_scope(self):
        result = self.enqueue()
        with self.runtime._lock:
            self.runtime._conn.execute("UPDATE extraction_job SET error=? WHERE id=?", ("private-arbitrary-error", result["job_id"]))
            self.runtime._conn.commit()
        timeline = {"turns": [{"turn_id": "turn", "correlation_id": "corr-turn"},
                              {"turn_id": "other", "correlation_id": "corr-other"}]}
        _attach_memory_jobs(timeline, SimpleNamespace(runtime=self.runtime), "conversation")
        self.assertEqual(len(timeline["turns"][0]["memory_jobs"]["jobs"]), 1)
        self.assertEqual(timeline["turns"][1]["memory_jobs"]["jobs"], [])
        self.assertNotIn("private-", json.dumps(timeline))
        _attach_memory_jobs(timeline, SimpleNamespace(runtime=self.runtime), "wrong-conversation")
        self.assertEqual(timeline["turns"][0]["memory_jobs"]["jobs"], [])

    def test_reset_clears_attempts(self):
        self.enqueue()
        AsyncMemoryExtractorWorker(self.runtime, provider=ControlledExtractor()).run_once()
        self.runtime.reset()
        self.assertEqual(self.snapshot()["jobs"], [])
        self.assertEqual(self.runtime._conn.execute("SELECT COUNT(*) FROM memory_extraction_attempt").fetchone()[0], 0)

    def test_attempt_survives_independent_reader_and_unfinished_is_not_live(self):
        self.enqueue()
        AsyncMemoryExtractorWorker(self.runtime, provider=ControlledExtractor()).run_once()
        reader = MemoryRuntime(Path(self.temp.name) / "memory.db", owner_user_id="owner", agent_id="agent")
        try:
            restored = memory_job_observer(reader).snapshot("conversation", ["turn"])
            self.assertEqual(restored["jobs"][0]["attempts"][0]["outcome"], "completed")
        finally:
            reader.close()
        unfinished = {"snapshot_state": "live", "spans": [{"status": "running", "duration_ms": None}]}
        with self.runtime._lock:
            self.runtime._conn.execute("UPDATE memory_extraction_attempt SET ended_at_ms=NULL, outcome='running', execution_json=?", (json.dumps(unfinished),))
            self.runtime._conn.commit()
        attempt = MemoryJobObserver(self.runtime).snapshot("conversation", ["turn"])["jobs"][0]["attempts"][0]
        self.assertEqual(attempt["outcome"], "end_unobserved")
        self.assertEqual(attempt["execution"]["spans"][0]["status"], "end_unobserved")
        self.assertIsNone(attempt["execution"]["spans"][0]["duration_ms"])

    def test_retention_hides_and_prunes_unfinished_old_attempts(self):
        self.enqueue()
        AsyncMemoryExtractorWorker(self.runtime, provider=ControlledExtractor()).run_once()
        with self.runtime._lock:
            self.runtime._conn.execute("UPDATE memory_extraction_attempt SET started_at_ms=?, ended_at_ms=NULL", (time.time() * 1000 - 8 * 86400_000,))
            self.runtime._conn.commit()
        job = self.snapshot()["jobs"][0]
        self.assertEqual(job["attempts"], [])
        self.assertTrue(job["attempts_truncated"])
        self.enqueue(turn="new-turn")
        AsyncMemoryExtractorWorker(self.runtime, provider=ControlledExtractor()).run_once()
        self.assertEqual(self.runtime._conn.execute("SELECT COUNT(*) FROM memory_extraction_attempt").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
