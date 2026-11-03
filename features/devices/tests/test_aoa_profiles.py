"""AOA Profile 存储与 API 的回归测试。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.devices import aoa_service, deployment_service
from features.devices.aoa_api import router as aoa_router
from foundation.error_model import register_api_error_handler


VALID_COMMANDS = ["wake", "home", "click 100 200", "key dpad_down dpad_center", "sleep 300"]


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deployment_service, "store",
        lambda: type("S", (), {"root": tmp_path})(),
    )
    app = FastAPI()
    register_api_error_handler(app)
    app.include_router(aoa_router)
    return TestClient(app)


def _auth(client):
    from features.auth import CurrentUser

    user = CurrentUser(id="owner-1", username="tester", role="user")

    class FakeRequest:
        state = type("S", (), {"current_user": user})()

    return FakeRequest()


def test_profile_crud_roundtrip(environment, monkeypatch):
    client = environment
    monkeypatch.setattr(
        "features.devices.aoa_api.require_human_principal",
        lambda request: type("U", (), {"id": "owner-1", "username": "t",
                                       "role": "user",
                                       "resource_owner_id": "owner-1"})(),
    )
    monkeypatch.setattr(
        "features.devices.aoa_api.principal_owner_id",
        lambda request: "owner-1",
    )
    created = client.post("/api/devices/aoa/profiles", json={
        "name": "rk-user-recovery", "description": "RK3576 user build",
        "commands": VALID_COMMANDS, "wait_adb_seconds": 60,
    })
    assert created.status_code == 200, created.text
    profile = created.json()["profile"]
    assert profile["commands"] == VALID_COMMANDS

    listed = client.get("/api/devices/aoa/profiles").json()["profiles"]
    assert [item["id"] for item in listed] == [profile["id"]]

    duplicate = client.post("/api/devices/aoa/profiles", json={
        "name": "rk-user-recovery", "commands": VALID_COMMANDS})
    assert duplicate.status_code == 409

    updated = client.put(f"/api/devices/aoa/profiles/{profile['id']}", json={
        "wait_adb_seconds": 120})
    assert updated.json()["profile"]["wait_adb_seconds"] == 120

    deleted = client.delete(f"/api/devices/aoa/profiles/{profile['id']}")
    assert deleted.status_code == 200
    assert client.get("/api/devices/aoa/profiles").json()["profiles"] == []


def test_invalid_commands_rejected_at_save(environment, monkeypatch):
    client = environment
    monkeypatch.setattr(
        "features.devices.aoa_api.require_human_principal",
        lambda request: type("U", (), {"id": "owner-1", "username": "t",
                                       "role": "user",
                                       "resource_owner_id": "owner-1"})(),
    )
    monkeypatch.setattr(
        "features.devices.aoa_api.principal_owner_id",
        lambda request: "owner-1",
    )
    response = client.post("/api/devices/aoa/profiles", json={
        "name": "bad", "commands": ["fly 1 2"]})
    assert response.status_code == 422, response.text


def test_store_rejects_invalid_commands_directly(tmp_path):
    store = aoa_service.AoaProfileStore(tmp_path)
    with pytest.raises(ValueError):
        store.create_profile("owner-1", "bad", "", ["key UNKNOWN"])


def test_record_presets_exposed(environment, monkeypatch):
    client = environment
    monkeypatch.setattr(
        "features.devices.aoa_api.require_human_principal",
        lambda request: type("U", (), {"id": "owner-1", "username": "t",
                                       "role": "user",
                                       "resource_owner_id": "owner-1"})(),
    )
    data = client.get("/api/devices/aoa/record/presets").json()
    assert data["success"] is True
    assert "usb_debugging" in data["presets"]
    steps = data["presets"]["usb_debugging"]
    assert steps and steps[0]["action"] in ("wake", "scroll_down", "click")


def test_record_with_preset_expands_steps(environment, monkeypatch):
    client = environment
    monkeypatch.setattr(
        "features.devices.aoa_api.require_human_principal",
        lambda request: type("U", (), {"id": "owner-1", "username": "t",
                                       "role": "user",
                                       "resource_owner_id": "owner-1"})(),
    )
    captured: dict = {}

    async def fake_device_action(request, payload):
        captured["action"] = payload["action"]
        captured["aoa_record"] = payload["aoa_record"]
        return {"success": True, "accepted": True}

    monkeypatch.setattr(
        "features.devices.aoa_api.cluster_port.submit_device_action",
        fake_device_action)
    response = client.post("/api/devices/aoa/record", json={
        "worker_id": "ats-worker-controller", "device": "ser-1",
        "preset": "usb_debugging",
    })
    assert response.status_code == 200, response.text
    assert captured["action"] == "aoa_record"
    steps = captured["aoa_record"]["steps"]
    assert any(step.get("find", {}).get("any_text") for step in steps)


def test_record_rejects_unknown_preset(environment, monkeypatch):
    client = environment
    monkeypatch.setattr(
        "features.devices.aoa_api.require_human_principal",
        lambda request: type("U", (), {"id": "owner-1", "username": "t",
                                       "role": "user",
                                       "resource_owner_id": "owner-1"})(),
    )
    response = client.post("/api/devices/aoa/record", json={
        "worker_id": "ats-worker-controller", "device": "ser-1",
        "preset": "no_such_preset",
    })
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_SEMANTICS"


def test_record_rejects_preset_and_steps_together(environment, monkeypatch):
    client = environment
    monkeypatch.setattr(
        "features.devices.aoa_api.require_human_principal",
        lambda request: type("U", (), {"id": "owner-1", "username": "t",
                                       "role": "user",
                                       "resource_owner_id": "owner-1"})(),
    )
    response = client.post("/api/devices/aoa/record", json={
        "worker_id": "ats-worker-controller", "device": "ser-1",
        "preset": "usb_debugging",
        "steps": [{"action": "wake", "wait_ms": 500}],
    })
    assert response.status_code == 400
    assert response.json()["code"] == "MALFORMED_REQUEST"


def test_interactive_record_input_uses_device_action_boundary(environment, monkeypatch):
    client = environment
    monkeypatch.setattr(
        "features.devices.aoa_api.require_human_principal",
        lambda request: type("U", (), {"id": "owner-1", "username": "t",
                                       "role": "user",
                                       "resource_owner_id": "owner-1"})(),
    )
    captured: dict = {}

    async def fake_submit(request, payload):
        captured.update(payload)
        return {"success": True, "command": "click 180 320"}

    monkeypatch.setattr(
        "features.devices.aoa_api.cluster_port.submit_device_action", fake_submit)
    response = client.post("/api/devices/aoa/record/input", json={
        "worker_id": "ats-worker-controller",
        "device": "ser-1",
        "input": {"action": "tap", "x": 400, "y": 600},
    })
    assert response.status_code == 200, response.text
    # with_screenshot 由 API 层注入（平台控制标志，客户端不可伪造）：
    # 输入执行后 Worker 顺带回传最新截图，前端免第二条截图命令。
    assert captured == {
        "worker_id": "ats-worker-controller",
        "devices": ["ser-1"],
        "action": "aoa_record_input",
        "aoa_record": {"action": "tap", "x": 400, "y": 600, "with_screenshot": True},
    }


def test_interactive_record_input_rejects_unlisted_action(environment, monkeypatch):
    client = environment
    monkeypatch.setattr(
        "features.devices.aoa_api.require_human_principal",
        lambda request: type("U", (), {"id": "owner-1", "username": "t",
                                       "role": "user",
                                       "resource_owner_id": "owner-1"})(),
    )
    response = client.post("/api/devices/aoa/record/input", json={
        "worker_id": "ats-worker-controller", "device": "ser-1",
        "input": {"action": "shell", "command": "id"},
    })
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_SEMANTICS"


def test_record_preserves_deferred_calibration(environment, monkeypatch):
    captured = {}
    monkeypatch.setattr("features.devices.aoa_api.require_human_principal", lambda _: None)

    async def submit(_request, payload):
        captured.update(payload)
        return {"success": True}

    monkeypatch.setattr("features.devices.aoa_api.cluster_port.submit_device_action", submit)
    response = environment.post("/api/devices/aoa/record", json={
        "worker_id": "worker", "device": "serial", "steps": [{"action": "home"}],
        "calibrate_touch": "after"})
    assert response.status_code == 200
    assert captured["aoa_record"]["calibrate_touch"] == "after"
