import sqlite3
from contextlib import closing

import pytest

from sora_sandbox.memory_embedding import DeterministicEmbeddingProvider
from sora_sandbox.memory_recall_planner import RuleRecallPlanner
from sora_sandbox.memory_reranker import FeatureMemoryReranker
from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.memory_surface_policy import DeterministicSurfacePolicyProvider, SurfacePolicyEngine


@pytest.fixture
def memory(tmp_path):
    runtime = MemoryRuntime(
        tmp_path / "memory.db",
        recall_planner=RuleRecallPlanner(),
        embedding_provider=DeterministicEmbeddingProvider(),
        reranker=FeatureMemoryReranker(),
        surface_policy=SurfacePolicyEngine(provider=DeterministicSurfacePolicyProvider()),
    )
    yield runtime
    runtime.close()


def candidate(memory, summary):
    evidence = memory.add_evidence(source_type="manual", content_summary=summary)
    return memory.propose(
        summary=summary,
        memory_type="preference",
        evidence_ids=[evidence],
        confidence=0.95,
        reason="isolated commit integrity fixture",
        allow_auto_commit=False,
    )["candidate_id"]


def record_counts(connection):
    tables = (
        "memory_record", "memory_evidence", "memory_revision",
        "memory_index_outbox", "memory_projector_outbox",
    )
    return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}


def test_failed_batch_cannot_be_committed_by_a_later_write_and_can_retry(memory):
    first = candidate(memory, "Synthetic first preference")
    second = candidate(memory, "Synthetic second preference")
    before = record_counts(memory._conn)
    memory._conn.execute(
        """CREATE TRIGGER fail_second_commit BEFORE UPDATE OF status ON memory_candidate
        WHEN NEW.status='committed' AND NEW.id='%s'
        BEGIN SELECT RAISE(ABORT, 'injected_commit_failure'); END""" % second
    )
    memory._conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected_commit_failure"):
        memory.commit([first, second])
    assert not memory._conn.in_transaction
    memory.add_evidence(source_type="manual", content_summary="Unrelated later write")
    with closing(sqlite3.connect(memory.path)) as reader:
        assert record_counts(reader) == before
        assert reader.execute("SELECT status FROM memory_candidate WHERE id IN (?, ?)", (first, second)).fetchall() == [("pending",), ("pending",)]

    memory._conn.execute("DROP TRIGGER fail_second_commit")
    memory._conn.commit()
    assert len(memory.commit([first, second])["committed"]) == 2
    after = record_counts(memory._conn)
    memory.commit([first, second])
    assert record_counts(memory._conn) == after


def test_unknown_candidate_rolls_back_earlier_candidates_in_the_same_batch(memory):
    first = candidate(memory, "Synthetic batch preference")
    before = record_counts(memory._conn)
    with pytest.raises(ValueError):
        memory.commit([first, "cand_missing_fixture"])
    memory.add_evidence(source_type="manual", content_summary="Separate later write")
    assert record_counts(memory._conn) == before
    assert memory.get_candidate(first)["status"] == "pending"
