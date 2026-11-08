"""Isolated real SQLite failure injection; no model or user-data operations."""
import sqlite3
import os
import subprocess
import sys
import threading
from unittest.mock import patch
from contextlib import closing

import pytest

from tests.test_memory_commit_integrity import memory, candidate
from sora_sandbox.memory_runtime import MemoryRuntime


def review(memory, summary="Synthetic reviewed preference"):
    item = memory.propose(summary=summary, memory_type="preference",
        evidence_ids=[memory.add_evidence(source_type="test", content_summary=summary)],
        reason="isolated_review_consistency")
    return memory.list_review_items()[0], item["candidate_id"]


def test_failed_batch_never_calls_external_listener(memory):
    observed = []
    memory.register_memory_change_listener("test-owner", lambda item, **kw: observed.append(item["id"]))
    first = candidate(memory, "Synthetic uncommitted preference")
    with pytest.raises(ValueError):
        memory.commit([first, "cand_missing_fixture"])
    assert observed == []
    assert memory.list_memories() == []


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_review_failure_rolls_back_canonical_candidate_and_review(memory, action):
    item, candidate_id = review(memory)
    revision = memory.review_revision(item["review_id"])
    # Only the final reviewer write fails; nested canonical helpers must not commit first.
    memory._conn.execute("""CREATE TRIGGER fail_review_resolution
        BEFORE UPDATE ON memory_review_item WHEN NEW.resolved_by='fixture-reviewer'
        AND NEW.resolution != 'committed'
        BEGIN SELECT RAISE(ABORT,'injected_resolution_failure'); END""")
    memory._conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected_resolution_failure"):
        memory.resolve_review_checked(item["review_id"], action=action,
            expected_revision=revision, actor="fixture-reviewer")
    assert not memory._conn.in_transaction
    memory.add_evidence(source_type="test", content_summary="Separate later write")
    with closing(sqlite3.connect(memory.path)) as reader:
        assert reader.execute("SELECT status FROM memory_candidate WHERE id=?", (candidate_id,)).fetchone()[0] == "needs_review"
        assert reader.execute("SELECT status FROM memory_review_item WHERE id=?", (item["review_id"],)).fetchone()[0] == "open"
        assert reader.execute("SELECT count(*) FROM memory_record").fetchone()[0] == 0


def test_listener_reads_committed_source_using_an_independent_connection(memory):
    observed = []
    def listener(item, **kwargs):
        with closing(sqlite3.connect(memory.path)) as reader:
            observed.append(reader.execute("SELECT status FROM memory_record WHERE id=?", (item["id"],)).fetchone())
    memory.register_memory_change_listener("read-committed-owner", listener)
    first = candidate(memory, "Synthetic visible-after-commit preference")
    memory.commit([first])
    assert observed == [("active",)]


class ExternalSQLiteProjection:
    """Explicit second-database owner fixture with durable event deduplication."""
    def __init__(self, path, canonical):
        self.path, self.canonical = path, canonical
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS received(event_id TEXT PRIMARY KEY, memory_id TEXT)")
        self.calls = 0

    def deliver(self, event):
        self.calls += 1
        with closing(sqlite3.connect(self.canonical)) as reader:
            assert reader.execute("SELECT status FROM memory_record WHERE id=?", (event["memory"]["id"],)).fetchone() is not None
        with sqlite3.connect(self.path) as connection:
            inserted = connection.execute("INSERT OR IGNORE INTO received VALUES (?,?)", (event["event_id"], event["memory"]["id"])).rowcount
        return {"status": "applied" if inserted else "skipped_duplicate"}


def test_rolled_back_profile_never_leaks_into_second_database(memory):
    adapter = ExternalSQLiteProjection(memory.path.parent / "domain.db", memory.path)
    memory.projector_bus.register_adapter("persona", adapter)
    evidence = memory.add_evidence(source_type="test", content_summary="Synthetic profile")
    first = memory.propose(summary="Synthetic profile", memory_type="profile", evidence_ids=[evidence], reason="test", allow_auto_commit=False)["candidate_id"]
    with pytest.raises(ValueError):
        memory.commit([first, "cand_missing_fixture"])
    assert adapter.calls == 0
    memory.commit([first])
    assert adapter.calls == 1


@pytest.mark.parametrize("action", ["approve", "reject", "defer"])
def test_review_audit_failure_rolls_back_and_exact_replay_returns_same_audit(memory, action):
    item, candidate_id = review(memory)
    revision = memory.review_revision(item["review_id"])
    memory._conn.execute("""CREATE TRIGGER fail_review_audit BEFORE INSERT ON memory_review_resolution_audit
        BEGIN SELECT RAISE(ABORT,'injected_review_audit_failure'); END""")
    memory._conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected_review_audit_failure"):
        memory.resolve_review_checked(item["review_id"], action=action, expected_revision=revision, actor="review-owner")
    assert memory.get_candidate(candidate_id)["status"] == "needs_review"
    assert memory.get_review_item(item["review_id"])["status"] == "open"
    assert memory.list_memories() == []
    assert not memory._conn.in_transaction
    memory._conn.execute("DROP TRIGGER fail_review_audit")
    memory._conn.commit()
    first = memory.resolve_review_checked(item["review_id"], action=action, expected_revision=revision, actor="review-owner")
    second = memory.resolve_review_checked(item["review_id"], action=action, expected_revision=revision, actor="review-owner")
    assert second["status"] == "already_resolved" and second["audit_id"] == first["audit_id"]
    with pytest.raises(ValueError):
        memory.resolve_review_checked(item["review_id"], action=action, expected_revision="0" * 64, actor="review-owner")
    assert memory._conn.execute("SELECT count(*) FROM memory_review_resolution_audit").fetchone()[0] == 1


def test_nested_owner_failure_discards_inner_commit_and_all_delivery_intents(memory):
    observed = []
    memory.register_memory_change_listener("nested-owner", lambda item, **kw: observed.append(item["id"]))
    first = candidate(memory, "Synthetic nested owner preference")
    with pytest.raises(RuntimeError):
        with memory._lock, memory._conn:
            memory.commit([first])
            assert observed == []
            raise RuntimeError("outer_unit_failed")
    memory.process_memory_changes()
    assert observed == [] and memory.list_memories() == []
    assert memory._conn.execute("SELECT count(*) FROM memory_change_delivery").fetchone()[0] == 0


def test_background_retry_preserves_intent_and_does_not_store_exception_text(memory):
    applied = threading.Event()
    calls = []
    def listener(item, **kwargs):
        calls.append(item["id"])
        if len(calls) == 1:
            raise RuntimeError("PRIVATE_ERROR_BODY_MUST_NOT_PERSIST")
        applied.set()
    memory.register_memory_change_listener("retry-owner", listener)
    item = memory.add_manual("Synthetic retry preference")
    row = memory._conn.execute("SELECT * FROM memory_change_delivery").fetchone()
    assert row["status"] == "pending" and row["attempts"] == 1 and row["error_code"] == "RuntimeError"
    assert "PRIVATE_ERROR" not in str(dict(row))
    memory.change_delivery.start()
    try:
        assert applied.wait(5)
    finally:
        memory.change_delivery.stop()
    row = memory._conn.execute("SELECT * FROM memory_change_delivery").fetchone()
    assert row["status"] == "delivered" and row["attempts"] == 2
    assert calls == [item["id"], item["id"]]
    assert not memory.change_delivery.snapshot()["running"]


def child(code, path):
    environment = {**os.environ, "PYTHONPATH": str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src")}
    return subprocess.run([sys.executable, "-c", code, str(path)], env=environment, capture_output=True, timeout=20)


def test_process_death_after_commit_keeps_listener_intent_for_restart(memory):
    # The child kills itself inside delivery, after canonical SQLite commit.
    result = child("""
import os,sys
from sora_sandbox.memory_runtime import MemoryRuntime
runtime=MemoryRuntime(__import__('pathlib').Path(sys.argv[1]))
runtime.register_memory_change_listener('crash-owner',lambda *a,**k:os._exit(73))
runtime.add_manual('Synthetic process death preference')
""", memory.path)
    assert result.returncode == 73, result.stderr
    assert len(memory.list_memories()) == 1
    observed = []
    memory.register_memory_change_listener("crash-owner", lambda item, **kw: observed.append(item["id"]))
    memory.process_memory_changes()
    assert len(observed) == 1
    assert memory._conn.execute("SELECT status FROM memory_change_delivery").fetchone()[0] == "delivered"


def test_process_death_during_approval_rolls_back_whole_review(memory):
    item, candidate_id = review(memory)
    result = child("""
import os,sys
from pathlib import Path
from sora_sandbox.memory_runtime import MemoryRuntime
r=MemoryRuntime(Path(sys.argv[1]))
r._conn.create_function('fixture_crash',0,lambda:os._exit(74))
r._conn.execute(\"CREATE TEMP TRIGGER crash_review BEFORE UPDATE ON memory_review_item WHEN NEW.resolution='approved' BEGIN SELECT fixture_crash(); END\")
r._conn.commit()
item=r.list_review_items()[0]
r.resolve_review_checked(item['review_id'],action='approve',expected_revision=r.review_revision(item['review_id']),actor='crash-reviewer')
""", memory.path)
    assert result.returncode == 74, result.stderr
    assert memory.get_candidate(candidate_id)["status"] == "needs_review"
    assert memory.get_review_item(item["review_id"])["status"] == "open"
    assert memory.list_memories() == []


def test_lost_projection_ack_replays_same_event_without_duplicate_domain_effect(memory):
    adapter = ExternalSQLiteProjection(memory.path.parent / "ack-domain.db", memory.path)
    memory.projector_bus.register_adapter("persona", adapter)
    memory._conn.execute("""CREATE TRIGGER lose_projection_ack BEFORE UPDATE ON memory_projector_outbox
        WHEN NEW.status='processed' BEGIN SELECT RAISE(ABORT,'injected_ack_loss'); END""")
    memory._conn.commit()
    item = memory.add_manual("Synthetic acknowledged profile", memory_type="profile")
    row = memory.list_projector_outbox()[0]
    assert row["status"] != "processed" and adapter.calls == 1
    assert memory.get_memory(item["id"])["status"] == "active"
    memory._conn.execute("DROP TRIGGER lose_projection_ack")
    memory._conn.execute("UPDATE memory_projector_outbox SET status='pending',next_attempt_at=NULL")
    memory._conn.commit()
    memory.process_memory_changes()
    after = memory.list_projector_outbox()[0]
    assert after["id"] == row["id"] and after["status"] == "processed"
    assert after["result"]["status"] == "skipped_duplicate"
    with closing(sqlite3.connect(adapter.path)) as reader:
        assert reader.execute("SELECT count(*) FROM received").fetchone()[0] == 1


def test_stale_listener_tombstone_cannot_override_later_restore(memory):
    observed = []
    memory.register_memory_change_listener("stale-owner", lambda item, **kw: observed.append((item["status"], kw["action"])))
    with patch.object(memory._conn, "after_commit", None):
        item = memory.add_manual("Synthetic restored preference")
        memory.disable(item["id"])
        memory.restore(item["id"])
    assert observed == []
    memory.process_memory_changes()
    assert observed == [("active", "upsert")]
    statuses = [row[0] for row in memory._conn.execute("SELECT status FROM memory_change_delivery")]
    assert statuses.count("rejected_policy") == 2 and statuses.count("delivered") == 1


def test_failed_forget_receipt_preserves_memory_evidence_and_external_state(memory):
    item = memory.add_manual("Synthetic forget integrity preference")
    observed = []
    memory.register_memory_change_listener("forget-owner", lambda item, **kw: observed.append(kw["action"]))
    memory._conn.execute("""CREATE TRIGGER fail_forget_receipt BEFORE UPDATE ON memory_forget_request
        WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT,'injected_forget_failure'); END""")
    memory._conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected_forget_failure"):
        memory.forget_memory(item["id"])
    assert memory.get_memory(item["id"])["status"] == "active"
    assert all(not evidence["deleted_at"] for evidence in memory.evidence_for_memory(item["id"]))
    assert observed == []
    assert not memory._conn.in_transaction


def test_failed_correction_cannot_leave_successor_evidence_or_external_effects(memory):
    item = memory.add_manual("Synthetic original preference")
    tables = ("memory_record", "memory_revision", "evidence", "memory_candidate", "memory_index_outbox", "memory_projector_outbox")
    before = {table: memory._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}
    observed = []
    memory.register_memory_change_listener("correct-owner", lambda *args, **kw: observed.append(kw["action"]))
    memory._conn.execute("""CREATE TRIGGER fail_correction BEFORE UPDATE ON memory_candidate
        WHEN NEW.status='committed' BEGIN SELECT RAISE(ABORT,'injected_correction_failure'); END""")
    memory._conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected_correction_failure"):
        memory.revise(item["id"], "Synthetic corrected preference")
    assert memory.get_memory(item["id"])["status"] == "active"
    assert before == {table: memory._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}
    assert observed == [] and not memory._conn.in_transaction
