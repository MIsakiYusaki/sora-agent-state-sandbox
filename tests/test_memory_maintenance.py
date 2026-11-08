from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
import threading
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from sora_sandbox.conversation_gateway.desktop_maintenance import install_maintenance_routes
from sora_sandbox.conversation_gateway.host import _authenticate, _require_memory_owner, _require_protocol
from sora_sandbox.conversation_gateway.security import LocalCapabilityProvider
from sora_sandbox.conversation_gateway.service import ConversationGatewayService, GatewayServiceError
from sora_sandbox.memory_lifecycle_scheduler import MemoryLifecycleScheduler
from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.memory_system import MemorySystem


@pytest.fixture
def memory(tmp_path):
    system = MemorySystem(MemoryRuntime(tmp_path / "memory.db", owner_user_id="alice", agent_id="sora"))
    yield system
    system.close()


def test_periodic_batch_eventually_scores_older_memories_and_checkpoints_restart(memory):
    for n in range(5):
        memory.add_manual(f"isolated preference {n}", memory_type="preference")
    now = datetime.now(timezone.utc)
    scheduler = MemoryLifecycleScheduler(memory.runtime, interval_seconds=60, batch_limit=2)
    assert scheduler.run_once(now=now.isoformat())["scored_count"] == 2
    assert MemoryLifecycleScheduler(memory.runtime).run_once(now=now.isoformat())["status"] == "waiting"
    scheduler.run_once(now=(now + timedelta(seconds=61)).isoformat())
    scheduler.run_once(now=(now + timedelta(seconds=122)).isoformat())
    assert len(memory.list_lifecycle_states()) == 5
    state = scheduler.snapshot()
    assert state["last_run_id"] and state["last_result"]["scored_count"] == 2


def test_pause_is_persistent_checked_and_audited_atomically(memory):
    scheduler = memory.lifecycle_scheduler
    before = scheduler.snapshot()
    paused = scheduler.set_paused(True, expected_revision=before["revision"], actor="qa")
    assert paused["paused"]
    with pytest.raises(ValueError, match="changed"):
        scheduler.set_paused(False, expected_revision=before["revision"], actor="qa")
    restarted = MemoryLifecycleScheduler(memory.runtime)
    assert restarted.run_once()["status"] == "paused"
    assert len(memory.quality_ops.list_quality_events(event_type="memory_maintenance_control")) == 1
    restarted.set_paused(False, expected_revision=paused["revision"], actor="qa")
    assert restarted.run_once()["status"] == "completed"


def test_maintenance_handles_expiry_without_reviving_forgotten_memory(memory):
    target = memory.add_manual("isolated ephemeral preference", memory_type="preference")
    forgotten = memory.add_manual("isolated forgotten preference", memory_type="preference")
    memory.retention.request_forget(target_type="memory", target_id=forgotten["id"], reason="qa forget")
    with memory._lock, memory._conn:
        memory._conn.execute("UPDATE memory_record SET valid_until='2000-01-01T00:00:00+00:00' WHERE id=?", (target["id"],))
    result = memory.lifecycle_scheduler.run_once()
    assert result["expired_memory_count"] == 1
    assert result["scored_count"] == 0
    assert memory.get_memory(target["id"])["status"] != "active"
    assert memory.get_memory(forgotten["id"])["status"] != "active"
    assert memory.lifecycle.find_tombstone_match(forgotten["summary"])


def test_failed_period_rolls_back_retention_heat_and_checkpoint(memory):
    target = memory.add_manual("isolated expiration rollback", memory_type="preference")
    with memory._lock, memory._conn:
        memory._conn.execute("UPDATE memory_record SET valid_until='2000-01-01T00:00:00+00:00' WHERE id=?", (target["id"],))
    with patch.object(memory.runtime.lifecycle, "process_tombstones", side_effect=RuntimeError("injected")):
        with pytest.raises(RuntimeError, match="injected"):
            memory.lifecycle_scheduler.run_once()
    assert memory.get_memory(target["id"])["status"] == "active"
    assert memory.list_heat_runs() == []
    assert memory.lifecycle_scheduler.snapshot()["last_run_id"] is None
    assert memory.lifecycle_scheduler.run_once()["status"] == "completed"


def test_pause_waits_for_inflight_transaction_and_blocks_next_tick(memory):
    entered, release = threading.Event(), threading.Event()
    original = memory.runtime.run_lifecycle
    def slow(**kwargs):
        entered.set()
        assert release.wait(3)
        return original(**kwargs)
    result = []
    revision = memory.lifecycle_scheduler.snapshot()["revision"]
    with patch.object(memory.runtime, "run_lifecycle", side_effect=slow):
        worker = threading.Thread(target=memory.lifecycle_scheduler.run_once)
        worker.start()
        assert entered.wait(3)
        control = threading.Thread(target=lambda: result.append(memory.lifecycle_scheduler.set_paused(True, expected_revision=revision, actor="qa")))
        control.start()
        assert not result
        release.set()
        worker.join(5); control.join(5)
    assert result[0]["paused"]
    assert memory.lifecycle_scheduler.run_once()["status"] == "paused"


def test_managed_worker_starts_once_stops_and_can_restart(memory):
    scheduler = MemoryLifecycleScheduler(memory.runtime, startup_delay_seconds=0, poll_seconds=.01)
    scheduler.start(); thread = scheduler._thread
    scheduler.start()
    assert scheduler._thread is thread
    deadline = time.monotonic() + 3
    while not scheduler.snapshot()["last_run_id"] and time.monotonic() < deadline:
        time.sleep(.01)
    assert scheduler.snapshot()["last_run_id"]
    scheduler.stop(timeout=3)
    assert scheduler.snapshot()["running"] is False
    scheduler.start(); scheduler.stop(timeout=3)
    assert scheduler.snapshot()["running"] is False


def test_gateway_controls_require_owner_permissions_confirmation_and_exact_revision(memory):
    app, capabilities = FastAPI(), LocalCapabilityProvider()
    service = SimpleNamespace(identity_scope=("profile-a", "alice", "sora"))
    service._require_identity_scope = lambda identity: ConversationGatewayService._require_identity_scope(service, identity)
    @app.exception_handler(GatewayServiceError)
    def handle(_request: Request, error: GatewayServiceError):
        return JSONResponse({"code": error.code.value}, status_code=error.status_code)
    install_maintenance_routes(app, stack=SimpleNamespace(memory=memory), service=service,
        capabilities=capabilities, authenticate=_authenticate, require_protocol=_require_protocol, require_owner=_require_memory_owner)
    def headers(user="alice", permissions=None):
        token, _ = capabilities.issue(profile_id="profile-a", subject_id=user, agent_id="sora", client_instance_id="qa-client",
            permissions=permissions or {"memory.read", "memory.write"}, allowed_origins={"sora-desktop://host"})
        return {"Authorization": "Bearer " + token, "X-Sora-Client-Instance": "qa-client", "Origin": "sora-desktop://host", "X-Sora-Protocol-Version": "1.0"}
    with TestClient(app) as client:
        scope = headers()
        page = client.get("/v1/memory/maintenance", headers=scope).json()
        body = {"paused": True, "expected_revision": page["revision"], "confirmed": True}
        assert client.post("/v1/memory/maintenance", headers=headers(permissions={"memory.read"}), json=body).status_code == 403
        assert client.get("/v1/memory/maintenance", headers=headers(user="bob")).status_code == 403
        assert client.post("/v1/memory/maintenance", headers=scope, json={**body, "confirmed": 1}).status_code == 422
        assert client.post("/v1/memory/maintenance", headers=scope, json=body).json()["paused"] is True
        assert client.post("/v1/memory/maintenance", headers=scope, json=body).status_code == 409
