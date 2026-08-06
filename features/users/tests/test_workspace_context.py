import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.users import workspace_context


class WorkspaceLocalWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_root = workspace_context.runtime.data_root
        workspace_context.runtime.data_root = Path(self.temp.name)

    def tearDown(self):
        workspace_context.runtime.data_root = self.previous_root
        self.temp.cleanup()

    def test_single_scope_uses_configured_local_worker_id(self):
        with patch.object(
            workspace_context, "_local_worker_id", return_value="controller-main"
        ):
            context = workspace_context.load_workspace_context("alice")
            saved = workspace_context.save_workspace_context(
                "alice",
                workspace_context.WorkspaceContextPatch(
                    scope_mode="single",
                    worker_id="remote-worker",
                    device_ids=["LOCAL", "remote-worker:ABC"],
                ),
            )

        self.assertEqual(context["worker_id"], "controller-main")
        self.assertEqual(saved["worker_id"], "controller-main")
        self.assertEqual(saved["device_ids"], ["LOCAL"])

    def test_cluster_scope_preserves_explicit_worker_id(self):
        path = workspace_context._context_path("alice")
        path.write_text(
            json.dumps({"scope_mode": "cluster", "worker_id": "ats-worker-controller"}),
            encoding="utf-8",
        )

        with patch.object(
            workspace_context, "_local_worker_id", return_value="controller-main"
        ):
            context = workspace_context.load_workspace_context("alice")

        self.assertEqual(context["worker_id"], "ats-worker-controller")


if __name__ == "__main__":
    unittest.main()
