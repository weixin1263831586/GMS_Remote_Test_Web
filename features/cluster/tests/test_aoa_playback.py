"""Controls are scoped to one owner, Worker and persisted playback."""
import asyncio
import unittest
from dataclasses import replace

from features.cluster import api as cluster_api
from features.cluster import control_aoa_playback
from foundation.error_model import ApiError

from .api_hardening_fixture import ClusterApiHardeningFixture


class PlaybackControlTests(ClusterApiHardeningFixture, unittest.TestCase):
    def playback(self):
        return self.repo.create_command({"worker_id": "worker-246", "command_type": "device_action",
            "initial_status": "running", "payload": {"owner_id": "admin-id", "action": "aoa_sequence",
                "playback_control_id": "pb-owned", "devices": ["worker-246:ABC"]}})

    def test_foreign_owner_or_worker_cannot_control_playback(self):
        self.playback()
        for worker, owner in [("worker-246", "bob-id"), ("another-worker", "admin-id")]:
            with self.assertRaises(ApiError) as error:
                asyncio.run(control_aoa_playback("pb-owned", worker, owner, "pause"))
            self.assertEqual(error.exception.to_response().status_code, 404)

    def test_remote_pause_and_resume_are_ordered_and_target_original_command(self):
        original = self.playback()
        for revision, action in enumerate(["pause", "resume"], 1):
            accepted = asyncio.run(control_aoa_playback("pb-owned", "worker-246", "admin-id", action))
            self.assertEqual(accepted["control_revision"], revision)
            control = self.repo.get_command_by_operation("worker-246", f"{original['id']}:control:{revision}")
            self.assertEqual(control["payload"]["target_command_id"], original["id"])
            self.assertEqual(control["payload"]["revision"], revision)
        self.assertEqual(self.repo.get_command(original["id"])["status"], "running")

    def test_local_control_is_persistent_without_creating_a_second_execution(self):
        original = self.playback()
        cluster_api.cluster_service.config = replace(cluster_api.cluster_service.config, local_worker_id="worker-246")
        asyncio.run(control_aoa_playback("pb-owned", "worker-246", "admin-id", "pause"))
        saved = self.repo.get_command(original["id"])["payload"]
        self.assertEqual(saved["aoa_control_action"], "pause")
        self.assertEqual(saved["aoa_control_revision"], 1)
        self.assertIsNone(self.repo.get_command_by_operation("worker-246", f"{original['id']}:control:1"))

    def test_finished_command_cannot_be_resumed_and_handle_cannot_be_reused(self):
        original = self.playback()
        self.repo.ack_command("worker-246", original["id"], {"status": "completed", "result": {"success": True}})
        with self.assertRaises(ApiError):
            asyncio.run(control_aoa_playback("pb-owned", "worker-246", "admin-id", "resume"))
        with self.assertRaises(ApiError):
            self.playback()

    def test_cancel_remains_cancel_when_a_late_resume_arrives(self):
        original = self.playback()
        asyncio.run(control_aoa_playback("pb-owned", "worker-246", "admin-id", "cancel"))
        result = asyncio.run(control_aoa_playback("pb-owned", "worker-246", "admin-id", "resume"))
        self.assertEqual(result["action"], "cancel")
        latest = self.repo.get_command_by_operation("worker-246", f"{original['id']}:control:2")
        self.assertEqual(latest["payload"]["action"], "cancel")

    def test_multiple_events_at_same_step_are_not_deduplicated(self):
        from features.cluster.device_action_operations import _local_aoa_progress_reporter

        original = self.playback()
        _, report = _local_aoa_progress_reporter(self.repo, original)
        phases = ["paused", "resumed", "running", "completed"]
        for phase in phases:
            report({"phase": phase, "index": 17, "total": 100, "command": "click 180 522"})
        events = self.repo.list_command_events(original["id"])
        self.assertEqual([event["sequence"] for event in events], [1, 2, 3, 4])
        self.assertEqual([event["payload"]["phase"] for event in events], phases)
