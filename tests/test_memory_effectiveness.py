import sqlite3

import pytest

from sora_sandbox.memory_context_packer import MemoryContextPacker, PackedMemory, _estimate_tokens, _truncate_to_budget
from sora_sandbox.memory_embedding import DeterministicEmbeddingProvider
from sora_sandbox.memory_recall_planner import RuleRecallPlanner
from sora_sandbox.memory_reranker import FeatureMemoryReranker
from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.memory_surface_policy import DeterministicSurfacePolicyProvider, SurfacePolicyEngine


@pytest.fixture
def memory(tmp_path):
    runtime = MemoryRuntime(tmp_path / "memory.db", recall_planner=RuleRecallPlanner(),
        embedding_provider=DeterministicEmbeddingProvider(), reranker=FeatureMemoryReranker(),
        surface_policy=SurfacePolicyEngine(provider=DeterministicSurfacePolicyProvider()))
    yield runtime
    runtime.close()


def proposal(memory, target, text="用户周末喜欢不加糖的茉莉花茶", **kwargs):
    evidence = memory.add_evidence(source_type="manual", content_summary=text)
    return memory.propose(summary=text, memory_type="preference", evidence_ids=[evidence],
        operation="MERGE", target_memory_id=target["id"], tags=["jasmine"], importance=.9,
        confidence=.95, reason="isolated semantic merge fixture", allow_auto_commit=False, **kwargs)


def test_merge_enhances_content_preserves_privacy_and_rolls_back(memory):
    original = memory.add_manual("用户周末喜欢茶", memory_type="preference", tags=["weekend"])
    memory.hide_memory(original["id"])
    original = memory.get_memory(original["id"])
    candidate = proposal(memory, original)
    merged = memory.commit([candidate["candidate_id"]])["committed"][0]
    assert merged["summary"] == candidate["summary"]
    assert set(merged["tags"]) == {"weekend", "jasmine"}
    assert merged["importance"] == .9
    assert set(merged["evidence_ids"]) == set(original["evidence_ids"] + candidate["evidence_ids"])
    for key in ("status", "retrieval_permission", "disclosure_permission", "surface_permission", "use_permission", "retention_policy"):
        assert merged[key] == original[key]
    count = memory._conn.execute("SELECT count(*) FROM memory_revision").fetchone()[0]
    memory.commit([candidate["candidate_id"]])
    assert memory._conn.execute("SELECT count(*) FROM memory_revision").fetchone()[0] == count
    memory.rollback_committed_candidate(candidate["candidate_id"])
    restored = memory.get_memory(original["id"])
    for key in ("summary", "tags", "importance", "evidence_ids"):
        assert restored[key] == original[key]
    assert memory.get_candidate(candidate["candidate_id"])["status"] == "rolled_back"


def test_merge_stale_proposal_and_later_rollback_conflict(memory):
    target = memory.add_manual("用户周末喜欢茶", memory_type="preference")
    first = proposal(memory, target)
    stale = proposal(memory, target, "用户周末喜欢温热的茶")
    memory.commit([first["candidate_id"]])
    with pytest.raises(ValueError, match="target changed"):
        memory.commit([stale["candidate_id"]])
    newer = proposal(memory, memory.get_memory(target["id"]), "用户周末喜欢温热的无糖茉莉花茶")
    memory.commit([newer["candidate_id"]])
    with pytest.raises(ValueError, match="later content change"):
        memory.rollback_committed_candidate(first["candidate_id"])
    assert memory.get_memory(target["id"])["summary"] == newer["summary"]


def test_merge_failure_rolls_back_content_and_evidence(memory):
    target = memory.add_manual("用户周末喜欢茶", memory_type="preference")
    candidate = proposal(memory, target)
    memory._conn.execute("""CREATE TRIGGER merge_failure BEFORE UPDATE OF status ON memory_candidate
        WHEN NEW.status='committed' BEGIN SELECT RAISE(ABORT, 'merge_failure'); END""")
    memory._conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="merge_failure"):
        memory.commit([candidate["candidate_id"]])
    memory.add_evidence(source_type="manual", content_summary="a later independent write")
    assert memory.get_memory(target["id"])["summary"] == target["summary"]
    assert memory.get_memory(target["id"])["evidence_ids"] == target["evidence_ids"]
    assert memory.get_candidate(candidate["candidate_id"])["status"] == "pending"


def test_merge_rollback_keeps_preexisting_evidence_in_the_proposal(memory):
    target = memory.add_manual("用户周末喜欢茶", memory_type="preference")
    candidate = proposal(memory, target)
    with memory._lock, memory._conn:
        import json
        memory._conn.execute("UPDATE memory_candidate SET evidence_ids_json=? WHERE id=?",
            (json.dumps(target["evidence_ids"] + candidate["evidence_ids"]), candidate["candidate_id"]))
    memory.commit([candidate["candidate_id"]])
    memory.rollback_committed_candidate(candidate["candidate_id"])
    assert memory.get_memory(target["id"])["evidence_ids"] == target["evidence_ids"]


def test_semantic_merge_updates_the_real_index_document(memory):
    target = memory.add_manual("用户周末喜欢茶", memory_type="preference")
    candidate = proposal(memory, target)
    memory.commit([candidate["candidate_id"]])
    memory.process_memory_changes()
    row = memory._conn.execute("SELECT search_text FROM memory_fts WHERE memory_id=?", (target["id"],)).fetchone()
    assert memory._conn.execute("SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ? AND memory_id=?", ('"茉莉"', target["id"])).fetchone()
    assert "jasmine" in row["search_text"]


@pytest.mark.parametrize("kind", ["inactive", "owner", "deleted_evidence", "sensitive", "tombstone"])
def test_merge_rejects_invalid_sources(memory, kind):
    target = memory.add_manual("用户周末喜欢茶", memory_type="preference")
    text = "用户周末喜欢不加糖的茉莉花茶" if kind != "sensitive" else "用户病历里的诊断信息"
    candidate = proposal(memory, target, text)
    if kind == "inactive":
        memory._conn.execute("UPDATE memory_record SET status='disabled' WHERE id=?", (target["id"],))
    elif kind == "owner":
        memory._conn.execute("UPDATE memory_record SET owner_user_id='another-user' WHERE id=?", (target["id"],))
    elif kind == "deleted_evidence":
        memory._conn.execute("UPDATE evidence SET deleted_at='2026-11-08' WHERE id=?", (candidate["evidence_ids"][0],))
    elif kind == "tombstone":
        memory.lifecycle.record_tombstone(target_type="memory", target_id=target["id"], content=text, reason="fixture forget", actor="test")
    memory._conn.commit()
    with pytest.raises(ValueError):
        memory.commit([candidate["candidate_id"]])
    assert memory.get_candidate(candidate["candidate_id"])["status"] == "pending"


def test_context_budget_is_a_bound_on_returned_text_including_tiny_and_zero(memory):
    target = memory.add_manual("中英文 mixed gist " * 80, memory_type="preference")
    candidates = [PackedMemory(1., target, ("fts",), "因为匹配了当前问题 " * 20)]
    for budget in range(0, 160):
        result = MemoryContextPacker().pack(candidates, token_budget=budget, explicit_recall=True)
        actual = sum(_estimate_tokens(item["gist"]) + _estimate_tokens(item["why_recalled"]) + 12 for item in result["items"])
        assert actual == result["token_used"] <= budget
        assert sum(result["context_frame"]["budget"]["pool_used"].values()) == actual
        if budget <= 12:
            assert result["items"] == []
    for budget in range(0, 25):
        text = _truncate_to_budget("中英文 mixed gist " * 80, budget)
        assert _estimate_tokens(text) <= budget
        assert _truncate_to_budget(text, budget) == text
