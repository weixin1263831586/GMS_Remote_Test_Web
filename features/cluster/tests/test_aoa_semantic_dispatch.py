"""Real recover -> model -> ownership -> claim -> durable semantic payload."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from features.cluster import _port_device_action_request
from features.devices import AoaCertificationStore, aoa_api
from foundation import cluster_port
from tests.aoa_semantic_fixtures import reference, semantic_template

from .api_hardening_fixture import ClusterApiHardeningFixture


class AoaSemanticDispatchTests(ClusterApiHardeningFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.context = reference()["context"]
        self.store = AoaCertificationStore(Path(self.temp.name) / "certs")
        commands = {}
        for serial in ("REF-A", "REF-B"):
            commands[serial] = {"status": "completed", "command_type": "device_action", "worker_id": "worker-246",
                "payload": {"owner_id": "admin-id", "devices": [serial], "usb_port_path": "1-2",
                    "lease_tokens": {serial: "fence"}, "action": "aoa_record_input", "aoa_record": {"action": "verify_direct"}},
                "result": {"success": True, "direct_reference_evidence": reference(serial)}}
        self.proof = self.store.issue("admin-id", "Synthetic class", ["REF-A", "REF-B"],
                                     SimpleNamespace(get_command=commands.get))
        worker = self.repo.get_worker("worker-246")
        self.repo.register_worker({**worker, "worker_id": "worker-246", "capabilities": {
            **worker["capabilities"], "aoa_semantic_v1": True, "aoa_per_command_v1": True}})
        self.repo.heartbeat("worker-246", {"devices": [{"serial": "ABC", "state": "available", "transport": "local_usb",
            "properties": {"usb": "1-2", "aoa_context": self.context}}]})
        self.client.app.include_router(aoa_api.router)
        for target, value in [("features.devices.aoa_semantic_service.certification_store", lambda: self.store),
                              ("features.devices.aoa_advanced_api.certification_store", lambda: self.store)]:
            mock = patch(target, value)
            mock.start()
            self.addCleanup(mock.stop)
        port = patch.object(cluster_port, "_device_action_request", _port_device_action_request)
        port.start()
        self.addCleanup(port.stop)

    def request_body(self):
        return {"worker_id": "worker-246", "device": "ABC", "commands": ["click 1 1"],
                "metadata": {"hid_touch_mode": "direct", "semantic_template": semantic_template(),
                             "class_certificate_ids": [self.proof["id"]]}}

    def test_reviewed_semantic_plan_survives_real_dispatch(self):
        with patch("features.cluster.device_actions_api.threading.Thread"):
            response = self.client.post("/api/devices/aoa/recover", json=self.request_body())
        assert response.status_code == 200, response.text
        payload = self.repo.get_command(response.json()["command_id"])["payload"]
        assert payload["aoa_mapping_policy"] == "direct_standard"
        assert payload["aoa_semantic_plan"]["commands"] == ["click 180 481"]
        assert payload["aoa_direct_certificate"]["id"] == self.proof["id"]
        assert payload["lease_tokens"]
        assert payload["usb_port_path"] == "1-2"

    def test_foreign_and_revoked_certificates_do_not_claim_devices(self):
        response = self.client.post("/api/devices/aoa/recover", json=self.request_body(), headers={"X-Test-User": "other"})
        assert response.status_code == 404, response.text
        self.store.revoke(self.proof["id"], "admin-id")
        response = self.client.post("/api/devices/aoa/recover", json=self.request_body())
        assert response.status_code == 404, response.text
        assert not self.repo.claims.list_active()

    def test_legacy_worker_is_rejected_before_device_claim(self):
        worker = self.repo.get_worker("worker-246")
        worker["capabilities"]["aoa_semantic_v1"] = False
        with patch.object(self.repo, "get_worker", return_value=worker):
            response = self.client.post("/api/devices/aoa/recover", json=self.request_body())
        assert response.status_code == 409, response.text
        assert not self.repo.claims.list_active()

    def test_reference_endpoint_uses_existing_fenced_action_channel(self):
        worker = self.repo.get_worker("worker-246")
        worker["capabilities"]["aoa_record_input_v1"] = True
        with patch.object(self.repo, "get_worker", return_value=worker), patch("features.cluster.device_actions_api.threading.Thread"):
            response = self.client.post("/api/devices/aoa/certifications/verify", json={"worker_id": "worker-246", "device": "ABC"})
        assert response.status_code == 200, response.text
        payload = self.repo.get_command(response.json()["command_id"])["payload"]
        assert payload["aoa_record"]["action"] == "verify_direct"
        assert payload["lease_tokens"] and payload["usb_port_path"] == "1-2"

    def test_local_reference_verification_keeps_a_durable_receipt(self):
        with patch("features.cluster.device_actions_api.service") as service, patch("features.cluster.device_actions_api.threading.Thread"):
            service.return_value.repository = self.repo
            service.return_value.config.local_worker_id = "worker-246"
            response = self.client.post("/api/devices/aoa/certifications/verify", json={"worker_id": "worker-246", "device": "ABC"})
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] is True
        command = self.repo.get_command(response.json()["command_id"])
        assert command["payload"]["usb_port_path"] == "1-2"
        assert command["status"] == "running"

    def test_reference_invalid_display_is_rejected_without_claim(self):
        response = self.client.post("/api/devices/aoa/certifications/verify", json={
            "worker_id": "worker-246", "device": "ABC", "display_id": "wrong"})
        assert response.status_code == 422, response.text
        assert not self.repo.claims.list_active()
