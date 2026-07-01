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
from sora_sandbox.memory_extractor_schema import (
    ExtractorValidationError,
    validate_extractor_result,
)
from sora_sandbox.extractor_eval import ExtractorEvalConfig, run_extractor_eval
from sora_sandbox.memory_extractor_provider import OpenAICompatibleExtractorProvider
from sora_sandbox.memory_extractor_repair import repair_extractor_json
from sora_sandbox.memory_console import MemoryConsole
from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.memory_system import MemorySystem
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

    def test_hybrid_rrf_recall_returns_governed_gist_and_trace(self) -> None:
        frame = self.runtime.recall("我们先讨论设计方案")

        self.assertEqual(frame["backend"], "hybrid_rrf")
        self.assertIn("fts", frame["recall_plan"]["backends"])
        self.assertIn("vector_local", frame["recall_plan"]["backends"])
        self.assertEqual(frame["retrieved_ids"], ["mem_test_workflow"])
        self.assertEqual(frame["items"][0]["gist"], "用户喜欢先讨论设计方案，再进入实现。")
        self.assertIn("fts", frame["items"][0]["matched_backends"])
        self.assertNotIn("raw_evidence", frame["items"][0])
        self.assertTrue(frame["recall_trace_id"])
        self.assertEqual(len(self.runtime.list_recall_traces()), 1)

        quoted = self.runtime.recall("我之前关于“设计方案”的记忆是什么？")
        self.assertEqual(quoted["recall_plan"]["salient_terms"], ["设计方案"])

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

    def test_sensitive_explicit_memory_goes_to_review_and_stays_local_only(self) -> None:
        result = self.runtime.observe_turn(
            "请记住：我的健康情况需要在交流中谨慎对待。",
            "好。",
        )

        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["sensitivity"], "sensitive")
        self.assertTrue(result["local_only"])
        reviews = self.runtime.list_review_items()
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["candidate_id"], result["candidate_id"])
        self.assertEqual(reviews[0]["queue"], "privacy")

    def test_explicit_memory_keeps_raw_evidence_locally(self) -> None:
        result = self.runtime.observe_turn(
            "请记住：我希望 Sora 是陪伴项目，不是助手项目。",
            "我会记住这个方向。",
        )

        evidence = self.runtime.evidence_for_memory(result["id"])
        self.assertEqual(evidence[0]["raw_content"], "请记住：我希望 Sora 是陪伴项目，不是助手项目。")
        self.assertEqual(evidence[0]["content_summary"], "我希望 Sora 是陪伴项目，不是助手项目")

    def test_implicit_statement_creates_review_candidate_with_raw_evidence(self) -> None:
        updates = self.runtime.observe_turn_candidates(
            "我喜欢 Sora 在陪伴场景里保留一点亲近感。",
            "我会把这个偏好先放进候选里。",
        )

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["status"], "needs_review")
        self.assertEqual(updates[0]["type"], "preference")
        self.assertEqual(updates[0]["reason"], "implicit_preference_statement")

        memory = self.runtime.commit([updates[0]["candidate_id"]])["committed"][0]
        evidence = self.runtime.evidence_for_memory(memory["id"])
        self.assertEqual(evidence[0]["raw_content"], "我喜欢 Sora 在陪伴场景里保留一点亲近感。")
        self.assertEqual(evidence[0]["metadata"]["extraction_reason"], "implicit_preference_statement")

    def test_ingest_turn_creates_episode_events_and_extraction_job(self) -> None:
        result = self.runtime.ingest_turn(
            "remember that I prefer local-first memory design",
            "I will keep that as context.",
            session_id="session-test",
            turn_id="turn-test",
        )

        self.assertEqual(result["turn_id"], "turn-test")
        self.assertEqual(result["episode"]["status"], "sealed")
        self.assertEqual(result["episode"]["sealed_reason"], "turn_complete")
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(result["events"][0]["actor"], "user")
        self.assertEqual(result["events"][1]["actor"], "sora")
        self.assertEqual(result["extraction_job"]["status"], "pending")

        trace = self.runtime.get_episode_trace(result["episode"]["episode_id"])
        self.assertEqual(trace["episode"]["episode_id"], result["episode"]["episode_id"])
        self.assertEqual([event["actor"] for event in trace["events"]], ["user", "sora"])
        self.assertEqual(trace["extraction_jobs"][0]["job_id"], result["extraction_job"]["job_id"])

    def test_observed_memory_links_back_to_episode_trace(self) -> None:
        result = self.runtime.observe_turn(
            "remember that I prefer local-first memory design",
            "I will remember that.",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "active")
        evidence = self.runtime.evidence_for_memory(result["id"])
        episode_id = evidence[0]["metadata"]["episode_id"]
        self.assertTrue(episode_id)
        self.assertTrue(evidence[0]["metadata"]["event_ids"])
        self.assertTrue(evidence[0]["metadata"]["extraction_job_id"])

        trace = self.runtime.get_episode_trace(episode_id)
        self.assertEqual(trace["episode"]["episode_id"], episode_id)
        self.assertEqual(len(trace["events"]), 2)
        self.assertEqual(trace["evidence"][0]["id"], evidence[0]["id"])
        self.assertEqual(trace["candidates"][0]["committed_memory_id"], result["id"])

    def test_session_window_strategy_aggregates_before_manual_seal(self) -> None:
        first = self.runtime.ingest_turn(
            "local memory design should stay local-first",
            "Noted.",
            session_id="design-session",
            episode_strategy="session_window",
            seal=False,
        )
        second = self.runtime.ingest_turn(
            "local memory design should keep evidence replayable",
            "That fits the design.",
            session_id="design-session",
            episode_strategy="session_window",
            seal=False,
        )

        self.assertIsNone(first["extraction_job"])
        self.assertIsNone(second["extraction_job"])
        self.assertEqual(first["episode"]["episode_id"], second["episode"]["episode_id"])
        self.assertEqual(second["episode"]["status"], "open")
        self.assertEqual(len(second["episode"]["event_ids"]), 4)

        sealed = self.runtime.seal_episode(second["episode"]["episode_id"], reason="manual_review")
        self.assertEqual(sealed["episode"]["status"], "sealed")
        self.assertEqual(sealed["episode"]["sealed_reason"], "manual_review")
        self.assertEqual(sealed["extraction_job"]["status"], "pending")

    def test_extraction_job_lifecycle_is_explicit_and_auditable(self) -> None:
        result = self.runtime.ingest_turn(
            "remember that evidence replay matters",
            "I will remember that.",
        )
        job_id = result["extraction_job"]["job_id"]

        running = self.runtime.start_extraction_job(job_id)
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["attempts"], 1)

        completed = self.runtime.complete_extraction_job(job_id)
        self.assertEqual(completed["status"], "completed")
        self.assertIsNone(completed["error"])

    def test_event_delete_propagates_to_episode_and_pending_jobs(self) -> None:
        result = self.runtime.ingest_turn(
            "remember that temporary evidence can be deleted",
            "I will remember that.",
        )
        episode_id = result["episode"]["episode_id"]
        event_ids = [event["event_id"] for event in result["events"]]

        first_delete = self.runtime.propagate_event_delete(event_ids[0])
        self.assertEqual(first_delete["episode"]["status"], "sealed")
        self.assertEqual(first_delete["cancelled_extraction_jobs"], [])

        second_delete = self.runtime.propagate_event_delete(event_ids[1])
        self.assertEqual(second_delete["episode"]["status"], "deleted")
        self.assertEqual(second_delete["episode"]["delete_reason"], "source_deleted")
        self.assertEqual(second_delete["cancelled_extraction_jobs"][0]["status"], "cancelled")
        trace = self.runtime.get_episode_trace(episode_id)
        self.assertTrue(all(event["deleted_at"] for event in trace["events"]))

    def test_evidence_has_hash_visibility_and_delete_fields(self) -> None:
        evidence_id = self.runtime.add_evidence(
            source_type="test",
            content_summary="User prefers auditable evidence",
            raw_content="raw turn text",
            visibility="local_only",
        )
        candidate = self.runtime.propose(
            summary="User prefers auditable evidence",
            memory_type="preference",
            evidence_ids=[evidence_id],
            reason="test_evidence_contract",
        )
        memory = self.runtime.commit([candidate["candidate_id"]])["committed"][0]

        evidence = self.runtime.evidence_for_memory(memory["id"])[0]
        self.assertEqual(evidence["visibility"], "local_only")
        self.assertEqual(len(evidence["content_hash"]), 64)
        self.assertIsNone(evidence["deleted_at"])
        self.assertIsNone(evidence["delete_reason"])

    def test_prefilter_archives_low_signal_episode_without_extraction_job(self) -> None:
        result = self.runtime.ingest_turn("ok", "ok", session_id="low-signal")

        self.assertEqual(result["episode"]["status"], "archived")
        self.assertIsNone(result["extraction_job"])
        self.assertFalse(result["pre_filter"]["should_extract"])
        self.assertEqual(result["pre_filter"]["reason"], "low_signal_ack_or_chitchat")

    def test_session_window_idle_timeout_splits_episode_and_queues_previous(self) -> None:
        first = self.runtime.ingest_turn(
            "memory architecture planning should remain auditable",
            "Noted.",
            session_id="idle-session",
            episode_strategy="session_window",
            seal=False,
            now="2026-01-01T00:00:00+00:00",
        )
        second = self.runtime.ingest_turn(
            "memory architecture planning continues after a break",
            "Still with you.",
            session_id="idle-session",
            episode_strategy="session_window",
            seal=False,
            now="2026-01-01T00:31:00+00:00",
        )

        self.assertNotEqual(first["episode"]["episode_id"], second["episode"]["episode_id"])
        previous = self.runtime.get_episode_trace(first["episode"]["episode_id"])["episode"]
        self.assertEqual(previous["status"], "sealed")
        self.assertEqual(previous["sealed_reason"], "idle_timeout")
        self.assertEqual(second["episode"]["status"], "open")

    def test_extraction_worker_claim_retry_and_complete_marks_episode_extracted(self) -> None:
        result = self.runtime.ingest_turn(
            "remember that worker protocol should be auditable",
            "I will keep the worker protocol trace.",
        )
        job_id = result["extraction_job"]["job_id"]

        claimed = self.runtime.claim_next_extraction_job()
        self.assertEqual(claimed["job_id"], job_id)
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["attempts"], 1)

        retry = self.runtime.fail_extraction_job(
            job_id,
            error="temporary extractor timeout",
            retry=True,
            retry_delay_seconds=0,
        )
        self.assertEqual(retry["status"], "pending")
        self.assertEqual(retry["error"], "temporary extractor timeout")

        claimed_again = self.runtime.claim_next_extraction_job()
        self.assertEqual(claimed_again["attempts"], 2)
        completed = self.runtime.complete_extraction_job(
            job_id,
            result_summary="1 candidate extracted",
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result_summary"], "1 candidate extracted")
        trace = self.runtime.get_episode_trace(result["episode"]["episode_id"])
        self.assertEqual(trace["episode"]["status"], "extracted")

    def test_async_extractor_worker_materializes_golden_add_and_auto_commits(self) -> None:
        result = self.runtime.ingest_turn(
            "remember that I prefer async extractor design for Project Sora memory",
            "I will keep that design preference.",
        )

        worker_result = self.runtime.run_extraction_once()

        self.assertEqual(worker_result["status"], "completed")
        self.assertEqual(worker_result["job"]["job_id"], result["extraction_job"]["job_id"])
        self.assertEqual(worker_result["materialized"]["candidates"][0]["operation"], "ADD")
        self.assertEqual(worker_result["materialized"]["candidates"][0]["schema_version"], "extractor_result.v1")
        self.assertEqual(len(worker_result["materialized"]["committed"]), 1)
        committed = worker_result["materialized"]["committed"][0]
        self.assertEqual(committed["status"], "active")
        self.assertIn("asynchronously outside the response hot path", committed["summary"])
        trace = self.runtime.get_episode_trace(result["episode"]["episode_id"])
        self.assertEqual(trace["episode"]["status"], "extracted")

    def test_async_extractor_worker_noop_negative_sample_does_not_create_candidate(self) -> None:
        before_candidates = len(self.runtime.list_candidates())
        result = self.runtime.ingest_turn(
            "remember that I hate all docs, just kidding do not remember that",
            "I will not treat the joke as durable memory.",
        )

        worker_result = self.runtime.run_extraction_once()

        self.assertEqual(worker_result["status"], "completed")
        self.assertEqual(worker_result["job"]["job_id"], result["extraction_job"]["job_id"])
        self.assertEqual(len(worker_result["materialized"]["noop_candidates"]), 1)
        self.assertEqual(len(self.runtime.list_candidates()), before_candidates)

    def test_async_extractor_update_uses_related_memory_target(self) -> None:
        previous = self.runtime.add_manual(
            "User prefers Project Sora development to use minimal loops before broader architecture.",
            memory_type="project_principle",
            tags=["project_sora", "development_principle"],
        )
        result = self.runtime.ingest_turn(
            "I no longer want minimal loops; Project Sora should pursue current best architecture.",
            "Understood.",
        )

        worker_result = self.runtime.run_extraction_once(auto_commit=False)

        self.assertEqual(worker_result["status"], "completed")
        candidate = worker_result["materialized"]["candidates"][0]
        self.assertEqual(candidate["operation"], "UPDATE")
        self.assertEqual(candidate["target_memory_id"], previous["id"])
        self.assertEqual(candidate["supersedes_id"], previous["id"])
        self.assertGreaterEqual(candidate["conflict_score"], 0.75)

        committed = self.runtime.commit([candidate["candidate_id"]], actor="test")["committed"][0]
        self.assertEqual(self.runtime.get_memory(previous["id"])["status"], "superseded")
        self.assertEqual(committed["supersedes_id"], previous["id"])

    def test_extractor_validator_rejects_out_of_bounds_evidence_span(self) -> None:
        result = self.runtime.ingest_turn(
            "remember that span validation matters",
            "I will remember that.",
        )
        input_package = self.runtime.build_extraction_input(result["extraction_job"]["job_id"])
        event_id = input_package["events"][0]["event_id"]
        payload = {
            "schema_version": "extractor_result.v1",
            "job_id": input_package["job"]["job_id"],
            "episode_id": input_package["episode"]["episode_id"],
            "model": {"provider": "test", "model": "test"},
            "candidates": [
                {
                    "operation": "ADD",
                    "memory_type": "project_principle",
                    "summary": "Span validation matters.",
                    "evidence_refs": [
                        {
                            "event_id": event_id,
                            "episode_id": input_package["episode"]["episode_id"],
                            "speaker": "user",
                            "span": [0, 9999],
                        }
                    ],
                    "confidence": 0.9,
                }
            ],
        }

        with self.assertRaises(ExtractorValidationError):
            validate_extractor_result(payload, input_package)

    def test_extractor_validator_rejects_invalid_contradiction_reference(self) -> None:
        result = self.runtime.ingest_turn(
            "remember that contradiction validation matters",
            "I will remember that.",
        )
        input_package = self.runtime.build_extraction_input(result["extraction_job"]["job_id"])
        event_id = input_package["events"][0]["event_id"]
        payload = {
            "schema_version": "extractor_result.v1",
            "job_id": input_package["job"]["job_id"],
            "episode_id": input_package["episode"]["episode_id"],
            "model": {"provider": "test", "model": "test"},
            "candidates": [
                {
                    "candidate_id": "draft_known",
                    "operation": "ADD",
                    "memory_type": "project_principle",
                    "summary": "Contradiction validation matters.",
                    "evidence_refs": [
                        {
                            "event_id": event_id,
                            "episode_id": input_package["episode"]["episode_id"],
                            "speaker": "user",
                            "span": [0, 12],
                        }
                    ],
                    "confidence": 0.9,
                }
            ],
            "contradictions": [
                {
                    "new_candidate_id": "draft_missing",
                    "existing_memory_id": "mem_missing",
                    "resolution": "UPDATE",
                }
            ],
        }

        with self.assertRaises(ExtractorValidationError):
            validate_extractor_result(payload, input_package)

    def test_extractor_json_repair_normalizes_common_real_llm_drift(self) -> None:
        result = self.runtime.ingest_turn(
            "remember that JSON repair matters for real extractor models",
            "I will remember that.",
        )
        input_package = self.runtime.build_extraction_input(result["extraction_job"]["job_id"])
        event_id = input_package["events"][0]["event_id"]
        repaired, repairs = repair_extractor_json(
            {
                "candidate": {
                    "operation": "add",
                    "type": "project",
                    "text": "JSON repair matters for real extractor models.",
                    "evidence": event_id,
                    "scores": {"confidence": "0.91", "importance": "0.8"},
                    "sensitivity": "low",
                }
            },
            input_package,
        )

        validated = validate_extractor_result(repaired, input_package)

        self.assertIn("candidate_to_candidates", repairs)
        self.assertEqual(validated.candidates[0].operation.value, "ADD")
        self.assertEqual(validated.candidates[0].memory_type, "project_principle")
        self.assertEqual(validated.candidates[0].scores.confidence, 0.91)

    def test_openai_provider_second_pass_repairs_malformed_json(self) -> None:
        class RepairingProvider(OpenAICompatibleExtractorProvider):
            def __init__(self) -> None:
                super().__init__(api_key="test", base_url="https://example.invalid")
                self.calls = 0

            def _post_chat_completion(self, body):  # type: ignore[no-untyped-def]
                self.calls += 1
                if self.calls == 1:
                    return {
                        "model": "fake-real",
                        "choices": [
                            {
                                "message": {
                                    "content": '{"candidates":[{"operation":"ADD" "memory_type":"project_principle"}]}'
                                }
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    }
                return {
                    "model": "fake-real",
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"candidates":[{"operation":"ADD","memory_type":"project_principle",'
                                    '"summary":"Second pass JSON repair matters.",'
                                    '"evidence_refs":["$first_user_event"],"confidence":0.91,'
                                    '"importance":0.7,"stability":0.7,"specificity":0.8,'
                                    '"novelty":1.0,"conflict_score":0.0,"sensitivity":"low"}]}'
                                )
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
                }

        result = self.runtime.ingest_turn(
            "remember that second pass JSON repair matters",
            "I will remember that.",
        )
        input_package = self.runtime.build_extraction_input(result["extraction_job"]["job_id"])

        payload = RepairingProvider().extract(input_package)
        validated = validate_extractor_result(payload, input_package)

        self.assertEqual(validated.candidates[0].summary, "Second pass JSON repair matters.")
        self.assertTrue(validated.trace["json_repair_pass"]["used"])
        self.assertEqual(validated.trace["usage"]["total_tokens"], 45)

    def test_extractor_observability_and_backfill_are_available(self) -> None:
        result = self.runtime.ingest_turn(
            "remember that observability should track extractor jobs",
            "I will remember that.",
        )

        worker_result = self.runtime.run_extraction_once()
        observability = self.runtime.extraction_observability()
        backfill = self.runtime.backfill_extraction_jobs(force=True, limit=1)

        self.assertEqual(worker_result["status"], "completed")
        self.assertGreaterEqual(observability["jobs_by_status"].get("completed", 0), 1)
        self.assertIn("llm_usage", observability)
        self.assertEqual(backfill["created_count"], 1)
        self.assertEqual(backfill["created_jobs"][0]["episode_id"], result["episode"]["episode_id"])

    def test_extractor_eval_runner_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as report_dir:
            report = run_extractor_eval(
                ExtractorEvalConfig(
                    cases_path=ROOT / "evals" / "extractor" / "cases.jsonl",
                    report_dir=Path(report_dir),
                )
            )

            self.assertEqual(report["failed_cases"], [])
            self.assertEqual(report["operation_accuracy"], 1.0)
            self.assertTrue((Path(report_dir) / "extractor_eval.json").exists())
            self.assertTrue((Path(report_dir) / "extractor_eval.md").exists())

    def test_event_delete_cascades_to_evidence_memory_index_and_replay(self) -> None:
        memory = self.runtime.observe_turn(
            "remember that cascade delete memory should disappear from recall",
            "I will remember it.",
        )
        self.assertEqual(memory["status"], "active")
        evidence = self.runtime.evidence_for_memory(memory["id"])[0]
        episode_id = evidence["metadata"]["episode_id"]
        event_id = evidence["metadata"]["event_ids"][0]

        result = self.runtime.propagate_event_delete(event_id, reason="test_source_deleted")

        self.assertEqual(result["deleted_evidence"][0]["id"], evidence["id"])
        self.assertEqual(result["disabled_memories"][0]["id"], memory["id"])
        self.assertEqual(self.runtime.get_memory(memory["id"])["status"], "disabled")
        self.assertNotIn(
            memory["id"],
            self.runtime.recall("cascade delete memory")["retrieved_ids"],
        )
        replay = self.runtime.replay_episode(episode_id)
        target_types = {item["target_type"] for item in replay["delete_propagations"]}
        self.assertIn("evidence", target_types)
        self.assertIn("memory_record", target_types)
        self.assertIn("memory_fts", target_types)
        self.assertTrue(replay["replay_contract"]["contains_delete_propagation"])

    def test_memory_console_exposes_episode_and_job_admin(self) -> None:
        console = MemoryConsole(self.runtime)
        result = self.runtime.ingest_turn(
            "remember that admin needs episode trace",
            "I will keep the trace.",
        )
        episode_id = result["episode"]["episode_id"]

        output, _ = console.execute("episodes all")
        self.assertIn(episode_id, output)

        output, _ = console.execute(f"episode-trace {episode_id}")
        self.assertIn(result["extraction_job"]["job_id"], output)

        output, _ = console.execute("run-job")
        self.assertIn("completed", output)

        output, _ = console.execute("observability")
        self.assertIn("jobs_by_status", output)

    def test_memory_system_facade_exposes_recall_contract(self) -> None:
        system = MemorySystem(self.runtime)

        frame = system.recall("讨论设计方案")

        self.assertEqual(frame["backend"], "hybrid_rrf")
        self.assertEqual(frame["retrieved_ids"], ["mem_test_workflow"])

    def test_revision_reclassifies_sensitivity(self) -> None:
        memory = self.runtime.add_manual("用户偏好简洁回复")

        revised = self.runtime.revise(
            memory["id"],
            "用户的健康情况需要谨慎处理",
        )

        self.assertEqual(revised["sensitivity"], "sensitive")
        self.assertEqual(revised["retention_policy"], "local_encrypted")
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
        self.assertEqual(candidate["status"], "needs_review")

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

    def test_review_approval_commits_candidate_and_resolves_queue(self) -> None:
        candidate = self.runtime.observe_turn(
            "请记住：我的健康情况需要在交流中谨慎对待。",
            "好。",
        )
        review = self.runtime.list_review_items()[0]

        result = self.runtime.approve_review(review["review_id"], actor="test_reviewer")

        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["review"]["status"], "resolved")
        self.assertEqual(result["committed"][0]["status"], "active")
        self.assertEqual(
            self.runtime.get_candidate(candidate["candidate_id"])["status"],
            "committed",
        )
        self.assertEqual(self.runtime.health()["open_reviews"], 0)

    def test_review_reject_and_manual_defer_resolve_queue(self) -> None:
        rejected_candidate = self.runtime.observe_turn(
            "请记住：我的健康情况需要在交流中谨慎对待。",
            "好。",
        )
        rejected_review = self.runtime.list_review_items()[0]

        reject_result = self.runtime.reject_review(
            rejected_review["review_id"],
            reason="test_reject",
            actor="test_reviewer",
        )

        self.assertEqual(reject_result["status"], "rejected")
        self.assertEqual(reject_result["review"]["status"], "resolved")
        self.assertEqual(
            self.runtime.get_candidate(rejected_candidate["candidate_id"])["status"],
            "rejected",
        )

        deferred_candidate = self.runtime.observe_turn(
            "请记住：我的财务情况需要在交流中谨慎对待。",
            "好。",
        )
        deferred_review = self.runtime.list_review_items()[0]

        defer_result = self.runtime.defer_review(
            deferred_review["review_id"],
            days=7,
            actor="test_reviewer",
        )

        self.assertEqual(defer_result["status"], "deferred")
        deferred = self.runtime.get_candidate(deferred_candidate["candidate_id"])
        self.assertEqual(deferred["status"], "deferred")
        self.assertTrue(deferred["deferred_until"])
        self.assertEqual(self.runtime.health()["open_reviews"], 0)

    def test_deferred_candidates_expire_to_rejected(self) -> None:
        evidence_id = self.runtime.add_evidence(
            source_type="test",
            content_summary="low stability candidate",
        )

        candidate = self.runtime.propose(
            summary="low stability candidate",
            memory_type="preference",
            evidence_ids=[evidence_id],
            confidence=0.8,
            stability=0.1,
            reason="test_low_stability",
        )

        self.assertEqual(candidate["status"], "deferred")
        expired = self.runtime.process_expired_deferrals(
            now="9999-01-01T00:00:00+00:00",
        )

        self.assertEqual(expired["expired_count"], 1)
        self.assertEqual(
            self.runtime.get_candidate(candidate["candidate_id"])["status"],
            "rejected",
        )

    def test_batch_gate_processes_multiple_pending_candidates(self) -> None:
        first_evidence = self.runtime.add_evidence(
            source_type="test",
            content_summary="batch candidate one",
        )
        second_evidence = self.runtime.add_evidence(
            source_type="test",
            content_summary="batch candidate two",
        )
        first = self.runtime.propose(
            summary="batch candidate one",
            memory_type="preference",
            evidence_ids=[first_evidence],
            confidence=0.8,
            reason="test_batch",
            allow_auto_commit=False,
        )
        second = self.runtime.propose(
            summary="batch candidate two",
            memory_type="preference",
            evidence_ids=[second_evidence],
            confidence=0.8,
            reason="test_batch",
            allow_auto_commit=False,
        )

        result = self.runtime.run_batch_gate([first["candidate_id"], second["candidate_id"]])

        self.assertEqual(result["requested_count"], 2)
        self.assertEqual(result["status_counts"]["needs_review"], 2)
        self.assertEqual(self.runtime.get_candidate(first["candidate_id"])["status"], "needs_review")
        self.assertEqual(self.runtime.get_candidate(second["candidate_id"])["status"], "needs_review")

    def test_high_risk_candidate_is_quarantined_in_independent_table(self) -> None:
        evidence_id = self.runtime.add_evidence(
            source_type="test",
            content_summary="high risk private material",
            sensitivity="high",
        )

        result = self.runtime.propose(
            summary="high risk private material",
            memory_type="preference",
            evidence_ids=[evidence_id],
            sensitivity="high",
            confidence=0.95,
            reason="test_high_risk",
        )

        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(len(self.runtime.list_quarantines()), 1)
        self.assertEqual(self.runtime.list_quarantines()[0]["source_id"], result["candidate_id"])
        self.assertFalse(
            any(memory["summary"] == "high risk private material" for memory in self.runtime.list_memories())
        )

    def test_candidate_hash_idempotency_merges_replayed_evidence(self) -> None:
        first_evidence = self.runtime.add_evidence(
            source_type="test",
            content_summary="用户偏好可审计记忆",
        )
        second_evidence = self.runtime.add_evidence(
            source_type="test",
            content_summary="用户偏好可审计记忆",
            raw_content="same summary from replayed backfill",
        )
        first_candidate = self.runtime.propose(
            summary="用户偏好可审计记忆",
            memory_type="preference",
            evidence_ids=[first_evidence],
            confidence=0.95,
            reason="first_backfill",
            allow_auto_commit=False,
        )
        second_candidate = self.runtime.propose(
            summary="用户偏好可审计记忆",
            memory_type="preference",
            evidence_ids=[second_evidence],
            confidence=0.95,
            reason="replayed_backfill",
            allow_auto_commit=False,
        )

        first_memory = self.runtime.commit([first_candidate["candidate_id"]])["committed"][0]
        second_memory = self.runtime.commit([second_candidate["candidate_id"]])["committed"][0]

        self.assertEqual(first_memory["id"], second_memory["id"])
        self.assertEqual(len(self.runtime.evidence_for_memory(first_memory["id"])), 2)

    def test_rollback_committed_add_removes_memory_from_recall(self) -> None:
        memory = self.runtime.add_manual(
            "用户偏好 rollback 测试记忆",
            memory_type="preference",
        )
        candidate = next(
            item
            for item in self.runtime.list_candidates("committed")
            if item["committed_memory_id"] == memory["id"]
        )

        result = self.runtime.rollback_committed_candidate(
            candidate["candidate_id"],
            reason="test_rollback",
        )

        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.runtime.get_candidate(candidate["candidate_id"])["status"], "rolled_back")
        self.assertEqual(self.runtime.get_memory(memory["id"])["status"], "disabled")
        self.assertNotIn(
            memory["id"],
            self.runtime.recall("rollback 测试记忆")["retrieved_ids"],
        )

    def test_rollback_committed_update_restores_previous_memory(self) -> None:
        previous = self.runtime.add_manual(
            "用户偏好旧版记忆方案",
            memory_type="preference",
        )
        revised = self.runtime.revise(
            previous["id"],
            "用户偏好新版记忆方案",
            reason="test_revision",
        )
        candidate = next(
            item
            for item in self.runtime.list_candidates("committed")
            if item["committed_memory_id"] == revised["id"]
            and item["operation"] == "UPDATE"
        )

        result = self.runtime.rollback_committed_candidate(
            candidate["candidate_id"],
            reason="test_rollback_update",
        )

        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.runtime.get_memory(revised["id"])["status"], "disabled")
        self.assertEqual(self.runtime.get_memory(previous["id"])["status"], "active")
        self.assertIn(
            previous["id"],
            self.runtime.recall("旧版记忆方案")["retrieved_ids"],
        )
        self.assertNotIn(
            revised["id"],
            self.runtime.recall("新版记忆方案")["retrieved_ids"],
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

    def test_repl_commands_expose_gate_review_and_quarantine_admin(self) -> None:
        state = seed_state()
        candidate = self.runtime.observe_turn(
            "请记住：我的健康情况需要在交流中谨慎对待。",
            "好。",
        )

        output, _ = handle_command("/memory-review-items", state, memory_admin=self.runtime)
        self.assertIn(candidate["candidate_id"], output)
        self.assertIn("privacy", output)

        output, _ = handle_command("/memory-gate-decisions", state, memory_admin=self.runtime)
        self.assertIn("NEEDS_REVIEW", output)

        review_id = self.runtime.list_review_items()[0]["review_id"]
        output, _ = handle_command(
            f"/approve-memory-review {review_id}",
            state,
            memory_admin=self.runtime,
        )
        self.assertIn("approved", output)
        self.assertEqual(
            self.runtime.list_review_items(status="resolved")[0]["status"],
            "resolved",
        )

        deferred_candidate = self.runtime.observe_turn(
            "请记住：我的财务情况需要在交流中谨慎对待。",
            "好。",
        )
        defer_review_id = self.runtime.list_review_items()[0]["review_id"]
        output, _ = handle_command(
            f"/defer-memory-review {defer_review_id} --days 3",
            state,
            memory_admin=self.runtime,
        )
        self.assertIn("deferred", output)
        self.assertEqual(
            self.runtime.get_candidate(deferred_candidate["candidate_id"])["status"],
            "deferred",
        )

        first_evidence = self.runtime.add_evidence(
            source_type="test",
            content_summary="command batch candidate one",
        )
        second_evidence = self.runtime.add_evidence(
            source_type="test",
            content_summary="command batch candidate two",
        )
        first = self.runtime.propose(
            summary="command batch candidate one",
            memory_type="preference",
            evidence_ids=[first_evidence],
            reason="command_batch",
            allow_auto_commit=False,
        )
        second = self.runtime.propose(
            summary="command batch candidate two",
            memory_type="preference",
            evidence_ids=[second_evidence],
            reason="command_batch",
            allow_auto_commit=False,
        )
        output, _ = handle_command(
            f"/run-batch-memory-gate {first['candidate_id']} {second['candidate_id']}",
            state,
            memory_admin=self.runtime,
        )
        self.assertIn("needs_review", output)

        evidence_id = self.runtime.add_evidence(
            source_type="test",
            content_summary="high risk private material",
            sensitivity="high",
        )
        quarantined = self.runtime.propose(
            summary="high risk private material",
            memory_type="preference",
            evidence_ids=[evidence_id],
            sensitivity="high",
            confidence=0.95,
            reason="test_high_risk",
        )
        output, _ = handle_command("/memory-quarantines", state, memory_admin=self.runtime)
        self.assertIn(quarantined["quarantine_id"], output)

        memory = self.runtime.add_manual("用户偏好命令回滚测试", memory_type="preference")
        rollback_candidate = next(
            item
            for item in self.runtime.list_candidates("committed")
            if item["committed_memory_id"] == memory["id"]
        )
        output, _ = handle_command(
            f"/rollback-memory-candidate {rollback_candidate['candidate_id']} command_test",
            state,
            memory_admin=self.runtime,
        )
        self.assertIn("rolled_back", output)
        self.assertEqual(self.runtime.get_memory(memory["id"])["status"], "disabled")

    def test_agent_runner_uses_runtime_and_persists_explicit_memory(self) -> None:
        state = seed_state()
        self.runtime.import_legacy_memories(state["memory"]["items"])
        runner = AgentRunner(FakeLLMClient(), memory_runtime=self.runtime)

        result = runner.run(state, "我们先讨论设计方案")
        self.assertEqual(result["context_pack"]["recollection_frame"]["backend"], "hybrid_rrf")
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
