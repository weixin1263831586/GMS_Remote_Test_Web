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
from features.system import jq_binary


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
            return payload, hashlib.sha256(payload).hexdigest(), None

        with patch.object(
            jq_binary, "JQ_BIN_PATH", "/tmp/does-not-exist-jq"
        ), patch(
            "features.system.jq_binary.asyncio.to_thread",
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
            jq_binary, "JQ_BIN_PATH", "/tmp/definitely-missing-jq-binary"
        ):
            response = self.client.get("/api/system/tools/jq")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["success"])

    def test_rejects_distro_jq_that_needs_libjq(self):
        """R12: tiny dynamically-linked wrapper builds are refused (404)."""
        with patch.object(
            jq_binary, "JQ_BIN_PATH", "/tmp/does-not-exist-jq"
        ), patch(
            "features.system.jq_binary.asyncio.to_thread",
            side_effect=lambda _func: (
                b"\x7fELF" + b"\x00" * 16,
                "0" * 64,
                "文件过小",
            ),
        ):
            response = self.client.get("/api/system/tools/jq")
        self.assertEqual(response.status_code, 404)

    def test_rejects_non_elf_garbage(self):
        with patch.object(
            jq_binary, "JQ_BIN_PATH", "/tmp/does-not-exist-jq"
        ), patch(
            "features.system.jq_binary.asyncio.to_thread",
            side_effect=lambda _func: (
                b"<html>404 not found</html>" + b"\x00" * (2 * 1024 * 1024),
                "0" * 64,
                "不是 ELF 文件（可能是文本/HTML 误存为二进制）",
            ),
        ):
            response = self.client.get("/api/system/tools/jq")
        self.assertEqual(response.status_code, 404)

    def test_real_pinned_binary_matches_header_when_present(self):
        """When the operator staged tools/jq-linux-amd64, header == content."""
        real = Path(jq_binary.JQ_BIN_PATH)
        if not real.is_file():
            self.skipTest("operator has not staged tools/jq-linux-amd64")
        response = self.client.get("/api/system/tools/jq")
        self.assertEqual(response.status_code, 200)
        digest = hashlib.sha256(response.content).hexdigest()
        self.assertEqual(response.headers.get("X-GMS-SHA256"), digest)
        # Executable bit preserved for direct install.
        self.assertTrue(response.content[:4] == b"\x7fELF")

    # 11.txt: the legacy bash installer (and its jq download priority
    # checks) is retired — installs flow through the gms-agent bootstrap,
    # which downloads the pinned jq via the same /api/system/tools/jq
    # endpoint when the CLI needs it. The old installer-text assertions
    # died with scripts/install.sh.


if __name__ == "__main__":
    unittest.main()
