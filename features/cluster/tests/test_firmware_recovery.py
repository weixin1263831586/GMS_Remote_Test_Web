"""Firmware continuation freezes account data and retains its original claims."""
import asyncio
import hashlib
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from features.auth import CurrentUser
from features.cluster import api as cluster_api
from features.cluster.firmware_recovery import (
    freeze_firmware_recovery,
    prepare_local_firmware_recovery,
    release_local_firmware_recovery,
    run_local_firmware_recovery,
)
from foundation.error_model import ApiError

from .api_hardening_fixture import ClusterApiHardeningFixture


class FirmwareRecoveryTests(ClusterApiHardeningFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.request = SimpleNamespace(state=SimpleNamespace(current_user=CurrentUser(
            id="admin-id", username="admin", role="admin")))
        self.profile = {"id": "profile-1", "name": "wizard", "revision": 2,
                        "commands": ["home"], "wait_adb_seconds": 60, "metadata": {}}
        worker = self.repo.get_worker("worker-246")
        self.caps = {key: True for key in ("aoa_identity_fencing_v1", "aoa_usb_hid_v1",
                                         "aoa_record_input_v1", "firmware_aoa_recovery_v1", "aoa_hid_modes_v1")}
        self.worker_patch = patch.object(self.repo, "get_worker", return_value={**worker, "capabilities": self.caps})
        self.worker_patch.start()
        self.profile_store = Mock()
        self.profile_store.list_profiles.return_value = [self.profile]
        self.store_patch = patch("features.cluster.firmware_recovery.profile_store", return_value=self.profile_store)
        self.store_patch.start()
        self.usb_patch = patch("features.cluster.firmware_recovery.usbip_source_host_for_device", return_value="")
        self.usb_patch.start()
        self.addCleanup(self.worker_patch.stop)
        self.addCleanup(self.store_patch.stop)
        self.addCleanup(self.usb_patch.stop)
        self.repo.heartbeat("worker-246", {"devices": [{"serial": "ABC", "state": "available",
                             "transport": "local_usb", "properties": {"usb": "1-6"}}]})

    def freeze(self, **kwargs):
        return freeze_firmware_recovery(self.request, self.repo, "worker-246", ["ABC"], "profile-1", **kwargs)

    def prepare_verified_touch(self):
        from tests.aoa_fixtures import firmware_declaration, verified_mapping

        source, target = verified_mapping(), verified_mapping()
        target.context.update({"release": "17", "fingerprint": "tested-user-firmware"})
        self.profile["commands"] = ["click 180 320"]
        self.profile["metadata"] = {"hid_touch_mode": "direct", "calibration": {"mapping": source.to_dict()},
            "firmware_compatibility": firmware_declaration(target.context, hashlib.sha256(b"firmware").hexdigest())}
        self.caps["aoa_affine_mapping_v2"] = True
        self.repo.heartbeat("worker-246", {"devices": [{"serial": "ABC", "state": "available",
            "transport": "local_usb", "properties": {"usb": "1-6", "aoa_context": source.context}}]})
        store = SimpleNamespace(find_matching=lambda owner, context: (
            {"mapping": target.to_dict(), "mapping_policy": "direct_calibrated"}
            if owner == "admin-id" and context == target.context else None))
        self.target_store_patch = patch("features.devices.aoa_mapping_service.calibration_store", return_value=store)
        self.target_store_patch.start()
        self.addCleanup(self.target_store_patch.stop)
        return target

    def test_post_flash_mapping_uses_declared_firmware_instead_of_old_build(self):
        target = self.prepare_verified_touch()
        frozen = self.freeze()
        assert frozen["target_context"] == target.context
        assert frozen["target_context"]["fingerprint"] == "tested-user-firmware"
        assert frozen["mapping_options"]["aoa_touch_mapping"] == target.to_dict()

    def test_wrong_firmware_bytes_never_create_a_flash_command_and_release_claim(self):
        self.prepare_verified_touch()
        with patch.object(self.repo, "create_command", wraps=self.repo.create_command) as create:
            response = self.client.post("/api/cluster/firmware/stage", data={
                "worker_id": "worker-246", "devices": "ABC", "aoa_profile_id": "profile-1"},
                files={"firmware_file": ("update.img", b"different-firmware")})
        assert response.status_code == 409, response.text
        assert "SHA-256" in response.json()["error"]
        create.assert_not_called()
        assert not self.repo.claims.list_active()

    def test_declared_firmware_is_bound_to_the_staged_command(self):
        self.prepare_verified_touch()
        response = self.client.post("/api/cluster/firmware/stage", data={
            "worker_id": "worker-246", "devices": "ABC", "aoa_profile_id": "profile-1"},
            files={"firmware_file": ("update.img", b"firmware")})
        assert response.status_code == 200, response.text
        command = self.repo.get_command(response.json()["command_id"])
        assert command["payload"]["sha256"] == command["payload"]["aoa_recovery"]["firmware_compatibility"]["sha256"]
        self.repo.claims.release(command["payload"]["claim_source_id"])

    def test_preflight_allows_legacy_touch_profile_and_stays_read_only(self):
        # 强制声明闸门已降级为选择性强化：legacy 触摸 Profile 预检放行，
        # 按未验证估算回放（estimated），预检本身不产生烧写命令或占用。
        self.profile["commands"] = ["click 180 320"]
        with patch.object(self.repo, "create_command", wraps=self.repo.create_command) as create:
            response = self.client.post("/api/cluster/firmware/preflight", json={
                "worker_id": "worker-246", "devices": ["ABC"], "aoa_profile_id": "profile-1"})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["ready"] is True
        assert payload["mapping_policy"] == "estimated"
        assert payload["expected_firmware_sha256"] == ""
        create.assert_not_called()
        assert not self.repo.claims.list_active()

    def test_preflight_uses_actual_target_and_returns_only_readiness_summary(self):
        self.prepare_verified_touch()
        with patch.object(self.repo, "create_command", wraps=self.repo.create_command) as create:
            response = self.client.post("/api/cluster/firmware/preflight", json={
                "worker_id": "worker-246", "devices": ["ABC"], "aoa_profile_id": "profile-1"})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["ready"] is True
        assert payload["expected_firmware_sha256"] == hashlib.sha256(b"firmware").hexdigest()
        assert payload["mapping_policy"] == "direct_calibrated"
        assert "mapping_options" not in payload and "metadata" not in payload
        create.assert_not_called()
        assert not self.repo.claims.list_active()

    def test_preflight_default_worker_matches_local_controller_configuration(self):
        service = cluster_api.cluster_service
        with patch.object(service, "config", replace(service.config, local_worker_id="worker-246")):
            response = self.client.post("/api/cluster/firmware/preflight", json={
                "devices": ["ABC"], "aoa_profile_id": "profile-1"})
        assert response.status_code == 200, response.text
        assert response.json()["worker_id"] == "worker-246"
        assert not self.repo.claims.list_active()

    def test_preflight_rejects_agent_and_foreign_profile(self):
        body = {"worker_id": "worker-246", "devices": ["ABC"], "aoa_profile_id": "profile-1"}
        response = self.client.post("/api/cluster/firmware/preflight", json=body, headers={"X-Test-Role": "agent_service"})
        assert response.status_code == 403, response.text
        self.profile_store.list_profiles.side_effect = lambda owner, include_all: [self.profile] if owner == "admin-id" else []
        response = self.client.post("/api/cluster/firmware/preflight", json=body, headers={"X-Test-User": "other"})
        assert response.status_code == 404, response.text
        assert not self.repo.claims.list_active()

    def test_automation_digest_field_is_checked_without_a_manual_profile(self):
        response = self.client.post("/api/cluster/firmware/stage", data={
            "worker_id": "worker-246", "devices": "ABC", "aoa_firmware_sha256": "a" * 64},
            files={"firmware_file": ("update.img", b"firmware")})
        assert response.status_code == 409, response.text
        assert not self.repo.claims.list_active()

    def test_freeze_uses_only_creating_account_and_copies_commands(self):
        frozen = self.freeze()
        self.profile_store.list_profiles.assert_called_once_with("admin-id", False)
        self.profile["commands"].append("back")
        assert frozen["commands"] == ["home"]
        assert frozen["profile_revision"] == 2 and frozen["usb_port_path"] == "1-6"

    def test_legacy_worker_rejected_before_flash_staging(self):
        self.caps.pop("firmware_aoa_recovery_v1")
        with self.assertRaises(ApiError) as error:
            self.freeze()
        assert error.exception.status_code == 409
        assert not self.repo.claims.list_active()

    def test_freeze_uses_target_environment_and_rejects_source_as_target(self):
        from tests.aoa_fixtures import verified_mapping

        mapping = verified_mapping().to_dict()
        self.caps["aoa_affine_mapping_v2"] = True
        self.profile["metadata"] = {"hid_touch_mode": "direct", "calibration": {"mapping": mapping}}
        with self.assertRaises(ApiError):
            self.freeze()  # The source Profile cannot supply missing target evidence.
        self.repo.heartbeat("worker-246", {"devices": [{"serial": "ABC", "state": "available",
            "transport": "local_usb", "properties": {"usb": "1-6", "aoa_context": mapping["context"]}}]})
        frozen = self.freeze()
        assert frozen["mapping_options"]["aoa_touch_mapping"] == mapping
        assert frozen["target_context"]["fingerprint"] == "build-a"

    def test_agent_burn_approval_does_not_authorize_hid(self):
        for role, identity in (("agent_service", "agent-1"), ("admin", "automation:run-1")):
            self.request.state.current_user = CurrentUser(id=identity, username="agent", role=role)
            with self.assertRaises(ApiError) as error:
                self.freeze()
            assert error.exception.status_code == 403

    def test_foreign_profile_and_invalid_wait_rejected(self):
        with self.assertRaises(ApiError):
            self.freeze(boot_wait_seconds="1.5")
        self.profile_store.list_profiles.return_value = []
        with self.assertRaises(ApiError) as error:
            self.freeze()
        assert error.exception.status_code == 404

    def test_staged_remote_command_contains_frozen_profile_and_original_fence(self):
        staged = self.client.post("/api/cluster/firmware/stage", data={
            "worker_id": "worker-246", "devices": "ABC", "aoa_profile_id": "profile-1",
            "aoa_boot_wait_seconds": "30"}, files={"firmware_file": ("update.img", b"firmware")})
        assert staged.status_code == 200, staged.text
        command = self.repo.get_command(staged.json()["command_id"])
        payload = command["payload"]
        assert payload["aoa_recovery"]["commands"] == ["home"]
        assert payload["aoa_recovery"]["boot_wait_seconds"] == 30
        assert payload["lease_tokens"][0]["lease_id"] == self.repo.claims.active_claim("worker-246:ABC")["id"]
        self.repo.claims.release(payload["claim_source_id"])

    def prepare_local(self):
        cluster_api.cluster_service.config = replace(cluster_api.cluster_service.config, local_worker_id="worker-246")
        self.repo.claims.acquire([{"device_key": "worker-246:ABC", "worker_id": "worker-246", "serial": "ABC"}],
            owner_id="browser-client", username="admin", source_id="firmware:browser-client",
            source_type="firmware", ttl_seconds=3600)
        return prepare_local_firmware_recovery(self.request, "browser-client", ["ABC"], "profile-1", 0)

    def test_local_receipt_runs_once_and_keeps_original_burn_lock(self):
        prepared = self.prepare_local()
        execute = Mock(return_value={"success": True, "adb_recovered": True})
        with patch("worker_agent.inventory.execute_device_action", execute):
            first = asyncio.run(run_local_firmware_recovery(prepared))
            second = asyncio.run(run_local_firmware_recovery(prepared))
        assert first["success"] is True and first["command_id"] == second["command_id"]
        assert execute.call_count == 1
        assert self.repo.claims.active_claim("worker-246:ABC") is not None
        assert self.repo.claims.active_claim("worker-246:@usb:1-6") is not None
        release_local_firmware_recovery(prepared)
        assert self.repo.claims.active_claim("worker-246:@usb:1-6") is None
        assert self.repo.claims.active_claim("worker-246:ABC") is not None

    def test_lost_burn_claim_never_sends_hid(self):
        prepared = self.prepare_local()
        self.repo.claims.release("firmware:browser-client")
        with patch("worker_agent.inventory.execute_device_action") as execute, self.assertRaises(RuntimeError):
            asyncio.run(run_local_firmware_recovery(prepared))
        execute.assert_not_called()

    def test_completed_input_without_adb_is_failed_recovery(self):
        prepared = self.prepare_local()
        with patch("worker_agent.inventory.execute_device_action", return_value={"success": True, "adb_recovered": False}):
            result = asyncio.run(run_local_firmware_recovery(prepared))
        assert result["success"] is False and result["reconcile_required"] is True

    def test_connected_adb_with_unconfirmed_native_usb_reports_usb_failure(self):
        prepared = self.prepare_local()
        receipt = {"success": False, "adb_recovered": True, "adb_state": "device",
                   "native_mode_restored": False, "commands_executed": 1, "commands_total": 1}
        with patch("worker_agent.inventory.execute_device_action", return_value=receipt):
            result = asyncio.run(run_local_firmware_recovery(prepared))
        assert result["success"] is False
        assert "ADB 已连接" in result["error"] and "USB" in result["error"]
        assert "尚未恢复或尚未授权" not in result["error"]

    def test_dispatch_failure_marks_recovery_failed_and_keeps_burn_claim(self):
        """start_local_command 派发失败：命令置 failed 供 reconcile，
        不得停留 accepted；burn claim 保持不动（归烧写请求 finally 管）。"""
        prepared = self.prepare_local()
        with patch.object(self.repo, "start_local_command", side_effect=RuntimeError("sqlite busy")):
            receipt = asyncio.run(run_local_firmware_recovery(prepared))
        assert receipt["status"] == "failed"
        assert "reconcile" in (receipt.get("error") or "")
        assert self.repo.claims.active_claim("worker-246:ABC") is not None
