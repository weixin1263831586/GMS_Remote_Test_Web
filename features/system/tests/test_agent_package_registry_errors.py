from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import Mock, patch

from features.system import agent_package_registry as registry


class AgentPackageRegistryErrorTests(unittest.IsolatedAsyncioTestCase):
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
