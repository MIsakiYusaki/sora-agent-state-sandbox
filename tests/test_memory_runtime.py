from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.agent import AgentRunner
from sora_sandbox.commands import handle_command
from sora_sandbox.llm import FakeLLMClient
from sora_sandbox.memory_console import MemoryConsole
from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.state import seed_state


class MemoryRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.runtime = MemoryRuntime(Path(self.temp_dir.name) / "memory.db")
        self.addCleanup(self.runtime.close)
        self.runtime.seed_if_empty(
            [
                {
                    "id": "mem_test_workflow",
                    "type": "preference",
                    "content": "用户喜欢先讨论设计方案，再进入实现。",
                    "tags": ["workflow", "agent_design"],
                    "sensitivity": "low",
                }
            ]
        )

    def test_sqlite_fts_recall_returns_governed_gist_and_trace(self) -> None:
        frame = self.runtime.recall("我们先讨论设计方案")

        self.assertEqual(frame["backend"], "sqlite_fts")
        self.assertEqual(frame["retrieved_ids"], ["mem_test_workflow"])
        self.assertEqual(frame["items"][0]["gist"], "用户喜欢先讨论设计方案，再进入实现。")
        self.assertNotIn("raw_evidence", frame["items"][0])
        self.assertTrue(frame["recall_trace_id"])
        self.assertEqual(len(self.runtime.list_recall_traces()), 1)

    def test_explicit_low_risk_remember_request_auto_commits(self) -> None:
        result = self.runtime.observe_turn(
            "请记住：我喜欢先看工程设计，再看实现细节。",
            "好，我会记住。",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "active")
        self.assertIn("工程设计", result["summary"])
        self.assertEqual(result["type"], "project_principle")

    def test_forbidden_secret_is_not_persisted(self) -> None:
        before_memories = len(self.runtime.list_memories())
        before_candidates = len(self.runtime.list_candidates())

        result = self.runtime.observe_turn(
            "请记住：我的 API key 是 sk-do-not-store",
            "收到。",
        )

        self.assertEqual(result["status"], "discarded")
        self.assertEqual(result["reason"], "forbidden_secret_not_persisted")
        self.assertEqual(len(self.runtime.list_memories()), before_memories)
        self.assertEqual(len(self.runtime.list_candidates()), before_candidates)

    def test_sensitive_explicit_memory_stays_pending_and_local_only(self) -> None:
        result = self.runtime.observe_turn(
            "请记住：我的健康情况需要在交流中谨慎对待。",
            "好。",
        )

        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["sensitivity"], "sensitive")
        self.assertTrue(result["local_only"])

    def test_revision_reclassifies_sensitivity(self) -> None:
        memory = self.runtime.add_manual("用户偏好简洁回复")

        revised = self.runtime.revise(
            memory["id"],
            "用户的健康情况需要谨慎处理",
        )

        self.assertEqual(revised["sensitivity"], "sensitive")
        self.assertEqual(revised["retention_policy"], "local_only")
        self.assertEqual(self.runtime.get_memory(memory["id"])["status"], "superseded")

    def test_candidate_commit_revision_and_soft_delete_lifecycle(self) -> None:
        evidence_id = self.runtime.add_evidence(
            source_type="test",
            content_summary="用户偏好短回复",
        )
        candidate = self.runtime.propose(
            summary="用户偏好短回复",
            memory_type="preference",
            evidence_ids=[evidence_id],
            reason="test_candidate",
        )
        self.assertEqual(candidate["status"], "pending")

        memory = self.runtime.commit([candidate["candidate_id"]])["committed"][0]
        self.assertEqual(memory["status"], "active")
        self.assertTrue(self.runtime.evidence_for_memory(memory["id"]))
        self.assertTrue(self.runtime.revisions_for_memory(memory["id"]))

        self.runtime.delete(memory["id"])
        self.assertEqual(self.runtime.get_memory(memory["id"])["status"], "deleted")
        self.assertNotIn(
            memory["id"],
            self.runtime.recall("短回复")["retrieved_ids"],
        )

        self.runtime.restore(memory["id"])
        self.assertEqual(self.runtime.get_memory(memory["id"])["status"], "active")
        self.assertIn(
            memory["id"],
            self.runtime.recall("短回复")["retrieved_ids"],
        )

    def test_memory_console_manages_runtime(self) -> None:
        console = MemoryConsole(self.runtime)
        output, should_back = console.execute(
            'add "用户偏好先看结论" --type preference --tags writing'
        )
        self.assertFalse(should_back)
        self.assertIn("用户偏好先看结论", output)

        output, _ = console.execute("search 先看结论")
        self.assertIn("用户偏好先看结论", output)

        output, should_back = console.execute("back")
        self.assertTrue(should_back)
        self.assertIn("主界面", output)

    def test_agent_runner_uses_runtime_and_persists_explicit_memory(self) -> None:
        state = seed_state()
        self.runtime.import_legacy_memories(state["memory"]["items"])
        runner = AgentRunner(FakeLLMClient(), memory_runtime=self.runtime)

        result = runner.run(state, "我们先讨论设计方案")
        self.assertEqual(result["context_pack"]["recollection_frame"]["backend"], "sqlite_fts")
        self.assertIn("mem_test_workflow", result["response_plan"]["used_memory_ids"])

        remember_result = runner.run(state, "请记住：我喜欢先验证接口，再做界面。")
        self.assertTrue(remember_result["memory_updates"])
        self.assertTrue(
            any("先验证接口" in item["summary"] for item in self.runtime.list_memories())
        )
        self.assertEqual(state["memory"]["runtime"]["mode"], "in_process")

    def test_legacy_memory_hook_command_commits_to_canonical_runtime(self) -> None:
        state = seed_state()
        self.runtime.import_legacy_memories(state["memory"]["items"])
        handle_command("/start-micro-quest evt_starport_lamp_001", state)
        handle_command(
            '/complete-micro-quest evt_starport_lamp_001 --choice "写一盏送回星港的灯"',
            state,
        )
        hook_id = state["worldops"]["memory_hooks"][0]["hook_id"]

        output, _ = handle_command(
            f"/commit-memory-hook {hook_id}",
            state,
            memory_admin=self.runtime,
        )

        self.assertIn('"status": "committed"', output)
        canonical_id = state["worldops"]["memory_hooks"][0]["committed_memory_id"]
        self.assertEqual(self.runtime.get_memory(canonical_id)["status"], "active")
        self.assertTrue(
            any(
                "写一盏送回星港的灯" in memory["summary"]
                for memory in self.runtime.list_memories("active")
            )
        )


if __name__ == "__main__":
    unittest.main()
