"""Real isolated owner stores, explicit controls and hostile/stale requests; no models."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from sora_sandbox.conversation_gateway.desktop_companion import install_companion_routes
from sora_sandbox.conversation_gateway.desktop_commands import install_desktop_command_routes
from sora_sandbox.conversation_gateway.host import _authenticate, _require_memory_owner, _require_protocol
from sora_sandbox.conversation_gateway.security import LocalCapabilityProvider
from sora_sandbox.conversation_gateway.service import GatewayServiceError
from sora_sandbox.memory_runtime import MemoryRuntime
from sora_sandbox.persona.store import PersonaStore
from sora_sandbox.persona.system import PersonaSystem
from sora_sandbox.relationship.store import RelationshipStore
from sora_sandbox.relationship.system import RelationshipSystem


@pytest.fixture
def companion(tmp_path):
    memory = MemoryRuntime(tmp_path / "memory.db", owner_user_id="alice", agent_id="sora")
    persona = PersonaSystem(PersonaStore(tmp_path / "persona.db", agent_id="sora"), start_worker=False)
    relationship = RelationshipSystem(RelationshipStore(tmp_path / "relationship.db", sora_id="sora"), start_worker=False)
    capabilities = LocalCapabilityProvider()
    app = FastAPI()

    @app.exception_handler(GatewayServiceError)
    def handle(_request: Request, error: GatewayServiceError):
        return JSONResponse({"code": error.code.value}, status_code=error.status_code)

    def token(user="alice", permissions=None):
        value, _ = capabilities.issue(profile_id="profile-a", subject_id=user, agent_id="sora", client_instance_id="client-a",
                                     permissions=permissions or {"controls.read", "controls.write", "debug.read", "debug.session.open"},
                                     allowed_origins={"sora-desktop://host"})
        return {"Authorization": "Bearer " + value, "X-Sora-Client-Instance": "client-a", "Origin": "sora-desktop://host", "X-Sora-Protocol-Version": "1.0"}

    stack = SimpleNamespace(memory=memory, persona=persona, relationship=relationship)
    service = SimpleNamespace(identity_scope=("profile-a", "alice", "sora"), readiness=lambda: {"active_turns": 0})
    install_desktop_command_routes(app, stack=stack, service=service, capabilities=capabilities,
        authenticate=_authenticate, require_protocol=_require_protocol, require_owner=_require_memory_owner)
    install_companion_routes(app, stack=stack, service=service,
                             capabilities=capabilities, authenticate=_authenticate, require_protocol=_require_protocol, require_owner=_require_memory_owner)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, headers=token(), token=token, persona=persona, relationship=relationship)
    relationship.close(); persona.close(); memory.close()


def read(env):
    result = env.client.get("/v1/companion", headers=env.headers)
    assert result.status_code == 200, result.text
    return result.json()


def update(env, control, value, revision, **extra):
    return env.client.post("/v1/companion/controls", headers=env.headers,
                           json={"control": control, "value": value, "expected_revision": revision, "confirmed": True, **extra})


def test_growth_control_persists_audit_and_does_not_change_other_user(companion):
    env = companion
    original = read(env)
    response = update(env, "persona_growth", True, original["persona"]["revision"])
    assert response.status_code == 200, response.text
    assert response.json()["persona"]["growth_paused"] is True
    assert env.persona.store.get_control(user_id="alice")["growth_paused"] is True
    assert env.persona.store.get_control(user_id="bob")["growth_paused"] is False
    assert env.persona.store.get_control()["growth_paused"] is False
    assert any(row["action"] == "control_updated" for row in env.persona.store.list_audit())
    with __import__("sqlite3").connect(env.persona.store.path) as connection:
        assert connection.execute("SELECT growth_paused FROM persona_control WHERE user_id='alice'").fetchone()[0] == 1


def test_stale_control_fails_without_overwriting_or_extra_audit(companion):
    env = companion
    revision = read(env)["persona"]["revision"]
    assert update(env, "persona_growth", True, revision).status_code == 200
    count = len(env.persona.store.list_audit())
    assert update(env, "persona_growth", False, revision).status_code == 409
    assert env.persona.store.get_control(user_id="alice")["growth_paused"] is True
    assert len(env.persona.store.list_audit()) == count


def test_relationship_control_changes_real_pair_and_frame(companion):
    env = companion
    state = read(env)
    first = update(env, "relationship_learning", True, state["relationship"]["revision"])
    assert first.status_code == 200, first.text
    second = update(env, "relationship_surface", "silent", first.json()["relationship"]["revision"])
    assert second.status_code == 200, second.text
    pair = env.relationship.pair(user_id="alice")
    assert pair["learning_paused"] is True
    assert env.relationship.frame(user_id="alice")["use_policy"]["surface"] == "silent"
    assert env.relationship.pair(user_id="bob")["learning_paused"] is False
    assert update(env, "relationship_learning", False, state["relationship"]["revision"]).status_code == 409


@pytest.mark.parametrize("confirmed", [False, 1, "true"])
def test_explicit_boolean_confirmation_required(companion, confirmed):
    env = companion
    assert update(env, "persona_growth", True, read(env)["persona"]["revision"], confirmed=confirmed).status_code == 422
    assert env.persona.store.get_control(user_id="alice")["growth_paused"] is False


def test_invalid_control_value_and_cross_owner_requests_blocked(companion):
    env = companion
    revision = read(env)["persona"]["revision"]
    assert update(env, "persona_growth", "explicit", revision).status_code == 409
    assert update(env, "persona_growth", 1, revision).status_code == 422
    assert env.client.get("/v1/companion", headers=env.token("bob")).status_code == 403
    assert env.client.post("/v1/companion/controls", headers=env.token(permissions={"controls.read"}),
        json={"control": "persona_growth", "value": True, "expected_revision": revision, "confirmed": True}).status_code == 403


def test_agreement_proposal_requires_explicit_confirmation_and_affects_frame(companion):
    env = companion
    before = read(env)["active_agreement"]
    result = env.client.post("/v1/companion/agreements/propose", headers=env.headers, json={"rhythm": "warm_low_pressure"})
    assert result.status_code == 200, result.text
    state = result.json()
    assert state["active_agreement"] == before
    assert state["active_agreement"]["user_confirmed"] is False
    assert "confirmation_token" not in result.text
    row = state["agreements"][0]
    assert row["can_confirm"] is True
    confirmed = env.client.post("/v1/companion/agreements/confirm", headers=env.headers,
        json={"candidate_id": row["id"], "expected_revision": row["revision"], "confirmed": True})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["active_agreement"]["comfort_rhythm"] == "warm_low_pressure"
    assert confirmed.json()["active_agreement"]["user_confirmed"] is True
    pair = env.relationship.pair(user_id="alice")
    assert env.relationship.store.active_agreement(pair["pair_id"])["terms"]["comfort_rhythm"] == "warm_low_pressure"
    assert "warm_low_pressure" in str(env.relationship.frame(user_id="alice"))
    active = confirmed.json()["active_agreement"]
    assert env.client.post("/v1/companion/agreements/revoke", headers=env.headers,
        json={"expected_revision": "0" * 64, "confirmed": True}).status_code == 409
    revoked = env.client.post("/v1/companion/agreements/revoke", headers=env.headers,
        json={"expected_revision": active["revision"], "confirmed": True})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["active_agreement"]["status"] != "active"


def test_protected_agreement_is_not_blindly_confirmable(companion):
    env = companion
    env.relationship.propose_agreement(user_id="alice", term_path="terms.private_note", proposed_value="a confidential note")
    row = read(env)["agreements"][0]
    assert row["value"] is None and row["can_confirm"] is False
    assert env.client.post("/v1/companion/agreements/confirm", headers=env.headers,
        json={"candidate_id": row["id"], "expected_revision": row["revision"], "confirmed": True}).status_code == 409


def test_repair_transition_is_user_scoped_and_stale_requests_fail(companion):
    env = companion
    env.relationship.open_repair_episode(user_id="alice", trigger_ref="turn:repair-example", affected_dimensions=["trust"])
    row = read(env)["repair"]
    body = {"repair_id": row["id"], "expected_status": row["status"], "to_state": "acknowledged", "confirmed": True}
    assert env.client.post("/v1/companion/repair", headers=env.headers, json=body).status_code == 200
    assert env.client.post("/v1/companion/repair", headers=env.headers, json=body).status_code == 409
    env.relationship.open_repair_episode(user_id="bob", trigger_ref="turn:neighbour", affected_dimensions=["trust"])
    neighbour = env.relationship.store.open_repair(env.relationship.pair(user_id="bob")["pair_id"])
    body.update(repair_id=neighbour["repair_episode_id"])
    assert env.client.post("/v1/companion/repair", headers=env.headers, json=body).status_code == 409
    assert env.relationship.store.open_repair(neighbour["pair_id"])["status"] == "rupture_detected"


def test_control_audit_failure_rolls_back_setting(companion):
    env = companion
    revision = read(env)["persona"]["revision"]
    with patch.object(env.persona.store, "_insert_audit", side_effect=RuntimeError("injected audit failure")):
        with pytest.raises(RuntimeError):
            update(env, "persona_growth", True, revision)
    assert env.persona.store.get_control(user_id="alice")["growth_paused"] is False


def test_governance_minimizes_content_and_filters_other_owner(companion):
    env = companion
    fixture = {"candidate_id": "candidate_own", "user_id": "alice", "status": "pending_review", "candidate": {"target_key": "playfulness", "evidence_refs": ["private raw evidence"]}, "source_payload": {"api_key": "secret-sentinel"}}
    with patch.object(env.persona.store, "list_candidates", return_value=[fixture, {**fixture, "candidate_id": "candidate_other", "user_id": "bob"}]):
        result = env.client.get("/v1/debug/companion", headers=env.headers)
    assert result.status_code == 200, result.text
    assert len(result.json()["candidates"]) == 1
    assert all(text not in result.text for text in ("secret-sentinel", "private raw evidence", "candidate_other", "source_payload"))
    assert env.client.get("/v1/debug/companion", headers=env.token(permissions={"controls.read"})).status_code == 403


def manage(env):
    response = env.client.post("/v1/debug/companion/session", headers=env.headers, json={"window_label": "main"})
    assert response.status_code == 200, response.text
    return {**env.headers, "Authorization": "Bearer " + response.json()["capability_token"]}, response.json()["session"]


def seed_relationship(env, user="alice", paused=False):
    from tests.test_relationship_system import RelationshipSystemTests
    if paused:
        env.relationship.set_learning_paused(user_id=user, paused=True)
    event = RelationshipSystemTests()._event("qa-" + user, "completed" if paused else "boundary_respected")
    env.relationship.ingest_event(event, user_id=user)
    env.relationship.process_pending()


def seed_persona(env, paused=False):
    from tests.test_persona_system import DeterministicProvider, projection_event
    env.persona.provider = DeterministicProvider()
    if paused:
        env.persona.set_growth_paused(True, user_id="alice")
    env.persona.ingest_projection(projection_event("qa-event", "qa-memory", user_id="alice"))
    env.persona.process_pending()


@pytest.mark.parametrize("domain", ["persona", "relationship"])
def test_governance_rejects_real_pending_candidate_with_atomic_audit(companion, domain):
    env = companion
    (seed_persona if domain == "persona" else seed_relationship)(env, paused=True)
    page = env.client.get("/v1/debug/companion", headers=env.headers, params={"domain": domain}).json()
    row = next(x for x in page["candidates"] if x["status"] == "pending_review")
    body = {"domain": domain, "action": "reject_candidate", "target_id": row["id"], "expected_revision": row["revision"], "confirmed": True}
    assert env.client.post("/v1/debug/companion/resolve", headers=env.headers, json=body).status_code == 403
    headers, session = manage(env)
    assert "companion.manage" in session["scopes"]
    response = env.client.post("/v1/debug/companion/resolve", headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["audit_id"]
    page = env.client.get("/v1/debug/companion", headers=env.headers, params={"domain": domain}).json()
    assert next(x for x in page["candidates"] if x["id"] == row["id"])["status"] == "rejected"
    assert env.client.post("/v1/debug/companion/resolve", headers=headers, json=body).status_code == 409
    env.client.delete("/v1/debug/sessions/" + session["session_id"], headers=headers)
    assert env.client.post("/v1/debug/companion/resolve", headers=headers, json=body).status_code == 403


@pytest.mark.parametrize("domain", ["persona", "relationship"])
def test_governance_rollback_rebuilds_from_actual_contributions(companion, domain):
    env = companion
    if domain == "persona":
        before = env.persona.store.active_snapshot()["trait_values"].copy()
        seed_persona(env)
        assert env.persona.store.active_snapshot()["trait_values"] != before
    else:
        seed_relationship(env)
    page = env.client.get("/v1/debug/companion", headers=env.headers, params={"domain": domain}).json()
    row = next(x for x in page["contributions"] if x["status"] == "active")
    headers, _ = manage(env)
    body = {"domain": domain, "action": "rollback_contribution", "target_id": row["id"], "expected_revision": row["revision"], "confirmed": True}
    response = env.client.post("/v1/debug/companion/resolve", headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["audit_id"]
    if domain == "persona":
        assert env.persona.store.active_snapshot()["trait_values"] == before
    page = env.client.get("/v1/debug/companion", headers=env.headers, params={"domain": domain}).json()
    assert next(x for x in page["contributions"] if x["id"] == row["id"])["status"] in {"inactive", "deactivated"}
    assert env.client.post("/v1/debug/companion/resolve", headers=headers, json=body).status_code == 409


def test_governance_cross_pair_and_audit_failure_leave_state_intact(companion):
    env = companion
    seed_relationship(env, user="bob", paused=True)
    row = env.relationship.list_candidates(user_id="bob")[0]
    from sora_sandbox.conversation_gateway.desktop_companion import fingerprint
    headers, _ = manage(env)
    body = {"domain": "relationship", "action": "reject_candidate", "target_id": row["candidate_id"], "expected_revision": fingerprint(row), "confirmed": True}
    assert env.client.post("/v1/debug/companion/resolve", headers=headers, json=body).status_code == 409
    assert env.relationship.store.candidate(row["candidate_id"])["status"] == "pending_review"
    seed_relationship(env, paused=True)
    row = env.relationship.list_candidates(user_id="alice")[0]
    body.update(target_id=row["candidate_id"], expected_revision=fingerprint(row))
    with patch.object(env.relationship.store, "_insert_audit", side_effect=RuntimeError("injected audit failure")):
        with pytest.raises(RuntimeError):
            env.client.post("/v1/debug/companion/resolve", headers=headers, json=body)
    assert env.relationship.store.candidate(row["candidate_id"])["status"] == "pending_review"


def test_governance_filtering_does_not_hide_source_truncation(companion):
    env = companion
    rows = [{"candidate_id": f"other-{i}", "user_id": "bob", "status": "pending_review"} for i in range(101)]
    with patch.object(env.persona.store, "list_candidates", return_value=rows):
        page = env.client.get("/v1/debug/companion", headers=env.headers).json()
    assert page["candidates"] == [] and page["truncated"] is True


def test_replacement_and_expired_native_sessions_cannot_mutate(companion):
    env = companion
    seed_persona(env, paused=True)
    row = env.client.get("/v1/debug/companion", headers=env.headers).json()["candidates"][0]
    body = {"domain": "persona", "action": "reject_candidate", "target_id": row["id"], "expected_revision": row["revision"], "confirmed": True}
    old, _ = manage(env)
    new, session = manage(env)
    assert env.client.post("/v1/debug/companion/resolve", headers=old, json=body).status_code == 403
    with patch("sora_sandbox.conversation_gateway.security.now_ms", return_value=session["expires_at_ms"] + 1):
        assert env.client.post("/v1/debug/companion/resolve", headers=new, json=body).status_code == 403
    assert env.persona.store.get_candidate(row["id"])["status"] == "pending_review"
