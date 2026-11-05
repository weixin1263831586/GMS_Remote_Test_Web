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
    monkeypatch.setattr(aoa_api, "principal_owner_id", lambda request: "owner-1")
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


def test_recover_forwards_embedded_mapping_and_source_context(environment, monkeypatch):
    from features.devices import aoa_api
    from foundation.aoa_calibration import five_point_calibration_points, mapping_from_samples

    monkeypatch.setattr(aoa_api, "require_human_principal", lambda request: None)
    monkeypatch.setattr(aoa_api, "principal_owner_id", lambda request: "owner-1")
    captured = {}

    async def submit(request, payload):
        captured.update(payload)
        return {"success": True}

    monkeypatch.setattr(aoa_api.cluster_port, "submit_device_action", submit)
    context = {"version": 3, "model": "RK3576", "release": "16",
               "display": [800, 1280], "rotation": 0, "hid_touch_mode": "direct"}
    mapping = mapping_from_samples(
        "direct",
        [((x, y), (x * 2.2, y * 2.2)) for x, y in five_point_calibration_points(360, 640)],
        context=context, display_pixels=(800, 1280),
        validation_samples=[((120, 213), (264, 468.6)), ((240, 427), (528, 939.4))],
    ).to_dict()
    response = environment.post("/api/devices/aoa/recover", json={
        "worker_id": "w", "device": "ABC", "commands": ["click 180 320"],
        "device_context": context,
        "metadata": {"hid_touch_mode": "direct", "calibration": {
            "context": context, "mapping": mapping}},
    })
    assert response.status_code == 200, response.text
    assert captured["aoa_touch_mapping"] == mapping
    assert captured["aoa_source_touch_mapping"] == mapping
