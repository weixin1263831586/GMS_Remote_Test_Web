import json
import io
import os
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.requests import Request

from features.test_execution import suites_api, transfers_api
from features.test_execution.models import SuiteApkAnalyzeRequest, TradefedListResultsRequest


class LocalConfigManager:
    def __init__(self, suites_path: str):
        self.suites_path = suites_path

    def load_config(self):
        return {
            "ubuntu_host": "127.0.0.1",
            "ubuntu_user": "tester",
            "suites_path": self.suites_path,
        }

    def is_config_host_local(self, _config):
        return True

    def get_ubuntu_user(self, _config):
        return "tester"


class FailingSshManager:
    def __getattr__(self, name):
        raise AssertionError(f"Controller-local suite operation attempted SSH: {name}")


class LocalSuiteApiTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _stream_body(response):
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return b"".join(chunks)

    async def test_file_browser_reads_controller_suite_without_ssh(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "GMS-Suite"
            tools = root / "android-vts" / "android-vts" / "tools"
            (tools / "subdir").mkdir(parents=True)
            (tools / "sample.apk").write_bytes(b"apk")
            manager = LocalConfigManager(str(root))
            old_config = suites_api.runtime.config_manager
            old_ssh = suites_api.runtime.ssh_manager
            suites_api.runtime.config_manager = manager
            suites_api.runtime.ssh_manager = FailingSshManager()
            try:
                response = await suites_api.list_suite_files(
                    suite_path=str(tools), path=""
                )
            finally:
                suites_api.runtime.config_manager = old_config
                suites_api.runtime.ssh_manager = old_ssh

            payload = json.loads(response.body)
            self.assertTrue(payload["success"], payload)
            self.assertEqual(
                [item["name"] for item in payload["data"]["items"]],
                ["tools"],
            )

    async def test_file_download_reads_controller_suite_without_ssh(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "GMS-Suite"
            tools = root / "android-vts" / "android-vts" / "tools"
            report = tools.parent / "results" / "run" / "report.html"
            report.parent.mkdir(parents=True)
            report.write_bytes(b"<html>local report</html>")
            manager = LocalConfigManager(str(root))
            old_config = suites_api.runtime.config_manager
            old_ssh = suites_api.runtime.ssh_manager
            suites_api.runtime.config_manager = manager
            suites_api.runtime.ssh_manager = FailingSshManager()
            try:
                response = await suites_api.download_suite_file(
                    suite_path=str(tools),
                    path="results/run/report.html",
                    inline=True,
                )
                body = await self._stream_body(response)
            finally:
                suites_api.runtime.config_manager = old_config
                suites_api.runtime.ssh_manager = old_ssh

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-disposition"], "inline")
            self.assertEqual(body, b"<html>local report</html>")

    async def test_directory_download_reads_controller_suite_without_ssh(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "GMS-Suite"
            tools = root / "android-vts" / "android-vts" / "tools"
            result_dir = tools.parent / "results" / "run"
            result_dir.mkdir(parents=True)
            (result_dir / "result.txt").write_text("local", encoding="utf-8")
            manager = LocalConfigManager(str(root))
            old_config = suites_api.runtime.config_manager
            old_ssh = suites_api.runtime.ssh_manager
            suites_api.runtime.config_manager = manager
            suites_api.runtime.ssh_manager = FailingSshManager()
            try:
                response = await suites_api.download_suite_directory(
                    suite_path=str(tools), path="results/run"
                )
                body = await self._stream_body(response)
            finally:
                suites_api.runtime.config_manager = old_config
                suites_api.runtime.ssh_manager = old_ssh

            self.assertEqual(response.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                self.assertEqual(archive.read("result.txt"), b"local")

    async def test_apk_analysis_copies_controller_suite_without_ssh(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "GMS-Suite"
            tools = root / "android-vts" / "android-vts" / "tools"
            apk = tools.parent / "testcases" / "sample.apk"
            apk.parent.mkdir(parents=True)
            apk.write_bytes(b"apk-content")
            upload_dir = Path(tmp) / "uploads"
            manager = LocalConfigManager(str(root))
            captured = {}
            old_values = (
                suites_api.runtime.config_manager,
                suites_api.runtime.ssh_manager,
                suites_api.runtime.apk_upload_dir,
                suites_api.runtime.apk_max_file_size,
                suites_api.runtime.normalize_apk_filename,
                suites_api.runtime.safe_join,
                suites_api.runtime.create_apk_task,
                suites_api.runtime.get_client_id_from_request,
            )
            suites_api.runtime.config_manager = manager
            suites_api.runtime.ssh_manager = FailingSshManager()
            suites_api.runtime.apk_upload_dir = str(upload_dir)
            suites_api.runtime.apk_max_file_size = 1024 * 1024
            suites_api.runtime.normalize_apk_filename = os.path.basename
            suites_api.runtime.safe_join = lambda base, name: os.path.join(base, name)
            suites_api.runtime.create_apk_task = lambda *args: captured.setdefault("args", args)
            suites_api.runtime.get_client_id_from_request = lambda _request: "client-local"
            try:
                response = await suites_api.create_suite_apk_analysis_task(
                    SuiteApkAnalyzeRequest(
                        suite_path=str(tools), path="testcases/sample.apk"
                    ),
                    Request({"type": "http", "method": "POST", "path": "/"}),
                )
            finally:
                (
                    suites_api.runtime.config_manager,
                    suites_api.runtime.ssh_manager,
                    suites_api.runtime.apk_upload_dir,
                    suites_api.runtime.apk_max_file_size,
                    suites_api.runtime.normalize_apk_filename,
                    suites_api.runtime.safe_join,
                    suites_api.runtime.create_apk_task,
                    suites_api.runtime.get_client_id_from_request,
                ) = old_values

            payload = json.loads(response.body)
            self.assertTrue(payload["success"], payload)
            self.assertEqual(Path(captured["args"][1]).read_bytes(), b"apk-content")
            self.assertEqual(captured["args"][3], "client-local")

    async def test_list_results_runs_controller_tradefed_without_ssh(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "GMS-Suite"
            tools = root / "android-vts" / "android-vts" / "tools"
            tools.mkdir(parents=True)
            launcher = tools / "vts-tradefed"
            launcher.write_text(
                "#!/bin/sh\n"
                "printf 'vts-tf > '\n"
                "read command\n"
                "printf 'Session  Pass  Fail  Modules Complete  Result Directory  Test Plan  Device serial(s)  Build ID  Product\\n'\n"
                "printf '1  9  1  1 of 1  2026.07.18_12.00.00  vts  SERIAL  BUILD  product\\n'\n"
                "printf 'vts-tf > '\n"
                "read command\n",
                encoding="utf-8",
            )
            os.chmod(launcher, 0o755)
            manager = LocalConfigManager(str(root))
            old_config = transfers_api.runtime.config_manager
            old_ssh = transfers_api.runtime.ssh_manager
            old_help = transfers_api.runtime.generate_help_or_continue
            transfers_api.runtime.config_manager = manager
            transfers_api.runtime.ssh_manager = FailingSshManager()
            transfers_api.runtime.generate_help_or_continue = lambda *_args: None
            try:
                response = await transfers_api.list_tradefed_results(
                    help=False,
                    req=TradefedListResultsRequest(suite_path=str(tools)),
                )
            finally:
                transfers_api.runtime.config_manager = old_config
                transfers_api.runtime.ssh_manager = old_ssh
                transfers_api.runtime.generate_help_or_continue = old_help

            payload = json.loads(response.body)
            self.assertTrue(payload["success"])
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["results"][0]["pass"], 9)


if __name__ == "__main__":
    unittest.main()
