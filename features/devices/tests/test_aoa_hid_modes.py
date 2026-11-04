"""AOA recover must pass profile HID mode through the existing dispatch port."""
import pytest

from .test_aoa_profiles import environment as environment


@pytest.mark.parametrize("metadata,mode,settle", [({}, "pointer", 2),
    ({"hid_session_mode": "per_command"}, "pointer", 2),
    ({"hid_touch_mode": "direct", "hid_settle_seconds": 4}, "direct", 4),
    ({"calibration": {"context": {"version": 2}}}, "direct", 2)])
def test_recover_preserves_hid_mode(environment, monkeypatch, metadata, mode, settle):
    from features.devices import aoa_api

    monkeypatch.setattr(aoa_api, "require_human_principal", lambda request: None)
    captured = {}

    async def submit(request, payload):
        captured.update(payload)
        return {"success": True}

    monkeypatch.setattr(aoa_api.cluster_port, "submit_device_action", submit)
    response = environment.post("/api/devices/aoa/recover", json={
        "worker_id": "w", "device": "ABC", "commands": ["click 180 525"], "metadata": metadata})
    assert response.status_code == 200, response.text
    assert captured["aoa_hid_touch_mode"] == mode
    assert captured["aoa_hid_settle_seconds"] == settle
    assert captured["aoa_commands"] == ["click 180 525"]
    assert captured["aoa_hid_session_mode"] == metadata.get("hid_session_mode", "continuous")
