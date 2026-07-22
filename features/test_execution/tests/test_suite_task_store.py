from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from features.test_execution.suite_task_store import SuiteTaskStore


class SuiteTaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "suite_tasks.sqlite3"
        self.store = SuiteTaskStore(self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def task(self, *, task_id: str = "task-1", owner: str = "owner-1") -> dict:
        return {
            "task_id": task_id,
            "owner_id": owner,
            "kind": "download",
            "status": "queued",
            "archive_path": "/srv/suites/cts.zip",
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    def test_tasks_survive_restart_and_are_owner_scoped(self) -> None:
        self.store.create(self.task())
        restarted = SuiteTaskStore(self.path)
        self.assertEqual(restarted.get("task-1", "owner-1")["status"], "queued")
        self.assertIsNone(restarted.get("task-1", "owner-2"))
        self.assertEqual(restarted.list_active()[0]["task_id"], "task-1")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_recovers_after_runtime_data_directory_deletion(self) -> None:
        shutil.rmtree(self.temp.name)

        self.assertEqual(self.store.list_active(), [])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_updates_and_expiry_are_durable(self) -> None:
        self.store.create(self.task())
        self.store.update("task-1", status="completed", progress=100)
        completed = self.store.get("task-1", "owner-1")
        self.assertEqual(completed["progress"], 100)
        self.assertEqual(
            self.store.delete_finished_before(completed["updated_at"] + 1),
            1,
        )
        self.assertIsNone(self.store.get("task-1", "owner-1"))

    def test_active_archive_lookup_does_not_disclose_owner_in_api_layer(self) -> None:
        self.store.create(self.task())
        found = self.store.find_active_download("/srv/suites/cts.zip")
        self.assertEqual(found["owner_id"], "owner-1")
        self.assertIsNone(
            self.store.find_active_download("/srv/suites/another.zip")
        )


if __name__ == "__main__":
    unittest.main()
