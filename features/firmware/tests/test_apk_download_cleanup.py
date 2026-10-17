"""APK 源码下载临时 ZIP 的清理回归。

`iterfile` 的 `os.remove` 原来写在 `with open(...)` 之后：客户端中断下载
（生成器被关闭，yield 处抛 GeneratorExit）时 remove 永远执行不到，临时
ZIP 残留在 apk_uploads 下。现在 try/finally 保证任何退出路径都清理。
"""

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.firmware import apk_api, runtime


def _principal():
    return SimpleNamespace(
        id="u1",
        username="user",
        role="user",
        resource_owner_id="u1",
        has_permission=lambda _scope: True,
    )


class ApkDownloadCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.upload_dir = Path(self.tmp.name) / "uploads"
        self.output_dir = Path(self.tmp.name) / "output" / "sources"
        self.output_dir.mkdir(parents=True)
        (self.output_dir / "Example.java").write_text(
            "class Example {}\n", encoding="utf-8"
        )
        self.task_id = "1b2b3c4d-1111-2222-3333-444455556666"
        runtime.configure_runtime(
            apk_upload_dir=str(self.upload_dir),
            global_state=SimpleNamespace(
                apk_analysis_tasks={},
                apk_analysis_tasks_lock=threading.RLock(),
            ),
            project_root=".",
        )
        # configure_runtime 会用持久化 store 重放任务表，种子任务必须在
        # 其后注入内存表。
        with runtime.global_state.apk_analysis_tasks_lock:
            runtime.global_state.apk_analysis_tasks[self.task_id] = {
                "task_id": self.task_id,
                "filename": "app.apk",
                "status": "completed",
                "progress": 100,
                "timestamp": 1,
                "owner_id": "u1",
                "output_dir": str(self.output_dir.parent),
            }
        app = FastAPI()
        app.include_router(apk_api.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        runtime.configure_runtime(
            apk_upload_dir=None,
            apk_task_store=None,
            global_state=None,
        )
        self.tmp.cleanup()

    def _expected_zip(self) -> Path:
        return (
            self.upload_dir / self.task_id / "app_decompiled.zip"
        )

    def test_aborted_download_still_removes_temp_zip(self):
        with patch.object(
            apk_api, "require_authenticated_user", return_value=_principal()
        ), patch(
            "features.auth.access.require_authenticated_user",
            return_value=_principal(),
        ), self.client.stream(
            "GET", f"/api/apk/download/{self.task_id}"
        ) as response:
            self.assertEqual(response.status_code, 200)
            for chunk in response.iter_bytes():
                if chunk:
                    break  # 中途断开
        self.assertFalse(
            self._expected_zip().exists(),
            "客户端中断后临时 ZIP 必须被清理",
        )

    def test_completed_download_removes_temp_zip(self):
        with patch.object(
            apk_api, "require_authenticated_user", return_value=_principal()
        ), patch(
            "features.auth.access.require_authenticated_user",
            return_value=_principal(),
        ):
            response = self.client.get(f"/api/apk/download/{self.task_id}")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content)
        self.assertFalse(
            self._expected_zip().exists(),
            "完整下载后临时 ZIP 必须被清理",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
