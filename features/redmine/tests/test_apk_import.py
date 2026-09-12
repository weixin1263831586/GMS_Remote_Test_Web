"""APK 附件导入与源码正文搜索/读取测试（离线）。"""

from __future__ import annotations

import threading
import unittest
import uuid
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import CurrentUser


def _tiny_apk(path: Path) -> None:
    """构造最小合法 ZIP/APK（PK 魔数 + AndroidManifest 占位）。"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("AndroidManifest.xml", "<manifest/>")
        archive.writestr("classes.dex", b"dex\n035\0" + b"\x00" * 32)


def _configure_firmware_runtime(upload_dir: str):
    from features.firmware import runtime as firmware_runtime

    firmware_runtime.configure_runtime(
        global_state=SimpleNamespace(
            apk_analysis_tasks={},
            apk_analysis_tasks_lock=threading.RLock(),
            apk_upload_locks={},
            apk_upload_locks_lock=threading.RLock(),
            background_tasks=set(),
        ),
        apk_max_tasks=20,
        apk_max_file_size=500 * 1024 * 1024,
        apk_max_source_file_size=2 * 1024 * 1024,
        apk_upload_dir=upload_dir,
        jadx_path="/nonexistent/jadx",
        jadx_timeout=5,
    )
    return firmware_runtime


class ApkImportApiTests(unittest.TestCase):
    def setUp(self):
        self.secret_env = patch.dict(
            "os.environ", {"GMS_SECRET_KEY": Fernet.generate_key().decode("ascii")}
        )
        self.secret_env.start()
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.firmware_runtime = _configure_firmware_runtime(str(self.root / "apk"))

        import features.redmine.evidence_store as evidence_store
        import features.redmine.users as redmine_users

        self.users_patch = patch.object(
            redmine_users,
            "owner_redmine_root",
            lambda owner: self.root / "owners" / str(owner),
        )
        self.users_patch.start()
        evidence_store._STORE_CACHE.clear()

        # 构造一个 ready 的 APK artifact。
        from features.redmine.evidence_store import owner_evidence_store

        store = owner_evidence_store("owner-a")
        snapshot = store.create_snapshot(issue_id=648526, download_policy="all")
        store.update_snapshot(
            snapshot["snapshot_id"],
            status="ready",
            content_sha256="a" * 64,
            manifest_json={"issue_id": 648526, "journals": [], "attachments": []},
            journal_count=0,
            attachment_count=1,
            downloaded_count=1,
            error_json=[],
        )
        artifact = store.create_artifact(
            snapshot_id=snapshot["snapshot_id"],
            attachment_id="888001",
            filename="test_app.apk",
            original_filename="test_app.apk",
            content_type="application/vnd.android.package-archive",
            kind="apk",
            declared_size=0,
        )
        rel = f"648526/{snapshot['snapshot_id']}/attachments/{artifact['artifact_id']}/test_app.apk"
        target = store.resolve_internal(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        _tiny_apk(target)
        import hashlib

        store.update_artifact(
            artifact["artifact_id"],
            status="ready",
            size_bytes=target.stat().st_size,
            sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
            stored_path=rel,
        )
        self.artifact_id = artifact["artifact_id"]
        self.snapshot_id = snapshot["snapshot_id"]

        from features.redmine import apk_import_api

        app = FastAPI()

        @app.middleware("http")
        async def authenticate(request, call_next):
            owner = request.headers.get("x-test-owner", "owner-a")
            role = request.headers.get("x-test-role", "user")
            request.state.current_user = CurrentUser(
                id=owner, username=owner, role=role
            )
            request.state.auth_method = "session"
            return await call_next(request)

        app.include_router(apk_import_api.router)
        app.include_router(apk_import_api.apk_router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.users_patch.stop()
        import features.redmine.evidence_store as evidence_store

        evidence_store._STORE_CACHE.clear()
        self.directory.cleanup()
        self.secret_env.stop()

    def _complete_task(self, task_id: str) -> None:
        """把任务直接置为 completed 并伪造 sources 目录，供读取测试使用。"""
        runtime = self.firmware_runtime
        task_dir = Path(runtime.apk_upload_dir) / task_id
        sources = task_dir / "jadx_output" / "sources" / "com" / "example"
        sources.mkdir(parents=True, exist_ok=True)
        (sources / "Foo.java").write_text(
            "package com.example;\npublic class Foo {\n  void bar() { throw new AssertionError(\"boom\"); }\n}\n",
            encoding="utf-8",
        )
        with runtime.global_state.apk_analysis_tasks_lock:
            task = runtime.global_state.apk_analysis_tasks[task_id]
            task.update(
                {
                    "status": "completed",
                    "progress": 100,
                    "output_dir": str(task_dir / "jadx_output"),
                }
            )

    # ------------------------------------------------------------------ tests

    def test_import_creates_task_with_source_ref(self):
        response = self.client.post(
            f"/api/redmine-agent/artifacts/{self.artifact_id}/apk-analysis",
            headers={"x-test-owner": "owner-a"},
        )
        self.assertEqual(response.status_code, 202)
        data = response.json()["data"]
        self.assertEqual(data["status"], "analyzing")
        self.assertEqual(data["source_ref"]["issue_id"], 648526)
        self.assertEqual(data["source_ref"]["snapshot_id"], self.snapshot_id)
        task = self.firmware_runtime.global_state.apk_analysis_tasks[data["task_id"]]
        self.assertEqual(task["source_ref"]["sha256"], task["source_ref"]["sha256"])
        # 伪造完成，清理后台任务
        self._complete_task(data["task_id"])

    def test_import_rejects_cross_owner(self):
        response = self.client.post(
            f"/api/redmine-agent/artifacts/{self.artifact_id}/apk-analysis",
            headers={"x-test-owner": "owner-b"},
        )
        self.assertEqual(response.status_code, 404)

    def test_source_search_and_read(self):
        # 直接构造 completed 任务
        task_id = str(uuid.uuid4())
        from features.firmware import create_apk_task

        apk_dir = Path(self.firmware_runtime.apk_upload_dir) / task_id
        apk_dir.mkdir(parents=True, exist_ok=True)
        apk_file = apk_dir / "test_app.apk"
        _tiny_apk(apk_file)
        create_apk_task(task_id, str(apk_file), "test_app.apk", "owner-a")
        self._complete_task(task_id)

        search = self.client.get(
            f"/api/apk/source-search/{task_id}",
            params={"q": "AssertionError"},
            headers={"x-test-owner": "owner-a"},
        )
        self.assertEqual(search.status_code, 200)
        matches = search.json()["data"]["matches"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["path"], "com/example/Foo.java")
        self.assertEqual(matches[0]["line"], 3)

        read = self.client.get(
            f"/api/apk/source-read/{task_id}",
            params={"path": "com/example/Foo.java", "offset": 0, "limit": 2},
            headers={"x-test-owner": "owner-a"},
        )
        self.assertEqual(read.status_code, 200)
        data = read.json()["data"]
        self.assertEqual(data["total_lines"], 4)
        self.assertEqual(data["returned_lines"], 2)
        self.assertTrue(data["truncated"])
        self.assertEqual(data["lines"][0]["line"], 1)

    def test_source_read_rejects_path_escape(self):
        task_id = str(uuid.uuid4())
        from features.firmware import create_apk_task

        apk_dir = Path(self.firmware_runtime.apk_upload_dir) / task_id
        apk_dir.mkdir(parents=True, exist_ok=True)
        apk_file = apk_dir / "test_app.apk"
        _tiny_apk(apk_file)
        create_apk_task(task_id, str(apk_file), "test_app.apk", "owner-a")
        self._complete_task(task_id)

        read = self.client.get(
            f"/api/apk/source-read/{task_id}",
            params={"path": "../../etc/passwd"},
            headers={"x-test-owner": "owner-a"},
        )
        self.assertEqual(read.status_code, 422)

    def test_source_read_rejects_cross_owner(self):
        task_id = str(uuid.uuid4())
        from features.firmware import create_apk_task

        apk_dir = Path(self.firmware_runtime.apk_upload_dir) / task_id
        apk_dir.mkdir(parents=True, exist_ok=True)
        apk_file = apk_dir / "test_app.apk"
        _tiny_apk(apk_file)
        create_apk_task(task_id, str(apk_file), "test_app.apk", "owner-a")
        self._complete_task(task_id)

        read = self.client.get(
            f"/api/apk/source-read/{task_id}",
            params={"path": "com/example/Foo.java"},
            headers={"x-test-owner": "owner-b"},
        )
        self.assertEqual(read.status_code, 404)


    def _corrupt_artifact(self, writer) -> str:
        """把 artifact 文件替换为指定损坏内容，返回 artifact_id。"""
        from features.redmine.evidence_store import owner_evidence_store

        store = owner_evidence_store("owner-a")
        import hashlib

        with store._connect() as conn:
            row = conn.execute(
                "SELECT stored_path FROM redmine_evidence_artifacts WHERE artifact_id = ?",
                (self.artifact_id,),
            ).fetchone()
        rel = str(row["stored_path"])
        target = store.resolve_internal(rel)
        writer(target)
        store.update_artifact(
            self.artifact_id,
            size_bytes=target.stat().st_size,
            sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        )
        return self.artifact_id

    def test_import_rejects_plain_zip_without_manifest(self):
        """P1 回归：改名为 .apk 的普通 ZIP 必须被 422 拒绝。"""
        def _write(target):
            with zipfile.ZipFile(target, "w") as archive:
                archive.writestr("readme.txt", "not an apk")

        artifact_id = self._corrupt_artifact(_write)
        response = self.client.post(
            f"/api/redmine-agent/artifacts/{artifact_id}/apk-analysis",
            headers={"x-test-owner": "owner-a"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("AndroidManifest", response.json()["error"])

    def test_import_rejects_zip_without_dex(self):
        """P1 回归：有 manifest 但缺 DEX 的 ZIP 必须被 422 拒绝。"""
        def _write(target):
            with zipfile.ZipFile(target, "w") as archive:
                archive.writestr("AndroidManifest.xml", "<manifest/>")

        artifact_id = self._corrupt_artifact(_write)
        response = self.client.post(
            f"/api/redmine-agent/artifacts/{artifact_id}/apk-analysis",
            headers={"x-test-owner": "owner-a"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("DEX", response.json()["error"])

    def test_import_rejects_corrupt_zip(self):
        """P1 回归：PK 魔数后损坏的 ZIP（testzip 失败）必须被 422 拒绝。"""
        def _write(target):
            # 先写一个合法 ZIP，再破坏其中部字节，使其仍以 PK 开头但解析失败。
            with zipfile.ZipFile(target, "w") as archive:
                archive.writestr("AndroidManifest.xml", "<manifest/>")
                archive.writestr("classes.dex", b"dex\n035\0" + b"\x00" * 32)
            data = bytearray(target.read_bytes())
            # 中央目录位于尾部；破坏倒数第 60 字节附近的数据区不影响魔数。
            for offset in range(len(data) - 200, len(data) - 100):
                data[offset] = 0xFF
            target.write_bytes(bytes(data))

        artifact_id = self._corrupt_artifact(_write)
        response = self.client.post(
            f"/api/redmine-agent/artifacts/{artifact_id}/apk-analysis",
            headers={"x-test-owner": "owner-a"},
        )
        self.assertEqual(response.status_code, 422)

    def test_import_rejects_garbage_bytes(self):
        """PK 魔数都不满足的内容必须被 422 拒绝。"""
        def _write(target):
            target.write_bytes(b"R" * 64)

        artifact_id = self._corrupt_artifact(_write)
        response = self.client.post(
            f"/api/redmine-agent/artifacts/{artifact_id}/apk-analysis",
            headers={"x-test-owner": "owner-a"},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
