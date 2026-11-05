"""Firmware continuation freezes account data and retains its original claims."""
import asyncio
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

    def test_dispatch_failure_marks_recovery_failed_and_keeps_burn_claim(self):
        """start_local_command 派发失败：命令置 failed 供 reconcile，
        不得停留 accepted；burn claim 保持不动（归烧写请求 finally 管）。"""
        prepared = self.prepare_local()
        with patch.object(self.repo, "start_local_command", side_effect=RuntimeError("sqlite busy")):
            receipt = asyncio.run(run_local_firmware_recovery(prepared))
        assert receipt["status"] == "failed"
        assert "reconcile" in (receipt.get("error") or "")
        assert self.repo.claims.active_claim("worker-246:ABC") is not None
