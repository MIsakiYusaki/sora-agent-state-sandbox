from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from sora_sandbox.conversation_gateway.desktop_privacy import install_privacy_routes
from sora_sandbox.conversation_gateway.host import _authenticate, _require_memory_owner, _require_protocol
from sora_sandbox.conversation_gateway.security import LocalCapabilityProvider
from sora_sandbox.conversation_gateway.service import GatewayServiceError
from sora_sandbox.conversation_gateway.contracts import GatewayErrorCode
from sora_sandbox.memory_runtime import MemoryRuntime


@pytest.fixture
def privacy(tmp_path):
    memory = MemoryRuntime(tmp_path / "memory.db", owner_user_id="user-a", agent_id="sora")
    capabilities = LocalCapabilityProvider()
    app = FastAPI()

    @app.exception_handler(GatewayServiceError)
    def handle(_request: Request, error: GatewayServiceError):
        return JSONResponse({"code": error.code.value}, status_code=error.status_code)

    def check_scope(identity):
        if (identity.profile_id, identity.subject_id, identity.agent_id) != ("profile-a", "user-a", "sora"):
            raise GatewayServiceError(GatewayErrorCode.FORBIDDEN, "Wrong space", status_code=403)

    install_privacy_routes(app, memory=memory, service=SimpleNamespace(_require_identity_scope=check_scope),
        capabilities=capabilities, authenticate=_authenticate, require_protocol=_require_protocol, require_owner=_require_memory_owner)
    with TestClient(app) as client:
        yield memory, capabilities, client
    memory.close()


def headers(capabilities, *, profile="profile-a", user="user-a", permissions=None):
    token, _ = capabilities.issue(profile_id=profile, subject_id=user, agent_id="sora", client_instance_id="fixture-client",
        permissions={"memory.read"} if permissions is None else permissions, allowed_origins={"sora-desktop://host"})
    return {"Authorization": "Bearer " + token, "X-Sora-Client-Instance": "fixture-client", "X-Sora-Protocol-Version": "1.0", "Origin": "sora-desktop://host"}


def test_export_requires_permission_scope_and_exact_confirmation(privacy):
    memory, capabilities, client = privacy
    payload = {"kind": "memory_summary", "confirmed": True}
    assert client.post("/v1/privacy/export", json=payload).status_code == 426
    assert client.post("/v1/privacy/export", headers={"X-Sora-Protocol-Version": "1.0"}, json=payload).status_code == 401
    for scoped in (headers(capabilities, user="foreign"), headers(capabilities, profile="foreign"), headers(capabilities, permissions=set())):
        assert client.post("/v1/privacy/export", headers=scoped, json=payload).status_code == 403
    for value in (False, 1, "true"):
        assert client.post("/v1/privacy/export", headers=headers(capabilities), json={**payload, "confirmed": value}).status_code == 422
    assert client.post("/v1/privacy/export", headers=headers(capabilities), json={**payload, "path": "arbitrary"}).status_code == 422


def test_summary_export_filters_hidden_protected_stale_classifications_and_raw_evidence(privacy):
    memory, capabilities, client = privacy
    visible = memory.add_manual("用户喜欢清淡的茉莉花茶")
    hidden = memory.add_manual("synthetic-hidden-sentinel")
    secret = memory.add_manual("synthetic-classification-sentinel")
    with memory._lock:
        memory._conn.execute("UPDATE memory_record SET surface_permission='none' WHERE id=?", (hidden["id"],))
        memory._conn.execute("UPDATE memory_record SET summary=? WHERE id=?", ("sk-" + "secret-sentinel-" * 3, secret["id"]))
        memory._conn.execute("UPDATE evidence SET raw_content='raw-evidence-sentinel'")
        memory._conn.commit()
    response = client.post("/v1/privacy/export", headers=headers(capabilities), json={"kind": "memory_summary", "confirmed": True})
    assert response.status_code == 200
    result = response.json()
    assert result["content"]["memories"] == [{"summary": visible["summary"]}]
    assert result["content"]["exported_count"] == 1
    assert result["content"]["excluded_count"] == 2
    assert all(value not in response.text for value in ("hidden-sentinel", "secret-sentinel", "raw-evidence-sentinel", "profile-a", "user-a"))


def test_diagnostics_contains_only_allowlisted_counts_and_versions(privacy):
    memory, capabilities, client = privacy
    memory.add_manual("synthetic-private-content-sentinel")
    response = client.post("/v1/privacy/export", headers=headers(capabilities), json={"kind": "diagnostic", "confirmed": True})
    assert response.status_code == 200
    result = response.json()
    assert set(result["content"]) == {"protocol_version", "python_version", "memory_records", "memory_states", "coverage"}
    assert result["content"]["memory_records"] == 1
    assert result["content"]["memory_states"]["active"] == 1
    assert "synthetic-private" not in response.text
    assert str(memory.path) not in response.text
