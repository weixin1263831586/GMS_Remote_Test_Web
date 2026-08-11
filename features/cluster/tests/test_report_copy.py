from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.cluster import api as cluster_api
from features.cluster.config import ClusterConfig
from features.cluster.repository import ClusterRepository, utc_now
from features.cluster.service import ClusterService
from worker_agent.config import WorkerConfig


REPORT_NAME = "2026.08.07_15.56.09.558_3101"


class ReportCopyApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = ClusterRepository(self.root / "cluster.sqlite3")
        self.local_worker = "ats-worker-controller"
        self.source_worker = "worker-246"
        self.target_worker = "worker-247"
        self.local_tools = self.root / "local-suites" / "android-cts" / "tools"
        self.local_tools.mkdir(parents=True)
        self.source_tools = "/home/wlq/GMS-Suite/android-cts-17_r1/android-cts/tools"
        self.target_tools = "/home/target/GMS-Suite/android-cts-17_r1/android-cts/tools"
        for worker_id, agent_version in (
            (self.local_worker, "controller-0.1.0"),
            (self.source_worker, "1"),
            (self.target_worker, "1"),
        ):
            self.repo.register_worker({
                "worker_id": worker_id,
                "name": worker_id,
                "hostname": worker_id,
                "address": "127.0.0.1",
                "agent_version": agent_version,
                "max_jobs": 1,
                "capabilities": {},
            })
        for worker_id, tools_path in (
            (self.local_worker, str(self.local_tools)),
            (self.source_worker, self.source_tools),
            (self.target_worker, self.target_tools),
        ):
            self.repo.heartbeat(worker_id, {
                "agent_version": "controller-0.1.0" if worker_id == self.local_worker else "1",
                "running_jobs": [],
                "devices": [],
                "suites": [{
                    "suite_type": "CTS",
                    "suite_version": "17_r1",
                    "suite_key": "CTS:17_r1",
                    "tools_path": tools_path,
                    "available": True,
                }],
            })

        self.previous_service = cluster_api.cluster_service
        cluster_api.cluster_service = ClusterService(
            self.repo,
            config=ClusterConfig(
                enabled=True,
                remote_dispatch_enabled=True,
                local_worker_id=self.local_worker,
            ),
        )
        app = FastAPI()

        @app.middleware("http")
        async def user_identity(request: Request, call_next):
            request.state.current_user = CurrentUser(
                id="owner-id", username="owner", role="user"
            )
            return await call_next(request)

        app.include_router(cluster_api.router)
        self.client = TestClient(app)
        token_file = self.root / "cluster-tokens.json"
        token_file.write_text(json.dumps({
            "worker_tokens": {
                self.source_worker: "source-token",
                self.target_worker: "target-token",
            }
        }), encoding="utf-8")
        self.token_env = patch.dict(
            "os.environ", {"GMS_WORKER_TOKENS_FILE": str(token_file)}
        )
        self.token_env.start()

    def tearDown(self):
        self.token_env.stop()
        self.client.close()
        cluster_api.cluster_service = self.previous_service
        self.temp.cleanup()

    def _create_copy(self, target_worker: str, target_suite_path: str) -> dict:
        response = self.client.post("/api/cluster/suites/report-copies", json={
            "source_worker_id": self.source_worker,
            "source_suite_path": self.source_tools,
            "report_name": REPORT_NAME,
            "target_worker_id": target_worker,
            "target_suite_path": target_suite_path,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _complete_copy_transfer(self, transfer_id: str) -> Path:
        directory = self.repo.db_path.parent / "transfers" / transfer_id
        directory.mkdir(parents=True)
        archive = directory / "report.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(f"{REPORT_NAME}/test_result.xml", "result")
        data = archive.read_bytes()
        self.repo.update_transfer(
            transfer_id,
            status="completed",
            filename=archive.name,
            relative_path=str(archive.relative_to(self.repo.db_path.parent / "transfers")),
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            completed_at=utc_now(),
        )
        return archive

    def test_remote_report_can_be_imported_into_controller_suite(self):
        created = self._create_copy(self.local_worker, str(self.local_tools))
        transfer_id = created["copy_id"]
        self.assertEqual(created["export_status"], "created")
        command = self.repo.get_command(created["export_command_id"])
        self.assertEqual(command["command_type"], "suite_export")
        self.assertEqual(command["payload"]["path"], f"results/{REPORT_NAME}")
        self._complete_copy_transfer(transfer_id)

        def local_config(data_root: Path) -> WorkerConfig:
            return WorkerConfig(
                worker_id=self.local_worker,
                controller_url="https://controller",
                token="unused",
                suite_roots=[self.local_tools.parents[1]],
                data_root=data_root,
            )

        with patch(
            "features.cluster.transfers_api._local_worker_config",
            side_effect=local_config,
        ):
            response = self.client.post(
                f"/api/cluster/suites/report-copies/{transfer_id}/import"
            )

        self.assertEqual(response.status_code, 200, response.text)
        destination = self.local_tools.parent / "results" / REPORT_NAME
        self.assertEqual(response.json()["result"]["destination"], str(destination))
        self.assertEqual(
            (destination / "test_result.xml").read_text(encoding="utf-8"),
            "result",
        )

    def test_controller_report_is_packaged_for_remote_target(self):
        source = self.local_tools.parent / "results" / REPORT_NAME
        source.mkdir(parents=True)
        (source / "test_result.xml").write_text("result", encoding="utf-8")

        def local_config(data_root: Path) -> WorkerConfig:
            return WorkerConfig(
                worker_id=self.local_worker,
                controller_url="https://controller",
                token="unused",
                suite_roots=[self.local_tools.parents[1]],
                data_root=data_root,
            )

        with patch(
            "features.cluster.transfers_api._local_worker_config",
            side_effect=local_config,
        ):
            response = self.client.post("/api/cluster/suites/report-copies", json={
                "source_worker_id": self.local_worker,
                "source_suite_path": str(self.local_tools),
                "report_name": REPORT_NAME,
                "target_worker_id": self.target_worker,
                "target_suite_path": self.target_tools,
            })

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["export_status"], "completed")
        transfer = self.repo.get_transfer(payload["copy_id"])
        archive = self.repo.db_path.parent / "transfers" / transfer["relative_path"]
        self.assertTrue(archive.is_file())
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(
                bundle.read(f"{REPORT_NAME}/test_result.xml"),
                b"result",
            )

    def test_remote_target_is_locked_and_receives_import_command(self):
        created = self._create_copy(self.target_worker, self.target_tools)
        transfer_id = created["copy_id"]
        archive = self._complete_copy_transfer(transfer_id)

        response = self.client.post(
            f"/api/cluster/suites/report-copies/{transfer_id}/import"
        )

        self.assertEqual(response.status_code, 200, response.text)
        command = self.repo.get_command(response.json()["command_id"])
        self.assertEqual(command["command_type"], "report_import")
        self.assertEqual(command["payload"]["target_suite_path"], self.target_tools)
        denied = self.client.get(
            f"/api/cluster/workers/{self.source_worker}/report-copies/{transfer_id}",
            headers={"Authorization": "Bearer source-token"},
        )
        allowed = self.client.get(
            f"/api/cluster/workers/{self.target_worker}/report-copies/{transfer_id}",
            headers={"Authorization": "Bearer target-token"},
        )
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.content, archive.read_bytes())

    def test_report_copy_rejects_same_worker_and_unknown_target_suite(self):
        same_worker = self.client.post("/api/cluster/suites/report-copies", json={
            "source_worker_id": self.source_worker,
            "source_suite_path": self.source_tools,
            "report_name": REPORT_NAME,
            "target_worker_id": self.source_worker,
            "target_suite_path": self.source_tools,
        })
        unknown_suite = self.client.post("/api/cluster/suites/report-copies", json={
            "source_worker_id": self.source_worker,
            "source_suite_path": self.source_tools,
            "report_name": REPORT_NAME,
            "target_worker_id": self.target_worker,
            "target_suite_path": "/unregistered/tools",
        })

        self.assertEqual(same_worker.status_code, 400)
        self.assertEqual(unknown_suite.status_code, 409)


if __name__ == "__main__":
    unittest.main()
