"""Queue ownership, idempotency and lifecycle of continuous single-step debugging."""
import unittest
from dataclasses import replace
from unittest.mock import patch

from features.cluster import api as cluster_api
from features.cluster.aoa_debug import debug_command_source
from features.cluster.device_action_operations import execute_local_operation
from foundation.error_model import ApiError

from .api_hardening_fixture import ClusterApiHardeningFixture


class AoaDebugTests(ClusterApiHardeningFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        cluster_api.cluster_service.config = replace(cluster_api.cluster_service.config, local_worker_id="worker-246")
        self.body = {"worker_id": "worker-246", "devices": ["ABC"], "action": "aoa_sequence",
            "aoa_commands": ["click 180 525", "back"], "playback_control_id": "dbg-test",
            "aoa_debug_session_id": "dbg-test", "aoa_debug_request_id": "first", "aoa_debug_command": "click 180 525"}
        # Exercise admission/receipt persistence without sending physical inputs.
        self.thread_patch = patch("features.cluster.device_actions_api.threading.Thread")
        self.thread = self.thread_patch.start()
        self.addCleanup(self.thread_patch.stop)

    def start(self):
        response = self.client.post("/api/cluster/devices/actions", json=self.body)
        assert response.status_code == 200, response.text
        return response.json()["command_id"]

    def test_subsequent_commands_reuse_one_receipt_claim_and_full_hid_set(self):
        command_id = self.start()
        claim = self.repo.claims.active_claim("worker-246:ABC")
        second = {**self.body, "aoa_debug_request_id": "second", "aoa_debug_command": "back"}
        response = self.client.post("/api/cluster/devices/actions", json=second)
        assert response.status_code == 200, response.text
        assert response.json()["command_id"] == command_id and response.json()["debug_sequence"] == 2
        assert self.repo.claims.active_claim("worker-246:ABC")["id"] == claim["id"]
        duplicate = self.client.post("/api/cluster/devices/actions", json=second)
        assert duplicate.json()["debug_sequence"] == 2
        assert self.thread.call_count == 1
        payload = self.repo.get_command(command_id)["payload"]
        assert len(payload["aoa_debug_queue"]) == 2
        assert payload["aoa_commands"] == self.body["aoa_commands"]

    def test_foreign_owner_changed_target_channel_or_bound_request_rejected(self):
        self.start()
        for updates, headers, expected in [
            ({"aoa_debug_request_id": "other"}, {"X-Test-User": "bob"}, 404),
            ({"aoa_debug_request_id": "other", "aoa_debug_command": "write abc"}, {}, 409),
            ({"aoa_debug_command": "back"}, {}, 409),
            ({"aoa_hid_touch_mode": "direct"}, {}, 409),
        ]:
            response = self.client.post("/api/cluster/devices/actions", json={**self.body, **updates}, headers=headers)
            assert response.status_code == expected, response.text

    def test_idle_close_and_cancel_reject_new_inputs(self):
        command_id = self.start()
        source = debug_command_source(self.repo, command_id)
        assert source()["sequence"] == 1
        assert source(close=True) is None
        response = self.client.post("/api/cluster/devices/actions", json={**self.body, "aoa_debug_request_id": "second"})
        assert response.status_code == 409
        assert len(self.repo.get_command(command_id)["payload"]["aoa_debug_queue"]) == 1

    def test_closing_poll_consumes_an_input_accepted_before_the_close_transaction(self):
        command_id = self.start()
        source = debug_command_source(self.repo, command_id)
        assert source()["sequence"] == 1
        self.repo.enqueue_aoa_debug_command(command_id, "admin-id", "second", "back")
        assert source(close=True)["sequence"] == 2
        assert not self.repo.get_command(command_id)["payload"].get("aoa_debug_closed")
        assert source(close=True) is None

    def test_claim_loss_fails_closed_and_remote_worker_is_explicitly_rejected(self):
        command_id = self.start()
        self.repo.claims.release(self.repo.get_command(command_id)["payload"]["claim_source_id"])
        response = self.client.post("/api/cluster/devices/actions", json={**self.body, "aoa_debug_request_id": "second"})
        assert response.status_code == 409
        cluster_api.cluster_service.config = replace(cluster_api.cluster_service.config, local_worker_id="other")
        response = self.client.post("/api/cluster/devices/actions", json={**self.body, "aoa_debug_session_id": "remote"})
        assert response.status_code == 409

    def test_cancel_rejects_new_input_immediately(self):
        command_id = self.start()
        self.repo.request_aoa_playback_control("worker-246", "dbg-test", "admin-id", "cancel")
        with self.assertRaises(ApiError):
            self.repo.enqueue_aoa_debug_command(command_id, "admin-id", "second", "back")

    def test_executor_reads_durable_queue_and_only_releases_claim_after_cleanup(self):
        command_id = self.start()
        self.repo.enqueue_aoa_debug_command(command_id, "admin-id", "second", "back")
        command = self.repo.get_command(command_id)

        def execute(action, devices, payload, **callbacks):
            assert self.repo.claims.active_claim("worker-246:ABC")
            source = payload["_aoa_debug_source"]
            assert source()["command"] == "click 180 525"
            assert source()["command"] == "back"
            assert source(close=True) is None
            payload["_validate_fencing"]()
            callbacks["progress_event_callback"]({"phase": "completed", "debug_sequence": 2,
                "index": 2, "total": 2, "command": "back"})
            return {"success": True, "commands_executed": 2, "native_mode_restored": True}

        execute_local_operation(self.repo, command, execute)
        assert self.repo.get_command(command_id)["status"] == "completed"
        assert self.repo.claims.active_claim("worker-246:ABC") is None
        events = self.repo.list_command_events(command_id)
        assert events[-1]["payload"]["debug_sequence"] == 2

    def test_cancelled_session_keeps_cleanup_diagnostics_in_its_receipt(self):
        command_id = self.start()

        def execute(*args, **kwargs):
            error = RuntimeError("debug cancelled")
            error.cancelled = True
            error.cleanup_errors = [{"step": "reset_usb", "error": "disconnected"}]
            raise error

        execute_local_operation(self.repo, self.repo.get_command(command_id), execute)
        command = self.repo.get_command(command_id)
        assert command["status"] == "cancelled"
        assert command["result"]["reconcile_required"] is True
        assert command["result"]["cleanup_errors"][0]["step"] == "reset_usb"
        assert self.repo.claims.active_claim("worker-246:ABC") is None
