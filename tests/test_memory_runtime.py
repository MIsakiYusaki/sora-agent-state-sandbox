from __future__ import annotations

import json
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
from sora_sandbox.memory_extractor_provider import GoldenSetExtractorProvider, OpenAICompatibleExtractorProvider
from sora_sandbox.memory_extractor_repair import repair_extractor_json
from sora_sandbox.memory_console import MemoryConsole
from sora_sandbox.memory_context_packer import PackedMemory
from sora_sandbox.memory_recall_planner import extract_salient_terms
from sora_sandbox.memory_reranker import HTTPCrossEncoderReranker, OpenAICompatibleLLMReranker
from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.memory_schema import JsonDict, RecallBudget, RecallPlan
from sora_sandbox.memory_system import MemorySystem
from sora_sandbox.memory_surface_policy import DeterministicSurfacePolicyProvider, SurfacePolicyEngine
from sora_sandbox.memory_vector_index import QdrantVectorIndex
from sora_sandbox.state import seed_state


class FakeQdrantClient:
    def __init__(self) -> None:
        self.points: dict[str, JsonDict] = {}
        self.requests: list[tuple[str, str, JsonDict | None]] = []
        self.fail_search = False

    def request(self, method: str, path: str, payload: JsonDict | None = None) -> JsonDict:
        self.requests.append((method, path, payload))
        if method == "GET":
            return {"result": {"status": "green"}}
        if method == "PUT" and "/points" not in path:
            return {"result": True}
        if method == "PUT" and "/points" in path:
            for point in (payload or {}).get("points", []):
                self.points[str(point["id"])] = point
            return {"result": {"operation_id": 1, "status": "completed"}}
        if method == "POST" and "/points/search" in path:
            if self.fail_search:
                raise RuntimeError("qdrant search unavailable")
            results = []
            must = (payload or {}).get("filter", {}).get("must", [])
            filters = {
                item.get("key"): item.get("match", {}).get("value")
                for item in must
                if isinstance(item, dict)
            }
            for point_id, point in self.points.items():
                point_payload = point.get("payload", {})
                if any(point_payload.get(key) != value for key, value in filters.items()):
                    continue
                results.append({"id": point_id, "score": 0.95, "payload": point_payload})
            return {"result": results[: int((payload or {}).get("limit") or 10)]}
        if method == "POST" and "/points/delete" in path:
            for point_id in (payload or {}).get("points", []):
                self.points.pop(str(point_id), None)
            return {"result": {"operation_id": 2, "status": "completed"}}
        return {"result": True}


class FakeCrossEncoderReranker(HTTPCrossEncoderReranker):
    def __init__(self, scores: list[float] | None = None, preferred_text: str = "") -> None:
        super().__init__(endpoint="http://reranker.local/rerank", model="fake-cross-encoder")
        self.scores = scores
        self.preferred_text = preferred_text

    def _post_json(self, payload: JsonDict) -> JsonDict:
        if self.scores is None:
            scores = [
                0.99 if self.preferred_text and self.preferred_text in document.get("text", "") else 0.05
                for document in payload.get("documents", [])
            ]
        else:
            scores = self.scores
        return {
            "results": [
                {"index": index, "relevance_score": score}
                for index, score in enumerate(scores)
            ],
            "usage": {"total_tokens": 7},
        }


class FakeLLMReranker(OpenAICompatibleLLMReranker):
    def __init__(self, preferred_text: str) -> None:
        super().__init__(
            api_key="test-key",
            base_url="http://llm.local/v1",
            model="fake-llm-reranker",
        )
        self.preferred_text = preferred_text

    def _chat_json(self, input_package: JsonDict) -> JsonDict:
        return {
            "scores": [
                {
                    "memory_id": document["id"],
                    "score": 0.98 if self.preferred_text in document.get("text", "") else 0.04,
                    "reason": "test score",
                }
                for document in input_package.get("documents", [])
            ],
            "usage": {"total_tokens": 11},
        }


class FakeSurfaceLLMProvider(DeterministicSurfacePolicyProvider):
    name = "fake_surface_llm"

    def __init__(self) -> None:
        self.compress_calls = 0
        self.relationship_calls = 0

    def compress(self, request: JsonDict) -> JsonDict:
        self.compress_calls += 1
        return {
            "text": "compressed surface gist",
            "degraded": False,
            "provider": self.name,
            "model": "fake-surface-llm",
            "prompt_version": "surface_policy_prompt.test",
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
            "cost": {"estimated_cost": 0.001, "currency": "USD"},
            "latency_ms": 3.2,
            "json_repair_pass": {"used": False},
            "fidelity_notes": ["memory_id preserved"],
        }

    def relationship_explain(self, request: JsonDict) -> JsonDict:
        self.relationship_calls += 1
        return {
            "text": "relationship-safe continuity gist",
            "degraded": False,
            "provider": self.name,
            "model": "fake-surface-llm",
            "prompt_version": "surface_policy_prompt.test",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "cost": {"estimated_cost": 0.001, "currency": "USD"},
            "latency_ms": 2.8,
            "json_repair_pass": {"used": False},
            "boundary_notes": ["no possessive framing"],
        }


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
        self.assertIn("vector", frame["recall_plan"]["backends"])
        self.assertIn("graph", frame["recall_plan"]["backends"])
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
        self.assertEqual(result["type"], "domain_principle")

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

    def test_evidence_persists_source_ref_and_span(self) -> None:
        evidence_id = self.runtime.add_evidence(
            source_type="test",
            content_summary="span aware evidence",
            raw_content="abcdef",
            source_ref="evt_source_1",
            span={"start": 1, "end": 4},
        )

        evidence = self.runtime.get_evidence(evidence_id)

        self.assertEqual(evidence["source_ref"], "evt_source_1")
        self.assertEqual(evidence["span"], {"start": 1, "end": 4})

    def test_canonical_store_lists_gets_links_and_unlinks_evidence(self) -> None:
        memory = self.runtime.add_manual(
            "用户偏好 canonical store 测试",
            memory_type="preference",
        )
        extra_evidence_id = self.runtime.add_evidence(
            source_type="test",
            content_summary="extra canonical evidence",
            raw_content="raw canonical evidence",
        )

        evidence = self.runtime.get_evidence(extra_evidence_id)
        listed = self.runtime.list_evidence(source_type="test")
        linked = self.runtime.link_evidence(memory["id"], extra_evidence_id, reason="test_link")

        self.assertEqual(evidence["raw_content"], "raw canonical evidence")
        self.assertTrue(any(item["id"] == extra_evidence_id for item in listed))
        self.assertIn(extra_evidence_id, linked["memory"]["evidence_ids"])
        self.assertTrue(
            any(
                revision["action"] == "evidence_linked"
                for revision in self.runtime.revisions_for_memory(memory["id"])
            )
        )

        unlinked = self.runtime.unlink_evidence(
            memory["id"],
            extra_evidence_id,
            reason="test_unlink",
        )

        self.assertEqual(unlinked["status"], "unlinked")
        self.assertNotIn(
            extra_evidence_id,
            self.runtime.get_memory(memory["id"])["evidence_ids"],
        )
        self.assertEqual(self.runtime.get_memory(memory["id"])["status"], "active")

    def test_delete_evidence_cascades_to_candidate_memory_and_fts(self) -> None:
        evidence_id = self.runtime.add_evidence(
            source_type="test",
            content_summary="delete evidence should remove recall",
        )
        candidate = self.runtime.propose(
            summary="delete evidence should remove recall",
            memory_type="preference",
            evidence_ids=[evidence_id],
            reason="test_evidence_delete",
            allow_auto_commit=False,
        )
        memory = self.runtime.commit([candidate["candidate_id"]])["committed"][0]
        pending_evidence_id = self.runtime.add_evidence(
            source_type="test",
            content_summary="pending candidate evidence",
        )
        pending = self.runtime.propose(
            summary="pending candidate evidence",
            memory_type="preference",
            evidence_ids=[pending_evidence_id],
            reason="test_pending_evidence_delete",
            allow_auto_commit=False,
        )

        deleted = self.runtime.delete_evidence(evidence_id, reason="test_delete_evidence")
        pending_deleted = self.runtime.delete_evidence(
            pending_evidence_id,
            reason="test_delete_pending_evidence",
        )

        self.assertEqual(deleted["status"], "deleted")
        self.assertEqual(self.runtime.get_evidence(evidence_id)["delete_reason"], "test_delete_evidence")
        self.assertEqual(self.runtime.get_memory(memory["id"])["status"], "disabled")
        self.assertNotIn(
            memory["id"],
            self.runtime.recall("delete evidence should remove recall")["retrieved_ids"],
        )
        self.assertEqual(
            self.runtime.get_candidate(pending["candidate_id"])["status"],
            "rejected",
        )
        self.assertEqual(pending_deleted["rejected_candidates"][0]["candidate_id"], pending["candidate_id"])

    def test_delete_evidence_rejects_only_exact_candidate_refs(self) -> None:
        now = "2026-01-01T00:00:00+00:00"
        with self.runtime._lock:
            for evidence_id in ("ev_delete", "ev_delete_suffix"):
                self.runtime._conn.execute(
                    """
                    INSERT INTO evidence (
                        id, source_type, content_summary, raw_content, raw_pointer,
                        source_ref, span_json, content_hash, visibility, sensitivity,
                        retention_policy, metadata_json, created_at
                    ) VALUES (?, 'test', ?, ?, NULL, NULL, '{}', ?, 'private', 'low',
                              'persistent', '{}', ?)
                    """,
                    (evidence_id, evidence_id, evidence_id, evidence_id * 8, now),
                )
            for candidate_id, evidence_ids in (
                ("mc_exact_ref", ["ev_delete"]),
                ("mc_suffix_ref", ["ev_delete_suffix"]),
            ):
                self.runtime._conn.execute(
                    """
                    INSERT INTO memory_candidate (
                        id, type, summary, tags_json, status, confidence, sensitivity,
                        reason, evidence_ids_json, explicit_user_request, local_only,
                        created_at
                    ) VALUES (?, 'preference', ?, '[]', 'pending', 0.8, 'low',
                              'exact_match_test', ?, 0, 0, ?)
                    """,
                    (candidate_id, candidate_id, json.dumps(evidence_ids), now),
                )
            self.runtime._conn.commit()

        deleted = self.runtime.delete_evidence("ev_delete", reason="exact_delete")

        self.assertEqual(deleted["rejected_candidates"][0]["candidate_id"], "mc_exact_ref")
        self.assertEqual(self.runtime.get_candidate("mc_exact_ref")["status"], "rejected")
        self.assertEqual(self.runtime.get_candidate("mc_suffix_ref")["status"], "pending")

    def test_link_evidence_rejects_blocked_memory_permission(self) -> None:
        memory = self.runtime.add_manual(
            "blocked permission memory",
            memory_type="preference",
        )
        evidence_id = self.runtime.add_evidence(
            source_type="test",
            content_summary="should not link to blocked memory",
        )
        with self.runtime._lock:
            self.runtime._conn.execute(
                "UPDATE memory_record SET retrieval_permission = 'blocked' WHERE id = ?",
                (memory["id"],),
            )
            self.runtime._conn.commit()

        with self.assertRaises(ValueError):
            self.runtime.link_evidence(memory["id"], evidence_id)

        self.assertNotIn(evidence_id, self.runtime.get_memory(memory["id"])["evidence_ids"])

    def test_store_audit_uses_exact_candidate_evidence_matching(self) -> None:
        now = "2026-01-01T00:00:00+00:00"
        with self.runtime._lock:
            for evidence_id in ("ev_abc", "ev_abcdef"):
                self.runtime._conn.execute(
                    """
                    INSERT INTO evidence (
                        id, source_type, content_summary, raw_content, raw_pointer,
                        source_ref, span_json, content_hash, visibility, sensitivity,
                        retention_policy, metadata_json, created_at
                    ) VALUES (?, 'test', ?, ?, NULL, NULL, '{}', ?, 'private', 'low',
                              'persistent', '{}', ?)
                    """,
                    (evidence_id, evidence_id, evidence_id, evidence_id * 8, now),
                )
            self.runtime._conn.execute(
                """
                INSERT INTO memory_candidate (
                    id, type, summary, tags_json, status, confidence, sensitivity,
                    reason, evidence_ids_json, explicit_user_request, local_only,
                    created_at
                ) VALUES ('mc_refs_longer_evidence', 'preference', 'refs longer evidence',
                          '[]', 'pending', 0.8, 'low', 'exact_audit_test',
                          ?, 0, 0, ?)
                """,
                (json.dumps(["ev_abcdef"]), now),
            )
            self.runtime._conn.commit()

        audit = self.runtime.store_audit()
        orphan_ids = {item["id"] for item in audit["issues"]["orphan_active_evidence"]}

        self.assertIn("ev_abc", orphan_ids)
        self.assertNotIn("ev_abcdef", orphan_ids)

    def test_store_audit_detects_and_repairs_fts_drift(self) -> None:
        memory = self.runtime.add_manual(
            "用户偏好 store audit 测试",
            memory_type="preference",
        )

        with self.runtime._lock:  # Test-only drift injection.
            self.runtime._conn.execute(
                "DELETE FROM memory_fts WHERE memory_id = ?",
                (memory["id"],),
            )
            self.runtime._conn.execute(
                "INSERT INTO memory_fts (memory_id, search_text) VALUES ('mem_orphan_fts', 'orphan')"
            )
            self.runtime._conn.commit()

        audit = self.runtime.store_audit()
        repaired = self.runtime.repair_fts_index(actor="test")
        audit_after = self.runtime.store_audit()

        self.assertEqual(audit["status"], "issues_found")
        self.assertIn(
            memory["id"],
            [item["id"] for item in audit["issues"]["fts_missing_active_memories"]],
        )
        self.assertIn(
            "mem_orphan_fts",
            [item["memory_id"] for item in audit["issues"]["fts_orphan_or_inactive_memories"]],
        )
        self.assertIn(memory["id"], repaired["memory_ids"])
        self.assertIn("mem_orphan_fts", repaired["removed_memory_ids"])
        self.assertEqual(repaired["removed_count"], 1)
        self.assertEqual(audit_after["issues"]["fts_orphan_or_inactive_memories"], [])
        self.assertEqual(audit_after["issues"]["fts_missing_active_memories"], [])
        self.assertEqual(audit_after["issue_breakdown"]["fts_missing_active_memories"], 0)

    def test_index_outbox_and_projectors_process_memory_changes(self) -> None:
        memory = self.runtime.add_manual(
            "relationship projector contract memory",
            memory_type="relationship_signal",
        )

        index_items = self.runtime.list_index_outbox(limit=20)
        projector_items = self.runtime.list_projector_outbox(limit=20)
        checkpoints = self.runtime.list_projector_checkpoints()

        self.assertTrue(memory["index_watermark"])
        self.assertTrue(all(item["status"] == "processed" for item in index_items))
        self.assertTrue(any(item["backend"] == "fts" for item in index_items))
        self.assertTrue(any(item["backend"] == "vector_local" for item in index_items))
        self.assertTrue(any(item["backend"] == "graph" for item in index_items))
        self.assertTrue(
            any(
                item["projector_name"] == "relationship"
                and item["action"] == "upsert"
                and item["status"] == "processed"
                for item in projector_items
            )
        )
        self.assertTrue(any(item["projector_name"] == "relationship" for item in checkpoints))

    def test_module05_indexes_real_vector_documents_and_trace_items(self) -> None:
        memory = self.runtime.add_manual(
            "workflow design vector recall alpha beta preference",
            memory_type="preference",
            tags=["workflow", "vector"],
        )

        document = self.runtime._conn.execute(
            "SELECT * FROM memory_index_document WHERE memory_id = ?",
            (memory["id"],),
        ).fetchone()
        embedding = self.runtime._conn.execute(
            "SELECT * FROM memory_embedding WHERE memory_id = ?",
            (memory["id"],),
        ).fetchone()
        vector_row = self.runtime._conn.execute(
            "SELECT * FROM memory_vector_index WHERE memory_id = ?",
            (memory["id"],),
        ).fetchone()
        graph_edges = self.runtime._conn.execute(
            "SELECT COUNT(*) FROM memory_graph_edge WHERE memory_id = ?",
            (memory["id"],),
        ).fetchone()[0]

        frame = self.runtime.recall("alpha beta workflow design", top_k=1)
        trace_item = self.runtime._conn.execute(
            "SELECT * FROM recall_trace_item WHERE trace_id = ? AND memory_id = ?",
            (frame["recall_trace_id"], memory["id"]),
        ).fetchone()

        self.assertEqual(document["schema_version"], "index_doc_v1")
        self.assertEqual(document["embedding_input_version"], "embedding_input_v1")
        self.assertGreater(embedding["vector_dim"], 0)
        self.assertEqual(vector_row["status"], "active")
        self.assertGreater(graph_edges, 0)
        self.assertEqual(frame["retrieved_ids"], [memory["id"]])
        self.assertIn("vector", frame["recall_plan"]["backends"])
        self.assertIn("graph", frame["recall_plan"]["backends"])
        self.assertGreater(frame["packing"]["token_budget"], 0)
        self.assertIsNotNone(trace_item["backend"])
        self.assertIsNotNone(trace_item["rrf_score"])
        self.assertGreater(self.runtime.get_memory(memory["id"])["access_count"], 0)

    def test_module05_index_document_preserves_lifecycle_metadata(self) -> None:
        memory = self.runtime.add_manual(
            "module five metadata stability expires fields",
            memory_type="preference",
        )

        document = self.runtime._conn.execute(
            "SELECT * FROM memory_index_document WHERE memory_id = ?",
            (memory["id"],),
        ).fetchone()
        metadata = json.loads(document["metadata_json"])

        self.assertIn("stability", metadata)
        self.assertIn("expires_at", metadata)
        self.assertIn("valid_from", metadata)
        self.assertIn("valid_until", metadata)

    def test_module05_outbox_idempotency_skips_duplicate_vector_payload(self) -> None:
        memory = self.runtime.add_manual(
            "module five duplicate vector payload",
            memory_type="preference",
        )

        first_count = self.runtime._conn.execute(
            "SELECT COUNT(*) FROM memory_vector_index WHERE memory_id = ?",
            (memory["id"],),
        ).fetchone()[0]
        queued = self.runtime.index_outbox.enqueue_memory(
            self.runtime.get_memory(memory["id"]),
            action="upsert",
            reason="test_duplicate",
            backends=("vector_local",),
        )
        self.runtime.process_index_outbox()
        processed = self.runtime.index_outbox.get_item(queued[0]["id"])
        second_count = self.runtime._conn.execute(
            "SELECT COUNT(*) FROM memory_vector_index WHERE memory_id = ?",
            (memory["id"],),
        ).fetchone()[0]

        self.assertEqual(first_count, 1)
        self.assertEqual(second_count, 1)
        self.assertEqual(processed["status"], "processed")
        self.assertTrue(processed["result"]["skipped"])

    def test_module05_dead_letter_requeue_and_rebuild(self) -> None:
        memory = self.runtime.add_manual(
            "module five rebuild dead letter memory",
            memory_type="preference",
        )
        with self.runtime._lock:
            self.runtime._conn.execute(
                """
                INSERT INTO memory_index_outbox (
                    id, backend, memory_id, action, status, attempts, max_attempts,
                    payload_json, result_json, index_generation, created_at
                ) VALUES (
                    'idx_bad_backend', 'unknown_backend', ?, 'upsert', 'pending',
                    0, 3, '{}', '{}', 'active', '2026-01-01T00:00:00+00:00'
                )
                """,
                (memory["id"],),
            )
            self.runtime._conn.commit()

        processed = self.runtime.process_index_outbox(limit=10)
        dead = self.runtime.list_index_outbox(status="dead_letter", limit=10)
        requeued = self.runtime.requeue_index_dead_letters(limit=10)
        rebuild = self.runtime.rebuild_memory_indexes(
            backends=("fts", "vector_local"),
            batch_size=1,
        )

        self.assertEqual(processed["dead_letter_count"], 1)
        self.assertEqual(dead[0]["id"], "idx_bad_backend")
        self.assertEqual(requeued["requeued_count"], 1)
        self.assertIn(rebuild["status"], {"completed", "completed_with_errors"})
        self.assertGreaterEqual(rebuild["processed_count"], 1)

    def test_module05_context_packing_trace_cleanup_and_recall_eval(self) -> None:
        first = self.runtime.add_manual(
            "module five packing topic first long memory detail " * 4,
            memory_type="preference",
            tags=["packing_topic"],
        )
        second = self.runtime.add_manual(
            "module five packing topic second long memory detail " * 4,
            memory_type="preference",
            tags=["packing_topic"],
        )

        frame = self.runtime.recall("packing_topic", top_k=3, token_budget=24)
        eval_run = self.runtime.run_recall_eval(
            cases=[
                {
                    "case_name": "packing_topic",
                    "query": "packing_topic first",
                    "expected_memory_ids": [first["id"]],
                    "forbidden_memory_ids": [],
                }
            ]
        )
        cleanup = self.runtime.cleanup_recall_traces(older_than_days=0)

        self.assertLessEqual(frame["packing"]["token_used"], 24)
        self.assertTrue(frame["packing"]["suppressed"])
        self.assertEqual(eval_run["status"], "passed")
        self.assertEqual(eval_run["passed_cases"], 1)
        self.assertGreaterEqual(cleanup["deleted_count"], 1)

    def test_module07_surface_policy_frame_trace_and_eval(self) -> None:
        memory = self.runtime.add_manual(
            "module seven surface policy remembers explicit topic",
            memory_type="preference",
            tags=["module7_surface"],
        )

        frame = self.runtime.recall("Do you remember module seven surface policy?", top_k=1)
        traces = self.runtime.list_surface_policy_traces(limit=1)
        eval_run = self.runtime.run_surface_eval()

        self.assertEqual(frame["surface_action"], "gist")
        self.assertEqual(frame["surface_action_v2"], "answer_memory_question")
        self.assertEqual(frame["memory_context_frame"]["version"], "memory_context_frame_v2")
        self.assertEqual(frame["memory_context_frame"]["items"][0]["memory_id"], memory["id"])
        self.assertEqual(frame["items"][0]["surface_instruction"], "answer_from_governed_gist")
        self.assertEqual(traces[0]["surface_action"], "answer_memory_question")
        self.assertEqual(traces[0]["items"][0]["memory_id"], memory["id"])
        self.assertEqual(eval_run["status"], "passed")

    def test_module07_surface_hide_override_blocks_visible_recall(self) -> None:
        memory = self.runtime.add_manual(
            "module seven hidden surface override memory",
            memory_type="preference",
            tags=["module7_hidden"],
        )

        override = self.runtime.hide_memory(memory["id"], actor="test_user")
        hidden = self.runtime.recall("module seven hidden surface override", top_k=3)
        cleared = self.runtime.clear_surface_override(memory["id"], actor="test_user")
        visible = self.runtime.recall("module seven hidden surface override", top_k=3)

        self.assertEqual(override["surface_permission"], "none")
        self.assertNotIn(memory["id"], hidden["retrieved_ids"])
        self.assertEqual(hidden["surface_action_v2"], "none")
        self.assertEqual(cleared["memory"]["surface_permission"], "auto")
        self.assertIn(memory["id"], visible["retrieved_ids"])
        self.assertTrue(self.runtime.list_surface_overrides(limit=5))

    def test_module07_silent_only_disclosure_requests_consent(self) -> None:
        memory = self.runtime.add_manual(
            "module seven silent only disclosure memory",
            memory_type="preference",
            tags=["module7_silent"],
        )
        with self.runtime._lock:
            self.runtime._conn.execute(
                "UPDATE memory_record SET disclosure_permission = 'silent_only' WHERE id = ?",
                (memory["id"],),
            )
            self.runtime._conn.commit()

        frame = self.runtime.recall("Do you remember module seven silent only disclosure memory?", top_k=1)
        consent = self.runtime.record_surface_consent(
            memory["id"],
            status="requested",
            recall_trace_id=frame["recall_trace_id"],
            prompt="test consent",
            actor="test_user",
        )

        self.assertEqual(frame["surface_action"], "none")
        self.assertEqual(frame["surface_action_v2"], "ask_consent")
        self.assertEqual(frame["retrieved_ids"], [])
        self.assertIn(memory["id"], frame["consent_request"]["memory_ids"])
        self.assertEqual(consent["status"], "requested")
        self.assertEqual(self.runtime.list_surface_consents(limit=1)[0]["id"], consent["id"])

    def test_module07_real_provider_compression_and_relationship_trace(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        provider = FakeSurfaceLLMProvider()
        runtime = MemoryRuntime(
            Path(temp_dir.name) / "memory.db",
            surface_policy=SurfacePolicyEngine(provider=provider),
        )
        self.addCleanup(runtime.close)
        compressed_memory = runtime.add_manual(
            "module seven provider compression alpha beta " * 12,
            memory_type="preference",
            tags=["module7_provider", "alpha_beta"],
        )
        relationship_memory = runtime.add_manual(
            "relationship companionship continuity should stay warm but not possessive",
            memory_type="relationship_signal",
            tags=["relationship", "companionship"],
        )
        with runtime._lock:
            runtime._conn.execute(
                "UPDATE memory_record SET relationship_impact = 0.8 WHERE id = ?",
                (relationship_memory["id"],),
            )
            runtime._conn.commit()

        compressed = runtime.recall("Do you remember alpha beta provider compression?", top_k=1, token_budget=18)
        relationship = runtime.recall("relationship companionship continuity", top_k=1, token_budget=120)
        traces = runtime.list_surface_policy_traces(limit=2)

        self.assertEqual(compressed["retrieved_ids"], [compressed_memory["id"]])
        self.assertGreater(provider.compress_calls, 0)
        self.assertTrue(compressed["packing"]["provider_usage"])
        self.assertEqual(compressed["items"][0]["gist"], "compressed surface gist")
        self.assertEqual(relationship["retrieved_ids"], [relationship_memory["id"]])
        self.assertGreater(provider.relationship_calls, 0)
        self.assertEqual(relationship["surface_action_v2"], "relationship_context")
        self.assertEqual(relationship["items"][0]["gist"], "relationship-safe continuity gist")
        self.assertTrue(any(trace["provider_usage"] for trace in traces))

    def test_module07_feedback_and_granted_consent_affect_surface_policy(self) -> None:
        feedback_memory = self.runtime.add_manual(
            "module seven feedback should not proactively surface",
            memory_type="preference",
            tags=["module7_feedback"],
        )
        self.runtime.record_surface_feedback(
            feedback_memory["id"],
            feedback_type="too_much",
            actor="test_user",
        )
        suppressed = self.runtime.recall("module seven feedback proactively surface", top_k=1)

        consent_memory = self.runtime.add_manual(
            "consentzeta omega can upgrade silent only disclosure",
            memory_type="preference",
            tags=["consentzeta", "omega"],
        )
        with self.runtime._lock:
            self.runtime._conn.execute(
                "UPDATE memory_record SET disclosure_permission = 'silent_only' WHERE id = ?",
                (consent_memory["id"],),
            )
            self.runtime._conn.commit()
        before = self.runtime.recall("Do you remember consentzeta omega?", top_k=1)
        granted = self.runtime.record_surface_consent(
            consent_memory["id"],
            status="granted",
            prompt="test grant",
            actor="test_user",
        )
        after = self.runtime.recall("Do you remember consentzeta omega?", top_k=1)

        self.assertNotIn(feedback_memory["id"], suppressed["retrieved_ids"])
        self.assertEqual(self.runtime.get_memory(feedback_memory["id"])["surface_feedback"]["negative_count"], 1)
        self.assertEqual(before["surface_action_v2"], "ask_consent")
        self.assertEqual(granted["status"], "granted")
        self.assertIn(consent_memory["id"], after["retrieved_ids"])
        self.assertEqual(after["surface_action_v2"], "answer_memory_question")

    def test_module07_surface_policy_limit_uses_priority_not_input_order(self) -> None:
        engine = SurfacePolicyEngine()
        plan = RecallPlan(
            query_text="Do you remember the important low score memory?",
            top_k=1,
            explicit_recall=True,
            surface_hint="allow_gist",
            query_intent="explicit_memory_lookup",
            budget=RecallBudget(token_budget=120),
        )
        low_priority_first = PackedMemory(
            score=0.4,
            memory={
                "id": "mem_low_priority_first",
                "type": "preference",
                "summary": "Low importance candidate arrived first.",
                "confidence": 0.2,
                "importance": 0.1,
                "relationship_impact": 0.0,
                "sensitivity": "low",
                "use_permission": "allow_implicit",
                "disclosure_permission": "gist_only",
                "surface_permission": "auto",
            },
            matched_backends=("test",),
            why_recalled="input order first",
        )
        high_priority_second = PackedMemory(
            score=0.5,
            memory={
                "id": "mem_high_priority_second",
                "type": "preference",
                "summary": "High importance candidate arrived second.",
                "confidence": 1.0,
                "importance": 1.0,
                "relationship_impact": 1.0,
                "sensitivity": "low",
                "use_permission": "allow_implicit",
                "disclosure_permission": "gist_only",
                "surface_permission": "auto",
            },
            matched_backends=("test",),
            why_recalled="higher surface priority",
        )

        decision = engine.decide(
            plan,
            [low_priority_first, high_priority_second],
            token_budget=120,
            limit=1,
        )

        self.assertEqual([item.memory_id for item in decision.selections], ["mem_high_priority_second"])
        self.assertEqual(decision.suppressed[0].memory_id, "mem_low_priority_first")
        self.assertEqual(decision.suppressed[0].suppression_reason, "rank_limit")

    def test_module05_default_recall_eval_covers_multi_memory_golden_set(self) -> None:
        eval_run = self.runtime.run_recall_eval()
        case_names = {case["case_name"] for case in eval_run["cases"]}

        self.assertIn("golden_gradual_preference_multi_memory", case_names)
        self.assertIn("golden_shared_episode", case_names)
        self.assertIn("golden_conflict_prefers_current_memory", case_names)
        self.assertIn("golden_graph_entity_related_memory", case_names)
        self.assertIn("golden_negative_empty_or_proactive_no_memory", case_names)
        self.assertEqual(eval_run["status"], "passed")

    def test_module05_qdrant_vector_backend_indexes_and_recalls(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        client = FakeQdrantClient()
        runtime = MemoryRuntime(
            Path(temp_dir.name) / "memory.db",
            vector_index_factory=lambda conn, provider: QdrantVectorIndex(
                conn,
                provider,
                url="http://qdrant.local",
                client=client,
            ),
        )
        self.addCleanup(runtime.close)

        memory = runtime.add_manual(
            "module five qdrant production vector alpha",
            memory_type="preference",
            tags=["qdrant_alpha"],
        )
        vector_row = runtime._conn.execute(
            "SELECT * FROM memory_vector_index WHERE memory_id = ?",
            (memory["id"],),
        ).fetchone()
        frame = runtime.recall("qdrant_alpha production vector", top_k=1)
        index_items = [
            item
            for item in runtime.list_index_outbox(limit=20)
            if item["memory_id"] == memory["id"]
        ]

        self.assertEqual(vector_row["backend"], "vector_qdrant")
        self.assertTrue(client.points)
        self.assertTrue(any(item["backend"] == "vector_qdrant" for item in index_items))
        self.assertEqual(frame["retrieved_ids"], [memory["id"]])
        self.assertEqual(frame["recall_plan"]["active_vector_backend"], "vector_qdrant")
        self.assertIn("vector_qdrant", frame["items"][0]["matched_backends"])

    def test_module05_vector_backend_failure_degrades_to_other_backends(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        client = FakeQdrantClient()
        runtime = MemoryRuntime(
            Path(temp_dir.name) / "memory.db",
            vector_index_factory=lambda conn, provider: QdrantVectorIndex(
                conn,
                provider,
                url="http://qdrant.local",
                client=client,
            ),
        )
        self.addCleanup(runtime.close)
        memory = runtime.add_manual(
            "module five qdrant fallback fts target",
            memory_type="preference",
            tags=["qdrant_fallback"],
        )

        client.fail_search = True
        frame = runtime.recall("qdrant_fallback target", top_k=1)
        trace = runtime.list_recall_traces(limit=1)[0]

        self.assertTrue(frame["degraded"])
        self.assertEqual(frame["retrieved_ids"], [memory["id"]])
        self.assertTrue(
            any(reason["backend"] == "vector_qdrant" for reason in frame["degradation_reasons"])
        )
        self.assertTrue(trace["degraded"])
        self.assertEqual(trace["cost"]["vector_backend"], "vector_qdrant")
        self.assertEqual(trace["cost"]["retrieval_errors"][0]["backend"], "vector_qdrant")

    def test_module05_cross_encoder_reranker_can_override_rrf_order(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        runtime = MemoryRuntime(
            Path(temp_dir.name) / "memory.db",
            reranker=FakeCrossEncoderReranker(preferred_text="preferred rerank target"),
        )
        self.addCleanup(runtime.close)
        runtime.add_manual(
            "module five rerank ordinary candidate shared token",
            memory_type="preference",
            tags=["rerank_case"],
        )
        preferred = runtime.add_manual(
            "module five preferred rerank target shared token",
            memory_type="preference",
            tags=["rerank_case"],
        )

        frame = runtime.recall("rerank_case shared token", top_k=1)
        trace = runtime.list_recall_traces(limit=1)[0]

        self.assertEqual(frame["retrieved_ids"], [preferred["id"]])
        self.assertEqual(frame["rerank"]["provider"], "http_cross_encoder")
        self.assertFalse(frame["rerank"]["degraded"])
        self.assertEqual(trace["cost"]["reranker"]["model"], "fake-cross-encoder")

    def test_module05_llm_reranker_uses_json_scores_and_records_usage(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        runtime = MemoryRuntime(
            Path(temp_dir.name) / "memory.db",
            reranker=FakeLLMReranker(preferred_text="llm preferred target"),
        )
        self.addCleanup(runtime.close)
        runtime.add_manual(
            "module five llm ordinary candidate shared token",
            memory_type="preference",
            tags=["llm_rerank_case"],
        )
        preferred = runtime.add_manual(
            "module five llm preferred target shared token",
            memory_type="preference",
            tags=["llm_rerank_case"],
        )

        frame = runtime.recall("llm_rerank_case shared token", top_k=1)
        trace = runtime.list_recall_traces(limit=1)[0]

        self.assertEqual(frame["retrieved_ids"], [preferred["id"]])
        self.assertEqual(frame["rerank"]["provider"], "openai_compatible_llm_reranker")
        self.assertEqual(frame["rerank"]["usage"]["total_tokens"], 11)
        self.assertEqual(trace["cost"]["reranker"]["model"], "fake-llm-reranker")

    def test_memory_delete_broadcasts_index_and_projector_tombstones(self) -> None:
        memory = self.runtime.add_manual(
            "delete propagation tombstone memory",
            memory_type="worldops_episode",
        )

        deleted = self.runtime.delete(memory["id"])
        propagations = self.runtime.list_delete_propagations(target_id=memory["id"], limit=50)
        target_types = {item["target_type"] for item in propagations}
        index_items = [
            item
            for item in self.runtime.list_index_outbox(limit=50)
            if item["memory_id"] == memory["id"] and item["action"] == "tombstone"
        ]
        projector_items = [
            item
            for item in self.runtime.list_projector_outbox(limit=50)
            if item["memory_id"] == memory["id"] and item["action"] == "tombstone"
        ]

        self.assertEqual(deleted["status"], "deleted")
        self.assertTrue({"memory_fts", "memory_vector_index", "memory_graph_index"}.issubset(target_types))
        self.assertTrue(
            {"relationship_projector", "worldops_projector", "persona_projector"}.issubset(target_types)
        )
        self.assertEqual({item["backend"] for item in index_items}, {"fts", "vector_local", "graph"})
        self.assertEqual(
            {item["projector_name"] for item in projector_items},
            {"relationship", "worldops", "persona"},
        )
        self.assertTrue(all(item["status"] == "processed" for item in index_items + projector_items))
        self.assertNotIn(
            memory["id"],
            self.runtime.recall("delete propagation tombstone memory")["retrieved_ids"],
        )

    def test_retention_worker_expires_evidence_and_disables_memory(self) -> None:
        evidence_id = self.runtime.add_evidence(
            source_type="test",
            content_summary="retention expired evidence",
            expires_at="2000-01-01T00:00:00+00:00",
        )
        candidate = self.runtime.propose(
            summary="retention expired evidence",
            memory_type="preference",
            evidence_ids=[evidence_id],
            confidence=0.95,
            reason="retention_test",
            allow_auto_commit=False,
        )
        memory = self.runtime.commit([candidate["candidate_id"]])["committed"][0]

        result = self.runtime.process_retention(now="2000-01-02T00:00:00+00:00")

        self.assertEqual(result["expired_evidence_count"], 1)
        self.assertEqual(self.runtime.get_evidence(evidence_id)["delete_reason"], "retention_evidence_expired")
        self.assertEqual(self.runtime.get_memory(memory["id"])["status"], "disabled")

    def test_forget_memory_deletes_memory_and_linked_evidence(self) -> None:
        memory = self.runtime.add_manual(
            "forget workflow memory",
            memory_type="preference",
        )
        evidence_id = self.runtime.evidence_for_memory(memory["id"])[0]["id"]

        request = self.runtime.forget_memory(memory["id"], actor="test_user")

        self.assertEqual(request["status"], "completed")
        self.assertEqual(self.runtime.get_memory(memory["id"])["status"], "deleted")
        self.assertEqual(self.runtime.get_evidence(evidence_id)["delete_reason"], "user_forget_request")
        self.assertEqual(len(self.runtime.list_forget_requests()), 1)

    def test_forget_evidence_redacts_raw_payload(self) -> None:
        evidence_id = self.runtime.add_evidence(
            source_type="test",
            content_summary="redaction evidence",
            raw_content="raw private text",
        )

        request = self.runtime.forget_evidence(evidence_id, mode="redact", actor="test_user")
        evidence = self.runtime.get_evidence(evidence_id)

        self.assertEqual(request["status"], "completed")
        self.assertEqual(evidence["visibility"], "redacted")
        self.assertIsNone(evidence["raw_content"])
        self.assertEqual(evidence["delete_reason"], "user_forget_request")

    def test_store_health_and_canonical_eval_cover_module04_invariants(self) -> None:
        self.runtime.add_manual(
            "canonical eval healthy memory",
            memory_type="preference",
        )

        health = self.runtime.store_health()
        eval_run = self.runtime.run_canonical_eval()

        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["index_outbox"]["pending"], 0)
        self.assertEqual(health["projectors"]["pending"], 0)
        self.assertEqual(eval_run["status"], "passed")
        self.assertEqual(eval_run["failed_cases"], 0)
        self.assertEqual(len(eval_run["cases"]), 4)

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
            "remember that I prefer async extractor design for my companion memory system",
            "I will keep that design preference.",
        )

        worker_result = self.runtime.run_extraction_once(provider=GoldenSetExtractorProvider())

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

        worker_result = self.runtime.run_extraction_once(provider=GoldenSetExtractorProvider())

        self.assertEqual(worker_result["status"], "completed")
        self.assertEqual(worker_result["job"]["job_id"], result["extraction_job"]["job_id"])
        self.assertEqual(len(worker_result["materialized"]["noop_candidates"]), 1)
        self.assertEqual(len(self.runtime.list_candidates()), before_candidates)

    def test_async_extractor_update_uses_related_memory_target(self) -> None:
        previous = self.runtime.add_manual(
            "User previously preferred minimal loops before broader architecture, but this product may need current best design.",
            memory_type="domain_principle",
            tags=["development_principle"],
        )
        result = self.runtime.ingest_turn(
            "I no longer want minimal loops; this product should pursue current best architecture.",
            "Understood.",
        )

        worker_result = self.runtime.run_extraction_once(
            provider=GoldenSetExtractorProvider(),
            auto_commit=False,
        )

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
                    "memory_type": "domain_principle",
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
                    "memory_type": "domain_principle",
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
        self.assertEqual(validated.candidates[0].memory_type, "domain_principle")
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
                                    "content": '{"candidates":[{"operation":"ADD" "memory_type":"domain_principle"}]}'
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
                                    '{"candidates":[{"operation":"ADD","memory_type":"domain_principle",'
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

        worker_result = self.runtime.run_extraction_once(provider=GoldenSetExtractorProvider())
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
                    provider=GoldenSetExtractorProvider(),
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

        memory = self.runtime.add_manual("用户偏好 console evidence 测试")
        evidence_id = self.runtime.evidence_for_memory(memory["id"])[0]["id"]
        output, _ = console.execute("evidence-list active --limit 5")
        self.assertIn(evidence_id, output)

        output, _ = console.execute(f"evidence-show {evidence_id}")
        self.assertIn("console evidence", output)

        output, _ = console.execute("store-audit")
        self.assertIn("counts", output)

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

        evidence_memory = self.runtime.add_manual("用户偏好命令 evidence 测试")
        evidence_id = self.runtime.evidence_for_memory(evidence_memory["id"])[0]["id"]
        output, _ = handle_command(
            "/memory-evidence-items active --limit 5",
            state,
            memory_admin=self.runtime,
        )
        self.assertIn(evidence_id, output)

        output, _ = handle_command(
            f"/memory-evidence-detail {evidence_id}",
            state,
            memory_admin=self.runtime,
        )
        self.assertIn("命令 evidence", output)

        output, _ = handle_command("/memory-store-audit", state, memory_admin=self.runtime)
        self.assertIn("counts", output)

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

    def test_recall_planner_v2_schema_and_subqueries(self) -> None:
        plan = self.runtime.plan_recall(
            "Do you remember what we said last time about my writing workflow?",
            turn_frame={"scene": "planning", "recent_topics": ["writing workflow", "character arcs"]},
            top_k=5,
        )

        self.assertEqual(plan["planner_version"], "recall_planner_v2")
        self.assertEqual(plan["decision"], "deep_recall")
        self.assertEqual(plan["query_intent"], "explicit_memory_lookup")
        self.assertTrue(plan["explicit_recall"])
        self.assertIn("fts", plan["backends"])
        self.assertIn("vector", plan["backends"])
        self.assertTrue(plan["subqueries"])
        self.assertIn("budget", plan)
        self.assertTrue(plan["decision_reasons"])

    def test_salient_terms_keeps_broad_my_context(self) -> None:
        self.assertEqual(extract_salient_terms("我的边界感需要更柔和"), ["边界感需要更柔和"])
        self.assertEqual(extract_salient_terms("我的陪伴节奏和安全感很重要"), ["陪伴节奏和安全感很重要"])
        self.assertEqual(extract_salient_terms("我的猫跑了"), ["猫跑了"])

    def test_prefetch_recall_writes_plan_cache_without_prompt_items(self) -> None:
        result = self.runtime.prefetch_recall(
            "next steps for my writing workflow design",
            turn_frame={"scene": "planning", "recent_topics": ["writing workflow"]},
        )

        self.assertEqual(result["status"], "cached")
        self.assertEqual(result["plan"]["decision"], "prefetch_only")
        self.assertEqual(result["plan"]["budget"]["token_budget"], 0)
        cached = self.runtime.list_recall_plan_cache()
        self.assertEqual(cached[0]["cache_key"], result["cache_key"])
        self.assertEqual(cached[0]["plan"]["decision"], "prefetch_only")

        cache_hit = self.runtime.prefetch_recall(
            "next steps for my writing workflow design",
            turn_frame={"scene": "planning", "recent_topics": ["writing workflow"]},
        )
        refreshed = self.runtime.list_recall_plan_cache()
        self.assertEqual(cache_hit["status"], "cache_hit")
        self.assertEqual(cache_hit["cache_key"], result["cache_key"])
        self.assertEqual(len(refreshed), 1)
        self.assertEqual(refreshed[0]["created_at"], cached[0]["created_at"])

    def test_recall_records_planner_trace(self) -> None:
        frame = self.runtime.recall("Do you remember our design workflow?", top_k=3)

        self.assertTrue(frame["recall_trace_id"])
        self.assertEqual(frame["recall_plan"]["planner_version"], "recall_planner_v2")
        traces = self.runtime.list_recall_plan_traces()
        self.assertEqual(traces[0]["recall_trace_id"], frame["recall_trace_id"])
        self.assertEqual(traces[0]["decision"], frame["recall_plan"]["decision"])
        self.assertTrue(traces[0]["subqueries"])

    def test_planner_eval_harness_runs_default_golden_set(self) -> None:
        result = self.runtime.run_planner_eval()

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["summary"]["case_count"], 8)
        self.assertEqual(result["summary"]["decision_accuracy"], 1.0)
        self.assertEqual(result["summary"]["intent_accuracy"], 1.0)
        self.assertEqual(result["summary"]["trace_completeness"], 1.0)

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
