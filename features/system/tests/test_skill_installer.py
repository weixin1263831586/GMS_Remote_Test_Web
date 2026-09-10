"""Skill archive download endpoint + deprecated installer wrapper tests.

11.txt 收口: the legacy bash installer (skills/.../scripts/install.sh) is
RETIRED — the single install lifecycle is GET /api/agent/install →
gms-agent install/update/rollback/enroll. What remains here:

* SkillInstallerTests      — /api/system/skills zip download semantics
                             (traversal guard, gms-remote-test root)
* DeprecatedInstallerTests — /api/system/skills/install.sh now serves a
                             short forwarder to the gms-agent bootstrap
"""

from __future__ import annotations

import hashlib
import unittest
import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.system.api import _skill_directory, router


ROOT = Path(__file__).resolve().parents[3]
SKILL_SOURCE = ROOT / "agent" / "gms-remote-test" / "skill"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


class SkillInstallerTests(unittest.TestCase):
    def test_skill_directory_rejects_path_traversal(self):
        self.assertIsNone(_skill_directory("../configs"))
        self.assertIsNone(_skill_directory("/tmp"))
        self.assertIsNone(_skill_directory("gms_remote_test"))

    def test_skills_zip_contains_skill_source_with_legacy_root(self):
        client = TestClient(_build_app(), base_url="https://testserver")
        response = client.get("/api/system/skills")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["Content-Type"], "application/zip")
        # Integrity headers survive the layout migration.
        self.assertEqual(
            response.headers["X-GMS-SHA256"],
            hashlib.sha256(response.content).hexdigest(),
        )
        with zipfile.ZipFile(__import__("io").BytesIO(response.content)) as archive:
            names = archive.namelist()
        # Legacy archives root everything under gms-remote-test/.
        self.assertTrue(any(n.startswith("gms-remote-test/") for n in names))
        self.assertIn("gms-remote-test/SKILL.md", names)

    def test_skills_zip_rejects_unknown_skill(self):
        client = TestClient(_build_app(), base_url="https://testserver")
        response = client.get("/api/system/skills", params={"skill_name": "other-skill"})
        self.assertEqual(response.status_code, 404)


class DeprecatedInstallerTests(unittest.TestCase):
    """Controller 渲染 install.sh wrapper 时必须拒绝可疑 Host（3d1e089 防护保留）."""

    def _installer(self, host: str):
        client = TestClient(_build_app(), base_url="http://placeholder")
        return client.get(
            "/api/system/skills/install.sh", headers={"Host": host}
        )

    def test_normal_hosts_render_forwarder(self):
        # 11.txt: the endpoint is a deprecated forwarder to the gms-agent
        # bootstrap; it must still render a server-bound script.
        for host in ("172.16.14.233:5001", "gms.example.local", "[::1]:5001"):
            with self.subTest(host=host):
                response = self._installer(host)
                self.assertEqual(response.status_code, 200)
                self.assertIn("/api/agent/install", response.text)
                self.assertIn("gms-agent", response.text)
                self.assertIn("install --server", response.text)
                # The retired bash installer must NOT be resurrected here.
                self.assertNotIn("GMS_SKILL_DOWNLOAD_URL", response.text)

    def test_shell_metacharacter_host_is_rejected(self):
        for host in ("evil'; id; '", "a b", "h/../../../etc", "h?x=1", "h#f"):
            with self.subTest(host=host):
                response = self._installer(host)
                self.assertEqual(response.status_code, 400)
                self.assertIn("服务地址", response.json()["error"])


if __name__ == "__main__":
    unittest.main()
