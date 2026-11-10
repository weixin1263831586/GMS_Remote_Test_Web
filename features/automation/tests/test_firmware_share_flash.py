"""Share-only ATS runs must stage the selected image before dispatching flash."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from features.automation.executors import HttpAutomationExecutor
from features.automation.tests.test_integrations import FakeResponse, FakeSession


def _run():
    return {
        "id": "ats_share", "worker_id": "worker-1", "device_reservation_id": "reservation-1",
        "devices_json": '[{"serial":"worker-1:ABC"}]',
        "test_plan_json": json.dumps({"flash": {"mode": "firmware", "firmware_share_id": "share-1"}}),
    }


def test_share_only_run_uploads_image_with_the_original_device_reservation(tmp_path):
    image = tmp_path / "update.img"
    image.write_bytes(b"shared-firmware")
    session = FakeSession()
    session.queue(FakeResponse(json_data={"success": True, "stage_id": "stage-1", "command_id": "cmd-1"}))
    resolver = Mock(return_value={"ok": True, "firmware_path": str(image)})
    cluster = SimpleNamespace(repository=SimpleNamespace(renew_reservation=lambda reservation: reservation == "reservation-1"))
    with patch("features.cluster.get_cluster_service", return_value=cluster), \
            patch("features.firmware.resolve_firmware_share_record", resolver), \
            patch("foundation.config.config_manager.load_config", return_value={
                "firmware_shares": {"allowed_prefixes": [str(tmp_path)]},
            }):
        result = HttpAutomationExecutor(base_url="http://controller", session=session).flash(_run())
    assert result["success"] is True
    assert result["command_id"] == "cmd-1"
    assert result["running"] is True
    assert resolver.call_args.args == ("share-1",)
    assert resolver.call_args.kwargs["run_id"] == "ats_share"
    fields = session.calls[0][2]["data"].fields
    assert fields["worker_id"] == "worker-1"
    assert fields["devices"] == "worker-1:ABC"
    assert fields["reservation_id"] == "reservation-1"
    assert fields["automation_run_id"] == "ats_share"
    assert fields["firmware_file"][0] == "update.img"
    assert not image.exists()


def test_expired_share_stops_before_flash_dispatch():
    session = FakeSession()
    cluster = SimpleNamespace(repository=SimpleNamespace(renew_reservation=lambda reservation: True))
    with patch("features.cluster.get_cluster_service", return_value=cluster), \
            patch("features.firmware.resolve_firmware_share_record", return_value={
                "ok": False, "reason": "固件分享已过期: share-1",
            }):
        result = HttpAutomationExecutor(base_url="http://controller", session=session).flash(_run())
    assert result == {"success": False, "error": "固件分享已过期: share-1"}
    assert session.calls == []


def test_polling_existing_flash_command_does_not_download_the_share_again():
    session = FakeSession()
    session.queue(FakeResponse(json_data={"success": True, "command": {
        "id": "cmd-1", "status": "running",
    }}))
    cluster = SimpleNamespace(repository=SimpleNamespace(renew_reservation=lambda reservation: True))
    with patch("features.cluster.get_cluster_service", return_value=cluster), \
            patch("features.firmware.resolve_firmware_share_record") as resolve:
        result = HttpAutomationExecutor(base_url="http://controller", session=session).flash({
            **_run(), "flash_command_id": "cmd-1",
        })
    assert result["running"] is True
    resolve.assert_not_called()
    assert [call[0] for call in session.calls] == ["GET"]


def test_flash_reuses_the_persisted_share_artifact(tmp_path):
    image = tmp_path / "update.img"
    image.write_bytes(b"shared-firmware")
    session = FakeSession()
    session.queue(FakeResponse(json_data={"success": True, "stage_id": "stage-1", "command_id": "cmd-1"}))
    cluster = SimpleNamespace(repository=SimpleNamespace(renew_reservation=lambda reservation: True))
    with patch("features.cluster.get_cluster_service", return_value=cluster), \
            patch("features.firmware.resolve_firmware_share_record") as resolve, \
            patch("foundation.config.config_manager.load_config", return_value={
                "firmware_shares": {"allowed_prefixes": [str(tmp_path)]},
            }):
        result = HttpAutomationExecutor(base_url="http://controller", session=session).flash({
            **_run(), "artifact_path": str(image),
        })
    assert result["success"] is True
    resolve.assert_not_called()
    assert image.read_bytes() == b"shared-firmware"


def test_queued_share_is_resolved_before_any_device_claim(tmp_path):
    from features.automation.executor_contract import StubAutomationExecutor
    from features.automation.models import AutomationRunCreateRequest
    from features.automation.orchestrator import AutomationOrchestrator
    from features.automation.repository import AutomationStore

    store = AutomationStore(tmp_path / "automation.sqlite3")
    request = AutomationRunCreateRequest(test_plan={"flash": {"firmware_share_id": "share-1"}})
    store.create_run(request.to_run_dict("ats_share"))
    executor = StubAutomationExecutor()
    executor.select_devices = Mock(side_effect=AssertionError("devices must stay unclaimed during download"))
    orchestrator = AutomationOrchestrator(store, executor)
    assert orchestrator.advance_run("ats_share")["status"] == "artifact_fetching"
    result = orchestrator.advance_run("ats_share")
    assert result["status"] == "waiting_device"
    assert result["artifact_path"] == "/stub/firmware-share/update.img"
    executor.select_devices.assert_not_called()


def test_share_resolution_failure_is_recorded_as_missing_artifact(tmp_path):
    from features.automation.executor_contract import StubAutomationExecutor
    from features.automation.models import AutomationRunCreateRequest
    from features.automation.orchestrator import AutomationOrchestrator
    from features.automation.repository import AutomationStore

    store = AutomationStore(tmp_path / "automation.sqlite3")
    request = AutomationRunCreateRequest(test_plan={"flash": {"firmware_share_id": "share-1"}})
    store.create_run(request.to_run_dict("ats_share"))
    executor = StubAutomationExecutor(fail_stage="resolve_firmware_share")
    orchestrator = AutomationOrchestrator(store, executor)
    assert orchestrator.advance_run("ats_share")["status"] == "artifact_fetching"
    result = orchestrator.advance_run("ats_share")
    assert result["status"] == "artifact_missing"
    assert "resolve_firmware_share" in result["error"]
