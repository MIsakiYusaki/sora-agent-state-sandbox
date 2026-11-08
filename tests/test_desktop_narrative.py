"""Signed authored choices and real canonical/Memory flow in isolated QA stores."""
import json
import sqlite3
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from sora_sandbox.conversation_gateway.desktop_narrative import install_narrative_routes
from sora_sandbox.conversation_gateway.host import _authenticate, _require_memory_owner, _require_protocol
from sora_sandbox.conversation_gateway.security import LocalCapabilityProvider
from sora_sandbox.conversation_gateway.service import GatewayServiceError
from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.worldops.compiler import CanonCompiler, Ed25519BundleSigner
from sora_sandbox.worldops.contracts import IdentityScope
from sora_sandbox.worldops.privacy import WorldOpsSecretVault
from sora_sandbox.worldops.runtime import WorldOpsRuntime
from sora_sandbox.worldops.store import WorldOpsStore
from sora_sandbox.worldops.system import WorldOpsSystem

SOURCE = Path(__file__).resolve().parents[1] / "content/worldops/lantern-message.source.json"


@pytest.fixture
def narrative(tmp_path):
    memory = MemoryRuntime(tmp_path / "memory.db", owner_user_id="alice", agent_id="sora")
    signer = Ed25519BundleSigner.generate(key_id="qa.narrative.key")
    store = WorldOpsStore(tmp_path / "worldops.db", identity=IdentityScope("qa", "alice", "sora", "qa.timeline"))
    store.register_trusted_key(key_id=signer.key_id, public_key=signer.public_key_bytes())
    compiled = CanonCompiler(signer).compile(json.loads(SOURCE.read_text(encoding="utf-8")), canon_version="1.1.0", release_sequence=1)
    store.install_bundle(compiled)
    store.activate_bundle(compiled["manifest"]["bundle_id"], compiled["manifest"]["canon_version"])
    system = WorldOpsSystem(store=store, runtime=WorldOpsRuntime(store), signer=None, server_store=None, studio=None,
        distribution=SimpleNamespace(), secret_vault=WorldOpsSecretVault(tmp_path / "caps"), memory_runtime=memory, start_worker=False)
    capabilities = LocalCapabilityProvider()
    app = FastAPI()

    @app.exception_handler(GatewayServiceError)
    def handle(_request: Request, error: GatewayServiceError):
        return JSONResponse({"code": error.code.value}, status_code=error.status_code)

    def token(user="alice", permissions=None):
        value, _ = capabilities.issue(profile_id="profile-a", subject_id=user, agent_id="sora", client_instance_id="client-a",
            permissions=permissions or {"controls.read", "controls.write"}, allowed_origins={"sora-desktop://host"})
        return {"Authorization": "Bearer " + value, "X-Sora-Client-Instance": "client-a", "Origin": "sora-desktop://host", "X-Sora-Protocol-Version": "1.0"}
    install_narrative_routes(app, stack=SimpleNamespace(memory=memory, worldops=system),
        service=SimpleNamespace(identity_scope=("profile-a", "alice", "sora")), capabilities=capabilities,
        authenticate=_authenticate, require_protocol=_require_protocol, require_owner=_require_memory_owner)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, headers=token(), token=token, worldops=system, memory=memory, signer=signer, store=store)
    system.close(); memory.close()


def read(env):
    response = env.client.get("/v1/narrative", headers=env.headers)
    assert response.status_code == 200, response.text
    return response.json()


def body(page, *, action="invite", request_id="qa-invite", choice_id=None):
    event = page["events"][0]
    return {"request_id": request_id, "instance_id": event["id"], "expected_revision": event["revision"],
        "bundle_id": page["bundle_id"], "canon_version": page["canon_version"], "action": action, "choice_id": choice_id}


def post(env, value):
    return env.client.post("/v1/narrative/actions", headers=env.headers, json=value)


def invite(env):
    response = post(env, body(read(env)))
    assert response.status_code == 200, response.text
    return response.json()["view"]


def test_literal_authored_choice_has_actual_fact_task_timeline_and_memory_proposal(narrative):
    env = narrative
    assert env.worldops.signer is None and env.worldops.server_store is None
    page = invite(env)
    assert page["events"][0]["state"] == "invited"
    request = body(page, action="choice", request_id="qa-pick", choice_id="safe_return")
    response = post(env, request)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["receipt"]["state"] == "completed" and result["receipt"]["llm_called"] is False
    assert result["view"]["events"][0]["tasks"][0]["completed"] is True
    assert "回家的灯" in env.store.active_facts()["starport.lantern.message"]
    assert env.worldops.runtime.provider is None
    env.worldops.process_outbox()
    candidates = env.memory.list_candidates()
    assert len(candidates) == 1 and candidates[0]["status"] == "needs_review"
    reviews = env.memory.list_review_items(status="open")
    assert len(reviews) == 1 and reviews[0]["candidate_id"] == candidates[0]["candidate_id"]
    assert "回家的灯" in candidates[0]["summary"]
    assert read(env)["events"][0]["memory_state"] == candidates[0]["status"]
    history = read(env)["events"][0]["history"]
    assert {x["state"] for x in history} >= {"invited", "active", "completed"}
    assert post(env, request).json()["receipt"] == result["receipt"]
    env.worldops.process_outbox()
    assert len(env.memory.list_candidates()) == 1
    snapshot = env.memory.review_snapshot(reviews[0]["review_id"])
    env.memory.resolve_review_checked(reviews[0]["review_id"], action="approve", expected_revision=snapshot["revision"], actor="qa-reviewer")
    assert read(env)["events"][0]["memory_state"] == "committed"


def test_shared_episode_gate_failure_rolls_back_materialization_and_can_retry(narrative):
    from unittest.mock import patch
    env = narrative
    page = invite(env)
    assert post(env, body(page, action="choice", request_id="qa-gate-failure", choice_id="safe_return")).status_code == 200
    shared = next(row for row in env.store.pending_outbox(limit=100) if row["event_type"] == "worldops.shared_episode.completed.v1")
    with patch.object(env.memory, "run_candidate_gate", side_effect=RuntimeError("injected gate failure")):
        with pytest.raises(RuntimeError, match="gate failure"):
            env.worldops._deliver_outbox_event(dict(shared["payload"]))
    assert env.memory.list_evidence(source_type="worldops_transition") == []
    assert env.memory.list_candidates() == []
    env.worldops._deliver_outbox_event(dict(shared["payload"]))
    assert len(env.memory.list_review_items(status="open")) == 1


def test_worldops_episode_cannot_auto_commit_even_with_extractor_schema():
    from sora_sandbox.memory_gate_engine import evaluate_candidate_gate
    decision = evaluate_candidate_gate({"candidate_id": "qa-worldops", "type": "worldops_episode", "operation": "ADD",
        "summary": "QA participated episode", "sensitivity": "low", "confidence": 1., "stability": 1.,
        "specificity": 1., "importance": 1., "novelty": 1., "explicit_user_request": True,
        "schema_version": "extractor_result.v1"}, evidence=[{"id":"qa-source"}])
    assert decision.decision.value == "NEEDS_REVIEW"


@pytest.mark.parametrize("choice", ["defer", "decline"])
def test_deferral_and_decline_do_not_complete_or_create_shared_memory(narrative, choice):
    env = narrative
    page = invite(env)
    response = post(env, body(page, action="choice", request_id="qa-no-participation", choice_id=choice))
    assert response.status_code == 200, response.text
    assert response.json()["receipt"]["state"] in {"hinted", "archived"}
    assert not response.json()["view"]["events"][0]["tasks"][0]["completed"]
    env.worldops.process_outbox()
    assert env.memory.list_candidates() == []


def test_stale_revision_unknown_choice_and_changed_request_rejected_before_effects(narrative):
    env = narrative
    original = read(env)
    page = invite(env)
    assert post(env, body(original, request_id="stale-invite")).status_code == 409
    request = body(page, action="choice", request_id="qa-choice", choice_id="unknown-choice")
    assert post(env, request).status_code == 409
    request["choice_id"] = "safe_return"
    assert post(env, request).status_code == 200
    request["choice_id"] = "small_courage"
    assert post(env, request).status_code == 409
    assert "回家的灯" in env.store.active_facts()["starport.lantern.message"]


def test_receipt_failure_rolls_back_choice_evidence_transitions_and_outbox(narrative):
    env = narrative
    page = invite(env)
    request = body(page, action="choice", request_id="qa-fail", choice_id="small_courage")
    with patch.object(env.store, "record_desktop_action", side_effect=RuntimeError("QA receipt failure")):
        with pytest.raises(RuntimeError): post(env, request)
    assert read(env)["events"][0]["state"] == "invited"
    assert env.store.active_facts() == {}
    env.store.update_control(story_mode=True)
    with sqlite3.connect(env.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM worldops_choice_ledger").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM worldops_task_evidence").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM worldops_transition_ledger WHERE to_state='completed'").fetchone()[0] == 0
    assert post(env, request).status_code == 200


def test_revoked_key_blocks_cached_content_and_participation(narrative):
    env = narrative
    page = read(env)
    env.store.revoke_key(env.signer.key_id, revocation_list_version=1, reason="qa_revocation")
    blocked = read(env)
    assert blocked["available"] is False and blocked["unavailable_reason"] == "content_verification_failed"
    assert post(env, body(page)).status_code == 409


def test_owner_permission_and_explicit_development_source(narrative):
    env = narrative
    assert env.client.get("/v1/narrative", headers=env.token("bob")).status_code == 403
    request = body(read(env))
    assert env.client.post("/v1/narrative/actions", headers=env.token(permissions={"controls.read"}), json=request).status_code == 403
    env.worldops.deployment_mode = "local_split_development"
    assert read(env)["source"] == "local_preview"


def test_other_reader_cannot_observe_uncommitted_choice_or_completion(narrative):
    env = narrative
    page = invite(env)
    entered, release = threading.Event(), threading.Event()
    original = env.store.record_desktop_action
    replies = []
    def checkpoint(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)
    request = body(page, action="choice", request_id="qa-isolation", choice_id="small_courage")
    with patch.object(env.store, "record_desktop_action", side_effect=checkpoint):
        worker = threading.Thread(target=lambda: replies.append(post(env, request)))
        worker.start()
        try:
            assert entered.wait(10)
            assert read(env)["events"][0]["state"] == "invited"
            assert env.store.active_facts() == {}
        finally:
            release.set(); worker.join(10)
    assert not worker.is_alive() and replies[0].status_code == 200
    assert read(env)["events"][0]["state"] == "completed"


def test_process_exit_before_receipt_cannot_leave_half_a_choice(narrative):
    env = narrative
    page = invite(env)
    request = body(page, action="choice", request_id="qa-process-exit", choice_id="ordinary_evening")
    code = f'''import os
from sora_sandbox.worldops.store import WorldOpsStore
from sora_sandbox.worldops.runtime import WorldOpsRuntime
from sora_sandbox.worldops.contracts import IdentityScope
store=WorldOpsStore({str(env.store.path)!r},identity=IdentityScope("qa","alice","sora","qa.timeline"))
store.record_desktop_action=lambda *args: os._exit(73)
WorldOpsRuntime(store).apply_authored_action(**{request!r})
'''
    result = subprocess.run([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    assert read(env)["events"][0]["state"] == "invited"
    assert env.store.active_facts() == {}
    assert post(env, request).status_code == 200
    assert read(env)["events"][0]["state"] == "completed"


def test_caught_nested_failure_cannot_commit_a_subset(narrative):
    env = narrative
    page = invite(env)
    event = page["events"][0]
    before = env.store.get_control()["story_mode"]
    with pytest.raises(sqlite3.IntegrityError):
        with env.store.transaction():
            with patch.object(env.store, "_trace_locked", side_effect=RuntimeError("QA nested failure")):
                try:
                    env.store.transition_event(event["id"], to_state="archived", expected_revision=event["revision"],
                        trigger_type="choice", choice_id="decline")
                except RuntimeError:
                    pass
            env.store.update_control(story_mode=not before)
    assert read(env)["events"][0]["state"] == "invited"
    assert env.store.get_control()["story_mode"] == before
