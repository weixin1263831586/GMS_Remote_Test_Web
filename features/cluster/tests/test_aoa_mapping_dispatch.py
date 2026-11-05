"""Exercise the real /recover -> request model -> durable Worker payload boundary."""
import unittest
from unittest.mock import patch

from features.cluster import _port_device_action_request
from features.devices import aoa_api
from foundation import cluster_port
from tests.aoa_fixtures import verified_mapping

from .api_hardening_fixture import ClusterApiHardeningFixture


class AoaMappingDispatchTests(ClusterApiHardeningFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.mapping = verified_mapping().to_dict()
        worker = self.repo.get_worker("worker-246")
        self.repo.register_worker({**worker, "worker_id": "worker-246", "capabilities": {
            **worker["capabilities"], "aoa_affine_mapping_v2": True}})
        self.client.app.include_router(aoa_api.router)
        self.repo.heartbeat("worker-246", {"devices": [{"serial": "ABC", "state": "available",
            "transport": "local_usb", "properties": {"usb": "1-2", "aoa_context": self.mapping["context"]}}]})
        self.port = patch.object(cluster_port, "_device_action_request", _port_device_action_request)
        self.port.start()
        self.addCleanup(self.port.stop)

    def test_recover_preserves_both_mappings_through_real_model_and_receipt(self):
        with patch("features.cluster.device_actions_api.threading.Thread"):
            response = self.client.post("/api/devices/aoa/recover", json={
                "worker_id": "worker-246", "device": "ABC", "commands": ["click 180 320"],
                "device_context": self.mapping["context"],
                "metadata": {"hid_touch_mode": "direct", "calibration": {"mapping": self.mapping}}})
        assert response.status_code == 200, response.text
        payload = self.repo.get_command(response.json()["command_id"])["payload"]
        assert payload["aoa_touch_mapping"] == self.mapping
        assert payload["aoa_source_touch_mapping"] == self.mapping
        assert payload["aoa_mapping_policy"] == "direct_calibrated"

    def test_wrong_mode_and_oversized_mapping_rejected_before_receipt(self):
        body = {"worker_id": "worker-246", "devices": ["ABC"], "action": "aoa_sequence",
                "aoa_commands": ["click 180 320"], "aoa_touch_mapping": self.mapping}
        assert self.client.post("/api/cluster/devices/actions", json=body).status_code == 422
        body["aoa_hid_touch_mode"] = "direct"
        body["aoa_touch_mapping"] = {**self.mapping, "padding": "x" * 65536}
        assert self.client.post("/api/cluster/devices/actions", json=body).status_code == 422

    def test_recover_ignores_client_context_and_uses_selected_worker_inventory(self):
        with patch("features.cluster.device_actions_api.threading.Thread"):
            response = self.client.post("/api/devices/aoa/recover", json={
                "worker_id": "worker-246", "device": "ABC", "commands": ["click 180 320"],
                "device_context": {"model": "spoofed", "fingerprint": "wrong"},
                "metadata": {"hid_touch_mode": "direct", "calibration": {"mapping": self.mapping}}})
        assert response.status_code == 200, response.text
        payload = self.repo.get_command(response.json()["command_id"])["payload"]
        assert payload["aoa_touch_mapping"]["context"] == self.mapping["context"]

    def test_source_device_context_cannot_impersonate_a_different_target(self):
        context = {**self.mapping["context"], "fingerprint": "different-target-build"}
        self.repo.heartbeat("worker-246", {"devices": [{"serial": "ABC", "state": "available",
            "transport": "local_usb", "properties": {"usb": "1-2", "aoa_context": context}}]})
        response = self.client.post("/api/devices/aoa/recover", json={
            "worker_id": "worker-246", "device": "ABC", "commands": ["click 180 320"],
            "device_context": self.mapping["context"],
            "metadata": {"hid_touch_mode": "direct", "calibration": {"mapping": self.mapping}}})
        assert response.status_code == 409
        assert not self.repo.claims.list_active()

    def test_legacy_worker_cannot_silently_drop_mapping(self):
        worker = self.repo.get_worker("worker-246")
        worker["capabilities"]["aoa_affine_mapping_v2"] = False
        with patch.object(self.repo, "get_worker", return_value=worker):
            response = self.client.post("/api/cluster/devices/actions", json={
                "worker_id": "worker-246", "devices": ["ABC"], "action": "aoa_sequence",
                "aoa_commands": ["click 180 320"], "aoa_hid_touch_mode": "direct",
                "aoa_touch_mapping": self.mapping})
        assert response.status_code == 409, response.text
