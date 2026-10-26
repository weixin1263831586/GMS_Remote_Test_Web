"""Personal documents stay human-only while external search retains its scope."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.knowledge import api
from features.knowledge.service import KnowledgeService
from features.knowledge.storage import KnowledgeStore


@pytest.fixture
def personal_client(tmp_path, monkeypatch):
    monkeypatch.setenv("GMS_AUTH_REQUIRED", "true")
    store = KnowledgeStore(tmp_path / "knowledge.sqlite3", tmp_path / "attachments")
    service = KnowledgeService(store)
    monkeypatch.setattr(api, "_service", service)
    doc = service.create_doc_from_text(
        "owner", space_id=store.default_space_id("owner", "gms"),
        title="private note", content="private contents",
    )
    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        method = request.headers.get("X-Auth", "session")
        request.state.auth_method = method
        request.state.current_user = CurrentUser(
            id="owner" if method == "session" else "agent:test",
            username="test", role="user" if method == "session" else "agent_service",
            resource_owner_id=request.headers.get("X-Owner", "owner"),
            extra_permissions=frozenset(filter(None, request.headers.get("X-Scopes", "").split(","))),
        )
        return await call_next(request)

    app.include_router(api.router)
    with TestClient(app) as client:
        yield client, store, doc


@pytest.mark.parametrize("auth", ["agent_token", "machine_authority"])
@pytest.mark.parametrize("scopes", ["", "knowledge.read"])
def test_nonhuman_cannot_read_or_mutate_personal_documents(personal_client, auth, scopes):
    client, store, doc = personal_client
    requests = [
        ("GET", f"/api/knowledge/docs/{doc['doc_id']}", None),
        ("PUT", f"/api/knowledge/docs/{doc['doc_id']}", {"content_md": "changed"}),
        ("DELETE", f"/api/knowledge/nodes/{doc['node_id']}", None),
        ("POST", "/api/knowledge/spaces", {"name": "new"}),
    ]
    for method, path, body in requests:
        response = client.request(method, path, headers={"X-Auth": auth, "X-Scopes": scopes}, json=body)
        assert response.status_code == 403
    assert store.get_doc("owner", doc["doc_id"])["content_md"] == "private contents"


def test_human_owner_can_edit_but_another_account_cannot(personal_client):
    client, store, doc = personal_client
    path = f"/api/knowledge/docs/{doc['doc_id']}"
    assert client.get(path, headers={"X-Owner": "other"}).status_code == 404
    assert client.put(path, json={"content_md": "updated"}).status_code == 200
    assert store.get_doc("owner", doc["doc_id"])["content_md"] == "updated"


def test_external_sources_are_not_subject_to_personal_human_gate(personal_client, monkeypatch):
    client, _, _ = personal_client
    monkeypatch.setattr(api._external_api, "federated_status", lambda: [])
    response = client.get("/api/knowledge/external/sources", headers={"X-Auth": "agent_token", "X-Scopes": "knowledge.read"})
    assert response.status_code == 200
