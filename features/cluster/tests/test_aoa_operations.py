"""Persistent local and remote AOA operations have the same replay contract."""
import unittest
from dataclasses import replace
from threading import Event
from unittest.mock import patch

from features.cluster import api as cluster_api
from features.cluster.device_action_operations import request_digest
from foundation.error_model import ApiError

from .api_hardening_fixture import ClusterApiHardeningFixture


class AoaOperationsTests(ClusterApiHardeningFixture, unittest.TestCase):
    def test_worker_without_per_command_support_is_rejected(self):
        response = self.client.post("/api/cluster/devices/actions", json={
            "worker_id": "worker-246", "devices": ["ABC"], "action": "aoa_sequence",
            "aoa_commands": ["click 180 525"], "aoa_hid_session_mode": "per_command"})
        assert response.status_code == 409
        assert "按单条方式" in response.json()["error"]

    def test_old_worker_cannot_silently_ignore_pointer_mode(self):
        worker = self.repo.get_worker("worker-246")
        with patch.object(self.repo, "get_worker", return_value={**worker, "capabilities": {"aoa_identity_fencing_v1": True}}):
            response = self.client.post("/api/cluster/devices/actions", json={
                "worker_id": "worker-246", "devices": ["ABC"], "action": "aoa_sequence",
                "aoa_commands": ["click 180 525"], "aoa_hid_touch_mode": "pointer"})
        assert response.status_code == 409
        assert "输入模式" in response.json()["error"]

    def prepare(self):
        self.repo.reserve_devices("worker-246", ["worker-246:ABC"], owner_id="admin-id", source_id="run")
        return {"worker_id": "worker-246", "devices": ["ABC"], "action": "aoa_sequence",
                "aoa_commands": ["home"], "automation_run_id": "run"}

    def test_replay_rejects_changed_content_and_foreign_account(self):
        body = self.prepare()
        first = self.client.post("/api/cluster/devices/actions", json=body)
        assert first.status_code == 200
        for changes in ({"aoa_commands": ["back"]}, {"wait_adb_seconds": 30}):
            response = self.client.post("/api/cluster/devices/actions", json={**body, **changes})
            assert response.status_code == 409
            assert response.json()["code"] == "STATE_CONFLICT"
        foreign = self.client.post("/api/cluster/devices/actions", json=body, headers={"X-Test-User": "bob"})
        assert foreign.status_code == 409
        assert "command_id" not in foreign.json()

    def test_local_response_loss_follows_one_running_receipt(self):
        body = self.prepare()
        cluster_api.cluster_service.config = replace(cluster_api.cluster_service.config, local_worker_id="worker-246")
        started, finished, release = Event(), Event(), Event()
        calls = []

        def execute(*args, **kwargs):
            calls.append(args)
            started.set()
            release.wait(5)
            finished.set()
            return {"success": True, "adb_recovered": True, "adb_state": "device"}

        try:
            with patch("worker_agent.inventory.execute_device_action", side_effect=execute):
                first = self.client.post("/api/cluster/devices/actions", json=body)
                assert started.wait(2)
                second = self.client.post("/api/cluster/devices/actions", json=body)
                assert first.json()["command_id"] == second.json()["command_id"]
                assert second.json()["deduplicated"] is True
                assert len(calls) == 1
                assert self.repo.get_command(first.json()["command_id"])["status"] == "running"
        finally:
            release.set()
            assert finished.wait(2)
            # Join the worker before the temporary database is removed.
            import threading
            for thread in threading.enumerate():
                if thread.name.startswith("LocalAction-"):
                    thread.join(2)

    def test_create_command_checks_binding_inside_transaction(self):
        payload = {"owner_id": "alice", "request_sha256": request_digest("worker-246", ["ABC"], {}, "alice")}
        data = {"worker_id": "worker-246", "command_type": "device_action", "operation_id": "same", "payload": payload}
        command = self.repo.create_command(data)
        assert self.repo.create_command(data)["id"] == command["id"]
        with self.assertRaises(ApiError):
            self.repo.create_command({**data, "payload": {**payload, "request_sha256": "different"}})
        with self.assertRaises(ApiError):
            self.repo.create_command({**data, "payload": {**payload, "owner_id": "bob"}})

    def test_changed_serial_cannot_escape_physical_port_claim(self):
        self.repo.heartbeat("worker-246", {"devices": [{"serial": "ABC", "state": "available",
            "transport": "local_usb", "properties": {"usb": "1-2"}}]})
        self.repo.acquire_device_operation_claim("worker-246", ["worker-246:ABC"],
            owner_id="alice", source_type="cluster-device-action", source_id="first")
        self.repo.heartbeat("worker-246", {"devices": [{"serial": "NEW", "state": "available",
            "transport": "local_usb", "properties": {"usb": "1-2"}}]})
        with self.assertRaises(ValueError):
            self.repo.acquire_device_operation_claim("worker-246", ["worker-246:NEW"],
                owner_id="bob", source_type="cluster-device-action", source_id="second")

    def test_legacy_worker_cannot_ignore_physical_identity_contract(self):
        body = self.prepare()
        worker = self.repo.get_worker("worker-246")
        with patch.object(self.repo, "get_worker", return_value={**worker, "capabilities": {}}):
            response = self.client.post("/api/cluster/devices/actions", json=body)
        assert response.status_code == 409
        assert self.repo.get_command_by_operation("worker-246", "automation:run:aoa_sequence") is None

    def test_local_calibration_validates_claim_before_usb_reenumeration(self):
        cluster_api.cluster_service.config = replace(cluster_api.cluster_service.config, local_worker_id="worker-246")

        def execute(_action, _devices, payload):
            check = payload.get("_validate_fencing")
            assert callable(check)
            check()
            self.repo.claims.release(payload["claim_source_id"])
            with self.assertRaises(RuntimeError):
                check()
            return {"success": True}

        with patch("worker_agent.inventory.execute_device_action", side_effect=execute):
            response = self.client.post("/api/cluster/devices/actions", json={
                "worker_id": "worker-246", "devices": ["ABC"], "action": "aoa_record_input",
                "aoa_record": {"action": "calibrate"},
            })
        assert response.status_code == 200, response.text
