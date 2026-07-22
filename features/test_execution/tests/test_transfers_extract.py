import io
import json
import stat
import tarfile
import unittest
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from features.test_execution import transfers_api
from features.test_execution.models import TestSuiteExtractRequest as SuiteExtractRequest
from features.test_execution.transfers_api import (
    _curl_resolve_arguments,
    _extract_archive_local_with_progress,
    _path_within_suite_root,
    _resolve_suite_download_target,
    _validate_suite_download_url,
    extract_test_suite_archive,
)
from foundation.outbound import ResolvedOutboundTarget


class SuiteExtractTests(unittest.TestCase):
    def test_download_rejects_non_http_protocols(self):
        for url in ('file:///etc/passwd', 'ftp://example.com/suite.zip', ''):
            with self.assertRaises(ValueError):
                _validate_suite_download_url(url)
        self.assertEqual(
            _validate_suite_download_url('https://example.com/suite.zip'),
            'https://example.com/suite.zip',
        )
        with self.assertRaises(ValueError):
            _validate_suite_download_url(
                'https://user:password@example.com/suite.zip'
            )

    def test_download_blocks_private_targets_and_pins_resolved_addresses(self):
        with self.assertRaises(ValueError):
            _resolve_suite_download_target('http://127.0.0.1/suite.zip')
        target = ResolvedOutboundTarget(
            url='https://example.com/suite.zip',
            hostname='example.com',
            port=443,
            addresses=('93.184.216.34', '2001:db8::1'),
        )
        self.assertEqual(
            _curl_resolve_arguments(target),
            [
                '--resolve', 'example.com:443:93.184.216.34',
                '--resolve', 'example.com:443:[2001:db8::1]',
            ],
        )

    def test_suite_paths_cannot_escape_configured_root(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / 'suites'
            root.mkdir()
            self.assertEqual(
                _path_within_suite_root(str(root / 'suite.zip'), str(root), 'archive'),
                str(root / 'suite.zip'),
            )
            with self.assertRaises(ValueError):
                _path_within_suite_root(str(root / '../escape.zip'), str(root), 'archive')

    def test_zip_extract_rejects_symbolic_link_member(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'unsafe.zip'
            link = zipfile.ZipInfo('link')
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, 'w') as zf:
                zf.writestr(link, '../escape')

            with self.assertRaises(ValueError):
                _extract_archive_local_with_progress(
                    str(archive), str(root / 'extract'), ''
                )

    def test_zip_extract_without_target_dir_rejects_parent_path(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../escape.txt", "bad")

            with self.assertRaises(ValueError):
                _extract_archive_local_with_progress(str(archive), str(root / "extract"), "")

            self.assertFalse((root / "escape.txt").exists())


class RemoteSuiteExtractTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_extract_uses_third_ssh_result_as_exit_code(self):
        class ConfigManager:
            def load_config(self):
                return {"ubuntu_host": "test-host", "ubuntu_user": "tester", "suites_path": "/srv/suites"}

            def get_ubuntu_user(self, config):
                return config["ubuntu_user"]

            def is_config_host_local(self, _config):
                return False

        class SshManager:
            @asynccontextmanager
            async def async_optional_connection(self, _config):
                yield object()

            def execute_command(self, _ssh, command, timeout=None):
                if command.startswith("mkdir"):
                    return "", "", 0
                return "extracted", "", 0

        old_config = transfers_api.runtime.config_manager
        old_ssh = transfers_api.runtime.ssh_manager
        transfers_api.runtime.config_manager = ConfigManager()
        transfers_api.runtime.ssh_manager = SshManager()
        try:
            response = await extract_test_suite_archive(SuiteExtractRequest(
                archive_path="/srv/suites/cts.zip",
                extract_dir="/srv/suites",
                target_dir_name="cts",
            ))
        finally:
            transfers_api.runtime.config_manager = old_config
            transfers_api.runtime.ssh_manager = old_ssh

        payload = json.loads(response.body)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["extracted_path"], "/srv/suites/cts")

    def test_tar_extract_without_target_dir_rejects_parent_path(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "unsafe.tar"
            payload = b"bad"
            info = tarfile.TarInfo("../escape.txt")
            info.size = len(payload)
            with tarfile.open(archive, "w") as tf:
                tf.addfile(info, io.BytesIO(payload))

            with self.assertRaises(ValueError):
                _extract_archive_local_with_progress(str(archive), str(root / "extract"), "")

            self.assertFalse((root / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
