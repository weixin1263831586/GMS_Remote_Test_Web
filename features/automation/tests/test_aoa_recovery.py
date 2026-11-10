"""Recovery gates must fail closed before a GMS test can start."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from features.automation.aoa_snapshot import freeze_recovery
from features.automation.executors import HttpAutomationExecutor, StubAutomationExecutor
from features.automation.models import AutomationRunCreateRequest
from features.automation.orchestrator import AutomationOrchestrator
from features.automation.repository import AutomationStore

from .test_integrations import FakeResponse, FakeSession


@pytest.mark.parametrize("receipt", [None, "legacy", [], {}, {"success": True},
    {"success": True, "adb_state": "device"},
    {"success": True, "adb_recovered": True},
    {"success": True, "adb_recovered": True, "adb_state": "unauthorized"}])
def test_incomplete_recovery_receipt_never_starts_tests(tmp_path, receipt):
    class Executor(StubAutomationExecutor):
        def aoa_recover(self, run):
            return {"success": True, "aoa_result": receipt}

    store = AutomationStore(tmp_path / "runs.sqlite3")
    run = AutomationRunCreateRequest().to_run_dict("run")
    run["status"] = run["current_stage"] = "aoa_recovering"
    store.create_run(run)
    assert AutomationOrchestrator(store, Executor()).advance_run("run")["status"] == "aoa_failed"


def test_flash_receipt_does_not_probe_adb(tmp_path, monkeypatch):
    session = FakeSession()
    session.queue(FakeResponse(json_data={"command": {"status": "completed", "result": {"success": True}}}))
    executor = HttpAutomationExecutor(session=session)
    executor._verify_cluster_post_flash = Mock(side_effect=AssertionError("ADB must wait for recovery"))
    monkeypatch.setattr("features.cluster.get_cluster_service", lambda: SimpleNamespace(
        repository=SimpleNamespace(renew_reservation=lambda _: True)))
    result = executor.flash({"id": "run", "devices_json": '[{"serial":"w:ABC"}]',
        "device_reservation_id": "r", "worker_id": "w", "flash_command_id": "cmd"})
    assert result["success"] is True and result["running"] is False
    executor._verify_cluster_post_flash.assert_not_called()


def test_flash_aoa_verify_test_order(tmp_path):
    calls = []

    class Executor(StubAutomationExecutor):
        def flash(self, run):
            calls.append("flash")
            return {"success": True}

        def aoa_enabled(self, run):
            return True

        def aoa_recover(self, run):
            calls.append("aoa")
            return {"success": True, "aoa_result": {"success": True, "adb_recovered": True, "adb_state": "device"}}

        def verify_post_flash(self, run):
            calls.append("verify")
            return {"success": True, "verified": True}

        def start_test(self, run):
            calls.append("test")
            return {"success": True}

    store = AutomationStore(tmp_path / "runs.sqlite3")
    run = AutomationRunCreateRequest().to_run_dict("run")
    run["status"] = run["current_stage"] = "flashing"
    store.create_run(run)
    orchestrator = AutomationOrchestrator(store, Executor())
    for _ in range(6):
        orchestrator.advance_run("run")
    assert calls == ["flash", "aoa", "verify", "test"]


def test_flash_only_run_completes_without_test(tmp_path):
    """test_mode=skip：烧写→AOA→固件校验后收尾为 completed，绝不进测试。"""
    calls = []

    class Executor(StubAutomationExecutor):
        def flash(self, run):
            calls.append("flash")
            return {"success": True}

        def aoa_enabled(self, run):
            return True

        def aoa_recover(self, run):
            calls.append("aoa")
            return {"success": True, "aoa_result": {"success": True, "adb_recovered": True, "adb_state": "device"}}

        def verify_post_flash(self, run):
            calls.append("verify")
            return {"success": True, "verified": True}

        def start_test(self, run):  # pragma: no cover - flash-only must not test
            calls.append("test")
            return {"success": True}

    store = AutomationStore(tmp_path / "runs.sqlite3")
    run = AutomationRunCreateRequest().to_run_dict("run")
    run["status"] = run["current_stage"] = "flashing"
    run["test_plan_json"] = json.dumps({"test_mode": "skip", "flash": {"mode": "firmware"}})
    store.create_run(run)
    orchestrator = AutomationOrchestrator(store, Executor())
    for _ in range(6):
        result = orchestrator.advance_run("run")
    assert calls == ["flash", "aoa", "verify"], "flash-only run must never start a test"
    assert result["status"] == "completed"


def test_firmware_share_queued_run_stages_artifact_before_devices(tmp_path):
    """受控固件来源：先持久化下载阶段，解析完成后再进等待设备。"""
    staged_calls = []

    class Executor(StubAutomationExecutor):
        def resolve_firmware_share(self, run, *, on_progress=None):
            staged_calls.append(run["id"])
            return {"success": True, "artifact_url": "",
                    "artifact_path": "/stage/run/firmware.img"}

    store = AutomationStore(tmp_path / "runs.sqlite3")
    run = AutomationRunCreateRequest().to_run_dict("run")
    run["status"] = run["current_stage"] = "queued"
    run["test_plan_json"] = json.dumps({
        "test_mode": "skip",
        "flash": {"mode": "firmware", "firmware_share_id": "share-1"},
    })
    store.create_run(run)
    orchestrator = AutomationOrchestrator(store, Executor())
    assert orchestrator.advance_run("run")["status"] == "artifact_fetching"
    assert staged_calls == []
    result = orchestrator.advance_run("run")
    assert staged_calls == ["run"]
    assert result["status"] == "waiting_device"
    assert result["artifact_path"] == "/stage/run/firmware.img"


def test_firmware_share_resolution_failure_fails_run_as_artifact_missing(tmp_path):
    """分享不存在/过期/下载失败：进入 artifact_missing，不静默进烧写。"""

    class Executor(StubAutomationExecutor):
        def resolve_firmware_share(self, run, *, on_progress=None):
            return {"success": False, "error": "固件分享已过期: share-1"}

    store = AutomationStore(tmp_path / "runs.sqlite3")
    run = AutomationRunCreateRequest().to_run_dict("run")
    run["status"] = run["current_stage"] = "queued"
    run["test_plan_json"] = json.dumps({
        "flash": {"mode": "firmware", "firmware_share_id": "share-1"},
    })
    store.create_run(run)
    orchestrator = AutomationOrchestrator(store, Executor())
    assert orchestrator.advance_run("run")["status"] == "artifact_fetching"
    result = orchestrator.advance_run("run")
    assert result["status"] == "artifact_missing"


def test_profile_changes_do_not_change_prepared_run(tmp_path, monkeypatch):
    from features.automation.service import AutomationService

    profile = {"id": "p1", "name": "recovery", "commands": ["home"], "wait_adb_seconds": 60, "revision": 1}
    profiles = SimpleNamespace(list_profiles=lambda owner, **_: [profile] if owner == "alice" else [])
    monkeypatch.setattr("features.devices.profile_store", lambda: profiles)
    service = AutomationService(store=AutomationStore(tmp_path / "runs.sqlite3"), profiles_path=tmp_path / "plans.json")
    run, _ = service.prepare_run({"test_plan": {"test_type": "CTS", "flash": {"mode": "skip"},
        "aoa": {"mode": "user_build_recovery", "profile": "recovery"}}}, created_by="alice")
    profile["commands"] = ["back"]
    profile["revision"] = 2
    frozen = json.loads(run["test_plan_json"])["aoa"]
    assert frozen["commands"] == ["home"]
    assert frozen["profile_id"] == profile["id"]
    assert frozen["commands_sha256"]
    with pytest.raises(ValueError, match="creating account"):
        freeze_recovery({"aoa": {"mode": "user_build_recovery", "profile": "recovery"}}, "mallory")


def test_dispatch_timeout_preserves_operation_for_idempotent_followup():
    session = FakeSession()
    session.post = Mock(side_effect=TimeoutError("response lost"))
    executor = HttpAutomationExecutor(session=session)
    plan = {"aoa": {"mode": "user_build_recovery", "commands": ["home"]}}
    freeze_recovery(plan, "owner")
    result = executor.aoa_recover({"id": "run", "worker_id": "w",
        "devices_json": '[{"serial":"w:ABC","usb_port_path":"1-2"}]',
        "test_plan_json": json.dumps(plan)})
    assert result["success"] is True and result["running"] is True
    assert session.post.call_args.kwargs["json"]["automation_run_id"] == "run"


@pytest.mark.parametrize("capabilities", [{}, {"aoa_identity_fencing_v1": True, "aoa_usb_hid_v1": False}])
def test_flash_checks_recovery_capability_before_staging(capabilities, monkeypatch):
    session = FakeSession()
    executor = HttpAutomationExecutor(session=session)
    repository = SimpleNamespace(renew_reservation=lambda _: True,
        get_worker=lambda _: {"capabilities": capabilities}, find_correlated_command=lambda *_: None)
    cluster = SimpleNamespace(repository=repository, config=SimpleNamespace(local_worker_id="local"))
    monkeypatch.setattr("features.cluster.get_cluster_service", lambda: cluster)
    result = executor.flash({"id": "run", "devices_json": '[{"serial":"w:ABC","usb_port_path":"1-2"}]',
        "device_reservation_id": "r", "worker_id": "w", "artifact_path": "/tmp/update.img",
        "test_plan_json": json.dumps({"aoa": {"mode": "user_build_recovery"}})})
    assert result["success"] is False
    assert "AOA" in result["error"]
    assert not session.calls


@pytest.mark.parametrize("status_code", [400, 403, 409, 422])
def test_explicit_dispatch_rejection_does_not_retry_for_entire_budget(status_code):
    session = FakeSession()
    session.queue(FakeResponse(status_code=status_code, text="request rejected"))
    executor = HttpAutomationExecutor(session=session)
    plan = {"aoa": {"mode": "user_build_recovery", "commands": ["home"]}}
    freeze_recovery(plan, "owner")
    result = executor.aoa_recover({"id": "run", "worker_id": "w",
        "devices_json": '[{"serial":"w:ABC","usb_port_path":"1-2"}]',
        "test_plan_json": json.dumps(plan)})
    assert result["success"] is False
    assert not result.get("running")
    assert result["reconcile_required"] is True
    assert str(status_code) in result["error"]


@pytest.mark.parametrize("recovery", [[], {"mode": []}, {"mode": "user_build_recovery", "commands": 123},
    {"mode": "user_build_recovery", "commands": ["home"], "wait_adb_seconds": True},
    {"mode": "user_build_recovery", "commands": ["home"], "wait_adb_seconds": 60.5}])
def test_invalid_recovery_configuration_is_rejected_before_run_creation(recovery):
    with pytest.raises(ValueError):
        freeze_recovery({"aoa": recovery}, "owner")
