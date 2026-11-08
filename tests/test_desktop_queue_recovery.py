"""Single-event recovery with real temporary Memory stores and no model calls."""
import json

import pytest

from sora_sandbox.memory_runtime import MemoryRuntime
from tests import test_desktop_commands as command_helpers


@pytest.fixture
def memory(tmp_path):
    runtime = MemoryRuntime(tmp_path / "memory.db", owner_user_id="user-a", agent_id="sora")
    runtime.import_legacy_memories([{"id": "fixture-memory", "type": "profile", "content": "测试中的低敏资料", "tags": []}])
    yield runtime
    runtime.close()


def fail_event(memory, kind):
    store = memory.index_outbox if kind == "index" else memory.projector_bus
    event = store.list_items(limit=1)[0]
    table = "memory_index_outbox" if kind == "index" else "memory_projector_outbox"
    memory._conn.execute(f"UPDATE {table} SET status='dead_letter',attempts=3,error='private-error-sentinel',error_type='timeout',next_attempt_at=NULL WHERE id=?", (event["id"],))
    memory._conn.commit()
    return memory.queue_record_snapshot(kind=kind, event_id=event["id"])


@pytest.mark.parametrize("kind", ["index", "projection"])
def test_single_requeue_preserves_identity_payload_cumulative_attempts_and_is_idempotent(memory, kind):
    snapshot = fail_event(memory, kind)
    assert snapshot["can_requeue"]
    original = snapshot["record"]
    request = {"kind": kind, "event_id": original["id"], "expected_revision": snapshot["revision"]}
    receipt = memory.requeue_queue_record(**request)
    current = memory.queue_record_snapshot(kind=kind, event_id=original["id"])["record"]
    assert receipt["status"] == "requeued"
    assert current["status"] == "pending"
    assert current["attempts"] == 3
    assert current["max_attempts"] == 6
    assert current["payload"] == original["payload"]
    assert current["payload_hash"] == original["payload_hash"]
    assert memory.requeue_queue_record(**request) == receipt
    events = memory.quality_ops.list_quality_events(event_type="memory_queue_requeue")
    assert len(events) == 1
    assert "private-error-sentinel" not in json.dumps(events)


def test_audit_failure_rolls_back_the_entire_requeue(memory):
    snapshot = fail_event(memory, "index")
    memory._conn.executescript("""CREATE TEMP TRIGGER fail_requeue_audit BEFORE INSERT ON memory_quality_event
        WHEN NEW.event_type='memory_queue_requeue' BEGIN SELECT RAISE(ABORT,'injected audit failure'); END;""")
    with pytest.raises(Exception, match="injected audit failure"):
        memory.requeue_queue_record(kind="index", event_id=snapshot["record"]["id"], expected_revision=snapshot["revision"])
    assert memory.queue_record_snapshot(kind="index", event_id=snapshot["record"]["id"])["record"] == snapshot["record"]
    assert not memory._conn.in_transaction


def test_worker_changes_and_foreign_owner_are_fenced(memory, tmp_path):
    snapshot = fail_event(memory, "index")
    other = MemoryRuntime(tmp_path / "memory.db", owner_user_id="user-a", agent_id="sora")
    try:
        other._conn.execute("UPDATE memory_index_outbox SET status='processed' WHERE id=?", (snapshot["record"]["id"],))
        other._conn.commit()
        with pytest.raises(ValueError, match="changed"):
            memory.requeue_queue_record(kind="index", event_id=snapshot["record"]["id"], expected_revision=snapshot["revision"])
    finally:
        other.close()
    foreign = MemoryRuntime(tmp_path / "memory.db", owner_user_id="foreign-user", agent_id="sora")
    try:
        assert foreign.list_queue_records(kind="index", status="all") == []
        with pytest.raises(ValueError, match="owner"):
            foreign.queue_record_snapshot(kind="index", event_id=snapshot["record"]["id"])
    finally:
        foreign.close()


def test_historical_projection_cannot_undo_a_later_privacy_change(memory):
    snapshot = fail_event(memory, "projection")
    memory.disable("fixture-memory")
    current = memory.queue_record_snapshot(kind="projection", event_id=snapshot["record"]["id"])
    assert current["can_requeue"]
    with pytest.raises(ValueError, match="changed"):
        memory.requeue_queue_record(kind="projection", event_id=snapshot["record"]["id"], expected_revision=snapshot["revision"])
    assert memory.get_memory("fixture-memory")["status"] == "disabled"
    memory.requeue_queue_record(kind="projection", event_id=current["record"]["id"], expected_revision=current["revision"])
    delivered = []
    class RecordingAdapter:
        def deliver(self, event):
            delivered.append(event)
            return {"status": "applied"}
    memory.projector_bus.register_adapter(current["record"]["projector_name"], RecordingAdapter())
    memory.process_projector_outbox(limit=100)
    assert all(event["memory"]["status"] != "active" for event in delivered)
    assert memory.projector_bus.get_item(current["record"]["id"])["result"]["status"] == "rejected_policy"


def test_real_maintenance_bridge_requires_preview_confirmation_and_redacts_payloads():
    case = command_helpers.DesktopCommandTests("test_preview_execute_receipt_and_idempotency_are_real")
    case.setUp()
    try:
        case.memory.import_legacy_memories([{"id": "fixture-command-memory", "type": "profile", "content": "private-memory-sentinel", "tags": []}])
        snapshot = fail_event(case.memory, "index")
        params = {"kind": "index", "event_id": snapshot["record"]["id"], "expected_revision": snapshot["revision"]}
        registry = case.client.get("/v1/debug/commands", headers=case.headers)
        assert registry.status_code == 200
        command = next(item for item in registry.json()["commands"] if item["command_id"] == "memory.event.requeue")
        assert command["available"] and command["default_params"] is None
        preview = case.preview("memory.event.requeue", params, "queue-request-one")
        assert preview.status_code == 200, preview.text
        assert preview.json()["rollback_type"] == "inspect_queue_receipt"
        assert "private-memory-sentinel" not in preview.text
        assert "private-error-sentinel" not in preview.text
        assert case.execute(preview.json(), confirmed=False).status_code == 422
        executed = case.execute(preview.json())
        assert executed.status_code == 200, executed.text
        assert executed.json()["result_summary"] == {"status": "requeued", "requeued_count": 1}
        assert case.execute(preview.json()).json()["audit_event_id"] == executed.json()["audit_event_id"]
    finally:
        case.doCleanups()


def test_old_index_tombstone_does_not_remove_a_restored_memory_index(memory):
    memory.disable("fixture-memory")
    tombstones = [item for item in memory.index_outbox.list_items(limit=100) if item["action"] != "upsert" and item["backend"] == "fts"]
    event_id = tombstones[0]["id"]
    memory.restore("fixture-memory")
    memory._conn.execute("UPDATE memory_index_outbox SET status='dead_letter',attempts=3 WHERE id=?", (event_id,))
    memory._conn.commit()
    snapshot = memory.queue_record_snapshot(kind="index", event_id=event_id)
    memory.requeue_queue_record(kind="index", event_id=event_id, expected_revision=snapshot["revision"])
    memory.index_outbox.process_pending(limit=100)
    assert memory.index_outbox.get_item(event_id)["result"]["status"] == "rejected_policy"
    assert memory._conn.execute("SELECT COUNT(*) FROM memory_fts WHERE memory_id='fixture-memory'").fetchone()[0] == 1
