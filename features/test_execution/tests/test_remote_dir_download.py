"""远端结果目录 zip 下载的超时语义回归。

Tradefed 结果目录（如 ``2026-09-01_09.25.32.033+0800_test_approval-go-results.zip``）
在远端打包耗时远超普通 stat 脚本；此前 ``download-dir`` 沿用默认 20s SSH
超时，失败落 500，浏览器附件下载只显示「无法下载 - 网络问题」。这里固定
两件事：打包脚本使用可配置大超时；超时映射 504 DEPENDENCY_TIMEOUT、
其他 SSH 失败映射 502 UPSTREAM_FAILURE，并归还 SSH 连接。
"""

import asyncio
import json
import shlex
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from starlette.requests import ClientDisconnect

from features.test_execution import suites_api
from foundation.ssh_executor import SSHExecutor


class RemoteConfigManager:
    def load_config(self):
        return {
            "ubuntu_host": "192.0.2.50",
            "ubuntu_user": "tester",
            "suites_path": "/srv/GMS-Suite",
        }

    def get_ubuntu_user(self, _config):
        return "tester"

    def is_config_host_local(self, _config):
        return False


class FailingSshManager:
    def __init__(self, error):
        self.returned = 0
        self.saw_timeout = None
        self.ssh = mock.Mock()
        self.ssh.exec_command.side_effect = error

    def get_connection(self, _config):
        return self.ssh

    def return_connection(self, _ssh):
        self.returned += 1

    def execute_command(self, _ssh, command, timeout=30, get_pty=False):
        if self.saw_timeout is None:
            self.saw_timeout = timeout
        return SSHExecutor().run(_ssh, command, timeout=timeout, get_pty=get_pty)


class SuiteDirectoryTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def _download(self, ssh_manager):
        old_config = suites_api.runtime.config_manager
        old_ssh = suites_api.runtime.ssh_manager
        suites_api.runtime.config_manager = RemoteConfigManager()
        suites_api.runtime.ssh_manager = ssh_manager
        try:
            return await suites_api.download_suite_directory(
                suite_path="/srv/GMS-Suite/android-cts/tools",
                path="results/2026-09-01_09.25.32.033+0800_test_approval-go",
            )
        finally:
            suites_api.runtime.config_manager = old_config
            suites_api.runtime.ssh_manager = old_ssh

    async def test_remote_zip_timeout_maps_to_504_and_returns_connection(self):
        manager = FailingSshManager(TimeoutError("SSH command timed out"))
        response = await self._download(manager)
        # handle_api_errors 把 ApiError 转为 504 响应（浏览器可感知语义，
        # 而不是通用 500「网络问题」）。
        self.assertEqual(response.status_code, 504)
        self.assertIn("DEPENDENCY_TIMEOUT", response.body.decode("utf-8"))
        # 超时路径必须归还 SSH 连接，不能泄漏回池。
        self.assertEqual(manager.returned, 1)

    async def test_remote_ssh_failure_maps_to_502_and_returns_connection(self):
        manager = FailingSshManager(OSError("SSH connection lost"))
        response = await self._download(manager)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(json.loads(response.body)["code"], "UPSTREAM_FAILURE")
        self.assertEqual(manager.returned, 1)

    async def test_remote_zip_script_uses_configured_large_timeout(self):
        manager = FailingSshManager(TimeoutError("SSH command timed out"))
        with mock.patch.object(
            suites_api, "suite_dir_zip_timeout", return_value=600
        ):
            await self._download(manager)
        # 打包脚本的超时来自 suite_dir_zip_timeout（远端压缩耗时远超 20s），
        # 而不是 _run_suite_file_script 的普通默认值。
        self.assertEqual(manager.saw_timeout, 600)

    async def test_sftp_open_failure_closes_session_and_removes_remote_zip(self):
        manager = DownloadSshManager(open_failure=True)
        response = await self._download(manager)
        self.assertEqual(response.status_code, 502, response.body)
        manager.sftp.close.assert_called_once()
        manager.sftp.remove.assert_called_once_with(manager.zip_path)
        self.assertEqual(manager.returned, 1)
        self.assertIn(suites_api.SUITE_DIR_CLEANUP_SCRIPT, manager.scripts)

    async def test_sftp_initialization_failure_still_removes_archive(self):
        manager = DownloadSshManager()
        manager.ssh.open_sftp.side_effect = OSError("SFTP unavailable")
        response = await self._download(manager)
        self.assertEqual(response.status_code, 502, response.body)
        self.assertEqual(manager.returned, 1)
        self.assertIn(suites_api.SUITE_DIR_CLEANUP_SCRIPT, manager.scripts)

    async def test_read_failure_closes_all_resources_once(self):
        manager = DownloadSshManager()
        manager.file.read.side_effect = OSError("connection lost")
        response = await self._download(manager)
        with self.assertRaises(OSError):
            async for _ in response.body_iterator:
                pass
        await asyncio.to_thread(response.cleanup)
        manager.file.close.assert_called_once()
        manager.sftp.close.assert_called_once()
        self.assertEqual(manager.returned, 1)

    async def test_response_construction_failure_closes_all_resources(self):
        manager = DownloadSshManager()
        with mock.patch.object(suites_api, "_SuiteStreamingResponse", side_effect=ValueError("invalid header")):
            response = await self._download(manager)
        self.assertEqual(response.status_code, 500)
        manager.file.close.assert_called_once()
        manager.sftp.remove.assert_called_once_with(manager.zip_path)
        manager.sftp.close.assert_called_once()
        self.assertEqual(manager.returned, 1)

    async def test_disconnect_before_body_iteration_closes_all_resources(self):
        manager = DownloadSshManager()
        response = await self._download(manager)

        async def receive():
            return {"type": "http.disconnect"}

        async def send(_message):
            raise OSError("client disconnected")

        with self.assertRaises(ClientDisconnect):
            await response({"type": "http", "method": "GET", "asgi": {"spec_version": "2.4"}}, receive, send)
        manager.file.read.assert_not_called()
        manager.file.close.assert_called_once()
        manager.sftp.close.assert_called_once()
        self.assertEqual(manager.returned, 1)

    async def test_zip_preparation_does_not_block_the_event_loop(self):
        entered, release = threading.Event(), threading.Event()
        manager = DownloadSshManager(entered=entered, release=release)
        task = asyncio.create_task(self._download(manager))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            await asyncio.wait_for(asyncio.sleep(0), timeout=0.2)
        finally:
            release.set()
        response = await task
        chunks = [chunk async for chunk in response.body_iterator]
        self.assertEqual(chunks, [b"archive"])
        self.assertEqual(manager.returned, 1)

    async def test_cancelled_preparation_cannot_open_sftp_after_connection_return(self):
        entered, release = threading.Event(), threading.Event()
        manager = DownloadSshManager(entered=entered, release=release)
        task = asyncio.create_task(self._download(manager))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
            self.assertEqual(manager.returned, 1)
        finally:
            release.set()
        self.assertTrue(await asyncio.to_thread(manager.prepared.wait, 3))
        await asyncio.sleep(0)
        manager.ssh.open_sftp.assert_not_called()

    async def test_untrusted_archive_path_is_not_opened_or_deleted(self):
        manager = DownloadSshManager(archive_path="/etc/passwd")
        response = await self._download(manager)
        self.assertEqual(response.status_code, 502)
        manager.ssh.open_sftp.assert_not_called()
        manager.sftp.remove.assert_not_called()
        self.assertEqual(manager.returned, 1)

    async def test_size_disk_and_timeout_rejections_keep_semantic_codes(self):
        for code, status in [("INVALID_SEMANTICS", 422), ("DEPENDENCY_UNAVAILABLE", 503),
                             ("DEPENDENCY_TIMEOUT", 504), ("UPSTREAM_FAILURE", 502)]:
            with self.subTest(code=code):
                manager = DownloadSshManager(rejection=code)
                response = await self._download(manager)
                self.assertEqual(response.status_code, status)
                self.assertEqual(json.loads(response.body)["code"], code)
                self.assertEqual(manager.returned, 1)

    def test_zip_timeout_env_floor_and_default(self):
        with mock.patch.dict("os.environ", {"GMS_SUITE_ZIP_TIMEOUT_SECONDS": "5"}):
            self.assertEqual(suites_api.suite_dir_zip_timeout(), 60)
        with mock.patch.dict("os.environ", {"GMS_SUITE_ZIP_TIMEOUT_SECONDS": "3600"}):
            self.assertEqual(suites_api.suite_dir_zip_timeout(), 3600)
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("GMS_SUITE_ZIP_TIMEOUT_SECONDS", None)
            self.assertEqual(suites_api.suite_dir_zip_timeout(), 600)
        with mock.patch.dict("os.environ", {"GMS_SUITE_ZIP_TIMEOUT_SECONDS": "abc"}):
            self.assertEqual(suites_api.suite_dir_zip_timeout(), 600)


class DownloadSshManager:
    def __init__(self, *, open_failure=False, rejection=None, entered=None, release=None, archive_path=None):
        self.returned = 0
        self.scripts = []
        self.zip_path = ""
        self.rejection = rejection
        self.entered, self.release = entered, release
        self.archive_path = archive_path
        self.prepared = threading.Event()
        self.file = mock.Mock()
        self.file.read.side_effect = [b"archive", b""]
        self.sftp = mock.Mock()
        self.sftp.open.return_value = self.file
        if open_failure:
            self.sftp.open.side_effect = OSError("remote file could not be opened")
        self.ssh = mock.Mock()
        self.ssh.open_sftp.return_value = self.sftp

    def get_connection(self, _config):
        return self.ssh

    def return_connection(self, _ssh):
        self.returned += 1

    def execute_command(self, _ssh, command, timeout=30, get_pty=False):
        argv = shlex.split(command)
        script = argv[2]
        self.scripts.append(script)
        if script == suites_api.SUITE_DIR_ZIP_SCRIPT:
            if self.entered is not None:
                self.entered.set()
                if not self.release.wait(3):
                    raise TimeoutError("blocked preparation")
            self.zip_path = self.archive_path or f"/tmp/gms-suite-downloads-1000/{argv[5]}/archive.zip"
            result = ({"success": False, "code": self.rejection, "error": "archive rejected"}
                      if self.rejection else {"success": True, "zip_path": self.zip_path,
                                             "name": "run", "size": 7})
            self.prepared.set()
        else:
            result = {"success": True}
        return SimpleNamespace(ok=True, stdout=json.dumps(result), stderr="")


class DownloadFileSshManager:
    def __init__(self, *, entered=None, release=None):
        self.returned = 0
        self.entered, self.release = entered, release
        self.prepared = threading.Event()
        self.file = mock.Mock()
        self.file.read.side_effect = [b"report", b""]
        self.sftp = mock.Mock()
        self.sftp.open.return_value = self.file
        self.ssh = mock.Mock()
        self.ssh.open_sftp.return_value = self.sftp

    def get_connection(self, _config):
        return self.ssh

    def return_connection(self, _ssh):
        self.returned += 1

    def execute_command(self, _ssh, _command, timeout=30, get_pty=False):
        if self.entered is not None:
            self.entered.set()
            if not self.release.wait(3):
                raise TimeoutError("blocked preparation")
        self.prepared.set()
        payload = {
            "success": True,
            "real_path": "/srv/GMS-Suite/results/report.html",
            "name": "report.html",
            "size": 6,
        }
        return SimpleNamespace(ok=True, stdout=json.dumps(payload), stderr="")


class RemoteFileDownloadLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def _download(self, manager):
        old_config = suites_api.runtime.config_manager
        old_ssh = suites_api.runtime.ssh_manager
        suites_api.runtime.config_manager = RemoteConfigManager()
        suites_api.runtime.ssh_manager = manager
        try:
            return await suites_api.download_suite_file(
                suite_path="/srv/GMS-Suite/android-cts/tools",
                path="results/report.html",
                inline=True,
            )
        finally:
            suites_api.runtime.config_manager = old_config
            suites_api.runtime.ssh_manager = old_ssh

    async def test_preparation_does_not_block_the_event_loop(self):
        entered, release = threading.Event(), threading.Event()
        manager = DownloadFileSshManager(entered=entered, release=release)
        task = asyncio.create_task(self._download(manager))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            await asyncio.wait_for(asyncio.sleep(0), timeout=0.2)
        finally:
            release.set()
        response = await task
        self.assertEqual(
            [chunk async for chunk in response.body_iterator],
            [b"report"],
        )
        self.assertEqual(manager.returned, 1)

    async def test_cancelled_preparation_cannot_open_sftp_after_return(self):
        entered, release = threading.Event(), threading.Event()
        manager = DownloadFileSshManager(entered=entered, release=release)
        task = asyncio.create_task(self._download(manager))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
            self.assertEqual(manager.returned, 0)
        finally:
            release.set()
        self.assertTrue(await asyncio.to_thread(manager.prepared.wait, 3))
        for _ in range(50):
            if manager.returned:
                break
            await asyncio.sleep(0.02)
        manager.ssh.open_sftp.assert_not_called()
        self.assertEqual(manager.returned, 1)

    async def test_disconnect_before_iteration_closes_resources_once(self):
        manager = DownloadFileSshManager()
        response = await self._download(manager)

        async def receive():
            return {"type": "http.disconnect"}

        async def send(_message):
            raise OSError("client disconnected")

        with self.assertRaises(ClientDisconnect):
            await response(
                {"type": "http", "method": "GET", "asgi": {"spec_version": "2.4"}},
                receive,
                send,
            )
        manager.file.read.assert_not_called()
        manager.file.close.assert_called_once()
        manager.sftp.close.assert_called_once()
        self.assertEqual(manager.returned, 1)


if __name__ == "__main__":
    unittest.main()
