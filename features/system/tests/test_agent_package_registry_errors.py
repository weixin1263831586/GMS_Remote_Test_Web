from __future__ import annotations

import hashlib
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest.mock import Mock, patch

from fastapi import Request

from features.system import agent_package_registry as registry
from scripts.release.build_binary_release import _write_agent_module


class AgentPackageRegistryErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_compiled_registry_serves_manifest_archive_and_bootstrap_without_source_tree(self):
        with TemporaryDirectory() as directory:
            path = _write_agent_module(Path(directory) / "build-input")
            bundled = ModuleType("_gms_bundled_agent")
            exec(compile(path.read_text(), str(path), "exec"), bundled.__dict__)
            request = Request({"type": "http", "scheme": "https", "server": ("controller.test", 5443),
                               "path": "/", "root_path": "", "headers": []})
            with (
                patch.object(registry, "_bundled_agent", bundled),
                patch.object(registry, "PROJECT_ROOT", Path(directory) / "absent"),
                patch.object(registry, "PACKAGE_YAML", Path(directory) / "absent/package.yaml"),
                patch.object(registry, "AGENT_PACKAGE_DIR", Path(directory) / "absent/plugins"),
                patch.object(registry, "_manifest_signature", return_value="signed") as sign,
                patch("features.system.skill_archive_signing.skill_verify_key_b64", return_value="pinned-key"),
            ):
                manifest = await registry.agent_package_manifest(request)
                archive = await registry.agent_package_download(bundled.VERSION, request)
                installer = await registry.agent_bootstrap_installer(request)
                wrong_version = await registry.agent_package_download("999.0.0", request)
            digest = hashlib.sha256(bundled.ARCHIVE).hexdigest()
            self.assertTrue(manifest["success"])
            self.assertEqual(manifest["artifacts"]["universal"]["sha256"], digest)
            self.assertEqual(archive.body, bundled.ARCHIVE)
            sign.assert_called_once_with(bundled.VERSION, digest, len(archive.body))
            self.assertEqual(archive.headers["x-gms-sha256"], digest)
            self.assertEqual(installer.status_code, 200)
            self.assertIn(b"https://controller.test:5443", installer.body)
            self.assertIn(b"pinned-key", installer.body)
            self.assertNotIn(b"__GMS_AGENT_DEFAULT_SERVER__", installer.body)
            self.assertEqual(wrong_version.status_code, 404)

    async def test_manifest_and_download_build_off_the_event_loop(self):
        main_thread = threading.get_ident()
        build_threads = []

        def build(_version):
            build_threads.append(threading.get_ident())
            return b'archive'

        with patch.object(registry, '_package_version', return_value='1.2.3'), \
                patch.object(registry, '_immutable_archive', side_effect=build), \
                patch.object(registry, '_manifest_signature', return_value='signed'):
            await registry.agent_package_manifest(Mock())
            assert (await registry.agent_package_download('1.2.3', Mock())).status_code == 200
        assert len(build_threads) == 2
        assert all(thread != main_thread for thread in build_threads)

    async def test_archive_reuses_bytes_until_payload_metadata_changes(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            payload = Path(tmp) / 'scripts/runtime.sh'
            payload.parent.mkdir()
            payload.write_text('first')
            with patch.object(registry, 'AGENT_PACKAGE_DIR', Path(tmp)), \
                    patch.object(registry, '_IMMUTABLE_CACHE', {}), \
                    patch.object(registry, '_SOURCE_STAMPS', {}), \
                    patch.object(registry, 'build_package_bytes', side_effect=[b'first', b'changed']) as build:
                assert registry._immutable_archive('1.2.3') == b'first'
                assert registry._immutable_archive('1.2.3') == b'first'
                assert build.call_count == 1
                payload.write_text('changed')
                with self.assertRaises(registry.DriftedVersionError):
                    registry._immutable_archive('1.2.3')

    async def test_public_manifest_hides_local_build_path(self):
        with (
            patch.object(registry, "_package_version", return_value="1.2.3"),
            patch.object(
                registry,
                "_immutable_archive",
                side_effect=FileNotFoundError("/srv/private/package.zip"),
            ),
        ):
            response = await registry.agent_package_manifest(Mock())

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(payload["code"], "INTERNAL_ERROR")
        self.assertIn("request_id=", payload["error"])
        self.assertNotIn("/srv/private", response.body.decode())

    async def test_public_download_hides_local_build_path(self):
        with (
            patch.object(registry, "_package_version", return_value="1.2.3"),
            patch.object(
                registry,
                "_immutable_archive",
                side_effect=FileNotFoundError("/srv/private/package.zip"),
            ),
        ):
            response = await registry.agent_package_download("1.2.3", Mock())

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(payload["code"], "INTERNAL_ERROR")
        self.assertIn("request_id=", payload["error"])
        self.assertNotIn("/srv/private", response.body.decode())


if __name__ == "__main__":
    unittest.main()
