"""Single, chunked and resumed uploads keep the selected host and directory."""

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from features.auth import CurrentUser, require_elevated_admin
from features.system import terminal_api as api


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    calls = []
    connections = []
    target = {"host": "selected-host"}
    sftp = MagicMock()
    sftp.put.side_effect = lambda local, remote, **kw: calls.append((Path(local).read_bytes(), remote))
    ssh = MagicMock()
    ssh.open_sftp.return_value.__enter__.return_value = sftp

    def resolve(worker_id, **kwargs):
        if worker_id != "selected":
            raise ValueError("unknown Worker")
        return worker_id, target["host"], "selected-user", "synthetic-password", ""

    @asynccontextmanager
    async def connect(config):
        connections.append(config)
        yield ssh

    monkeypatch.setattr(api, "upload_temp_root", lambda: str(tmp_path))
    monkeypatch.setattr(api, "resolve_authorized_terminal_target", resolve)
    monkeypatch.setattr(api.config_manager, "load_config", lambda: {"ubuntu_host": "wrong-host"})
    monkeypatch.setattr(api.ssh_manager, "async_optional_connection", connect)
    monkeypatch.setattr(api.ssh_manager, "optimize_sftp_performance", lambda sftp: None)
    app = FastAPI()

    def admin(request: Request):
        return CurrentUser(id=request.headers.get("X-Owner", "owner"), username="admin", role="admin")

    app.dependency_overrides[require_elevated_admin] = admin
    app.include_router(api.router)
    with TestClient(app) as client:
        yield client, calls, connections, target, sftp


def _data(**extra):
    return {"worker_id": "selected", "path": "/chosen", "upload_id": "upload-one",
            "file_name": "test.bin", "file_size": "2", "total_chunks": "2", **extra}


def test_chunked_upload_and_resume_use_selected_target(uploads, tmp_path):
    client, calls, connections, _, _ = uploads
    first = client.post("/api/terminal/push", data=_data(chunk_index="0"), files={"file": ("test.bin", b"a")})
    assert first.status_code == 200, first.text
    status = client.post("/api/terminal/push", data=_data(check_chunks="1"))
    assert status.json()["uploaded_chunks"] == [0]
    metadata = next(tmp_path.rglob("target.json")).read_text()
    assert "synthetic-password" not in metadata
    final = client.post("/api/terminal/push", data=_data(chunk_index="1"), files={"file": ("test.bin", b"b")})
    assert final.status_code == 200, final.text
    assert final.json()["remote_path"] == "/chosen/test.bin"
    assert calls == [(b"ab", "/chosen/test.bin")]
    assert connections[0]["ubuntu_host"] == "selected-host"
    assert connections[0]["username"] == "selected-user"


@pytest.mark.parametrize("change", [{"path": "/other"}, {"worker_id": "missing"}, {"file_name": "other.bin"}])
def test_resume_cannot_change_target_or_file(uploads, change):
    client, calls, _, _, _ = uploads
    assert client.post("/api/terminal/push", data=_data(chunk_index="0"), files={"file": ("test.bin", b"a")}).status_code == 200
    response = client.post("/api/terminal/push", data=_data(check_chunks="1", **change))
    assert response.status_code in {400, 409}
    assert calls == []


def test_worker_address_change_requires_new_upload_session(uploads):
    client, calls, _, target, _ = uploads
    assert client.post("/api/terminal/push", data=_data(chunk_index="0"), files={"file": ("test.bin", b"a")}).status_code == 200
    target["host"] = "reassigned-host"
    response = client.post("/api/terminal/push", data=_data(chunk_index="1"), files={"file": ("test.bin", b"b")})
    assert response.status_code == 409
    assert calls == []


def test_other_owner_cannot_resume_existing_upload(uploads):
    client, _, _, _, _ = uploads
    client.post("/api/terminal/push", data=_data(chunk_index="0"), files={"file": ("test.bin", b"a")})
    response = client.post("/api/terminal/push", data=_data(check_chunks="1"), headers={"X-Owner": "other"})
    assert response.status_code == 200
    assert response.json()["uploaded_chunks"] == []


def test_single_upload_preserves_selected_target(uploads):
    client, calls, connections, _, _ = uploads
    response = client.post("/api/terminal/push", data={"worker_id": "selected", "path": "/chosen"},
                           files={"file": ("test.bin", b"ab")})
    assert response.status_code == 200, response.text
    assert calls == [(b"ab", "/chosen/test.bin")]
    assert connections[0]["host"] == "selected-host"


def test_explicit_directory_failure_does_not_fall_back_to_home(uploads):
    client, calls, _, _, sftp = uploads
    sftp.stat.side_effect = PermissionError("synthetic directory denial")
    response = client.post("/api/terminal/push", data=_data(chunk_index="0", total_chunks="1"),
                           files={"file": ("test.bin", b"ab")})
    assert response.status_code == 502
    assert calls == []
