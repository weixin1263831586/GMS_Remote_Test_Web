from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from features.system import agent_package_registry as registry


class AgentPackageRegistryErrorTests(unittest.IsolatedAsyncioTestCase):
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
