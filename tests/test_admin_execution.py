from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.memory_quality_ops import AdminActionSpec


class AdminExecutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = MemoryRuntime(Path(self.directory.name) / "memory.db")
        self.addCleanup(self.runtime.close)
        self.ops = self.runtime.quality_ops

    def test_concurrent_execution_claims_preview_once(self):
        started, release = threading.Event(), threading.Event()
        calls = []

        def execute(params):
            calls.append(params)
            if len(calls) == 1:
                started.set()
                self.assertTrue(release.wait(3))
            return {"processed": 1}

        self.ops.register_admin_action(AdminActionSpec(
            name="fixture_once", risk_level="low", preview=lambda params: {}, execute=execute,
        ))
        preview = self.ops.preview_admin_action("fixture_once")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.ops.execute_admin_action, preview["id"])
            try:
                self.assertTrue(started.wait(3))
                duplicate = self.ops.execute_admin_action(preview["id"])
                self.assertEqual(len(calls), 1)
                self.assertEqual(duplicate["execution_status"], "executing")
            finally:
                release.set()
            result = future.result(timeout=3)
        self.assertEqual(result["execution_status"], "executed")
        self.assertEqual(self.ops.execute_admin_action(preview["id"])["execution_status"], "executed")
        self.assertEqual(len(calls), 1)

    def test_failed_handler_is_uncertain_and_not_automatically_reexecuted(self):
        calls = []

        def execute(params):
            calls.append(params)
            raise ValueError("private-exception-sentinel")

        self.ops.register_admin_action(AdminActionSpec(
            name="fixture_unknown", risk_level="low", preview=lambda params: {}, execute=execute,
        ))
        preview = self.ops.preview_admin_action("fixture_unknown")
        outcome = self.ops.execute_admin_action(preview["id"])
        self.assertEqual(outcome["execution_status"], "execution_unknown")
        self.assertNotIn("private-exception-sentinel", str(outcome))
        self.assertIsNotNone(outcome["audit_event_id"])
        self.assertEqual(self.ops.execute_admin_action(preview["id"])["execution_status"], "execution_unknown")
        self.assertEqual(len(calls), 1)

    def test_expected_actor_fails_closed_and_risk_change_requires_new_preview(self):
        calls = []
        self.ops.register_admin_action(AdminActionSpec(
            name="fixture_bound", risk_level="low", preview=lambda params: {}, execute=lambda params: calls.append(params) or {},
        ))
        preview = self.ops.preview_admin_action("fixture_bound", actor="client-a")
        with self.assertRaises(ValueError):
            self.ops.execute_admin_action(preview["id"], expected_actor="client-b")
        self.ops.register_admin_action(AdminActionSpec(
            name="fixture_bound", risk_level="high", preview=lambda params: {}, execute=lambda params: calls.append(params) or {},
        ))
        with self.assertRaises(ValueError):
            self.ops.execute_admin_action(preview["id"], expected_actor="client-a")
        self.assertEqual(calls, [])

    def test_preview_request_identity_survives_reopen_and_conflicts_fail_closed(self):
        first = self.ops.preview_admin_action("sync_trace_index", {"limit": 1}, actor="client-a", role="operator", idempotency_key="request-a")
        other = MemoryRuntime(self.runtime.path)
        try:
            duplicate = other.quality_ops.preview_admin_action("sync_trace_index", {"limit": 1}, actor="client-a", role="operator", idempotency_key="request-a")
            self.assertEqual(first["id"], duplicate["id"])
            with self.assertRaises(ValueError):
                other.quality_ops.preview_admin_action("sync_trace_index", {"limit": 2}, actor="client-a", role="operator", idempotency_key="request-a")
            self.assertEqual(len(self.ops.list_admin_actions(actor="client-a")), 1)
        finally:
            other.close()
