"""远端结果目录 zip 下载的超时语义回归。

Tradefed 结果目录（如 ``2026-09-01_09.25.32.033+0800_test_approval-go-results.zip``）
在远端打包耗时远超普通 stat 脚本；此前 ``download-dir`` 沿用默认 20s SSH
超时，失败落 500，浏览器附件下载只显示「无法下载 - 网络问题」。这里固定
两件事：打包脚本使用可配置大超时；超时/失败映射 504 DEPENDENCY_TIMEOUT
并归还 SSH 连接。
"""

import unittest
from unittest import mock

from features.test_execution import suites_api


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


class TimingOutSshManager:
    def __init__(self):
        self.returned = 0
        self.saw_timeout = None

    def get_connection(self, _config):
        return object()

    def return_connection(self, _ssh):
        self.returned += 1

    def execute_command(self, _ssh, command, timeout=30, get_pty=False):
        self.saw_timeout = timeout
        raise TimeoutError(f"SSH command timed out after {timeout} seconds")


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
        manager = TimingOutSshManager()
        response = await self._download(manager)
        # handle_api_errors 把 ApiError 转为 504 响应（浏览器可感知语义，
        # 而不是通用 500「网络问题」）。
        self.assertEqual(response.status_code, 504)
        self.assertIn("DEPENDENCY_TIMEOUT", response.body.decode("utf-8"))
        # 超时路径必须归还 SSH 连接，不能泄漏回池。
        self.assertEqual(manager.returned, 1)

    async def test_remote_zip_script_uses_configured_large_timeout(self):
        manager = TimingOutSshManager()
        with mock.patch.object(
            suites_api, "suite_dir_zip_timeout", return_value=600
        ):
            await self._download(manager)
        # 打包脚本的超时来自 suite_dir_zip_timeout（远端压缩耗时远超 20s），
        # 而不是 _run_suite_file_script 的普通默认值。
        self.assertEqual(manager.saw_timeout, 600)

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


if __name__ == "__main__":
    unittest.main()
