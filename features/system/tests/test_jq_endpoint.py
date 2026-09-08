"""Controller jq binary endpoint tests (2026-09-08 audit §十二 fix).

`/api/system/tools/jq` serves the pinned jq binary so air-gapped build
servers can complete the skill installer bootstrap without GitHub access.
The endpoint must: serve the pinned path only, advertise the exact
SHA-256 in X-GMS-SHA256, and 404 (never a redirect/placeholder) when the
operator has not staged the binary.
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.system import api as system_api


class JqBinaryEndpointTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(system_api.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()

    def test_serves_pinned_binary_with_matching_sha256_header(self):
        # Stage a deterministic fake "binary"; the endpoint must stream it
        # verbatim and publish its exact SHA-256.
        payload = b"#!/bin/sh\necho fake-jq-for-tests\n"

        def _fake_read(_func):
            return payload, hashlib.sha256(payload).hexdigest()

        with patch.object(system_api, "_JQ_BIN_PATH", "/tmp/does-not-exist-jq"), patch(
            "features.system.api.asyncio.to_thread",
            side_effect=_fake_read,
        ):
            response = self.client.get("/api/system/tools/jq")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, payload)
        self.assertEqual(
            response.headers.get("X-GMS-SHA256"),
            hashlib.sha256(payload).hexdigest(),
        )
        self.assertEqual(
            response.headers.get("Content-Type"),
            "application/octet-stream",
        )
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_missing_pinned_binary_returns_404(self):
        with patch.object(
            system_api, "_JQ_BIN_PATH", "/tmp/definitely-missing-jq-binary"
        ):
            response = self.client.get("/api/system/tools/jq")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["success"])

    def test_real_pinned_binary_matches_header_when_present(self):
        """When the operator staged tools/jq-linux-amd64, header == content."""
        real = Path(system_api._JQ_BIN_PATH)
        if not real.is_file():
            self.skipTest("operator has not staged tools/jq-linux-amd64")
        response = self.client.get("/api/system/tools/jq")
        self.assertEqual(response.status_code, 200)
        digest = hashlib.sha256(response.content).hexdigest()
        self.assertEqual(response.headers.get("X-GMS-SHA256"), digest)
        # Executable bit preserved for direct install.
        self.assertTrue(response.content[:4] == b"\x7fELF")

    def test_installer_prefers_controller_before_github(self):
        """install.sh 优先级必须是 Controller → GitHub，而非虚假的 python3 fallback。"""
        installer = (
            Path(__file__).resolve().parents[3]
            / "skills"
            / "gms-remote-test"
            / "scripts"
            / "install.sh"
        )
        text = installer.read_text(encoding="utf-8")
        self.assertIn("install_jq_from_controller", text)
        self.assertIn("/api/system/tools/jq", text)
        # 旧的虚假承诺必须消失：python3 不是可用的 JSON fallback。
        self.assertNotIn(
            "the CLI will use its JSON fallback",
            text,
        )
        # Controller 尝试必须先于 GitHub fallback。
        self.assertLess(
            text.index("install_jq_from_controller"),
            text.index("install_portable_jq ||"),
        )


if __name__ == "__main__":
    unittest.main()
