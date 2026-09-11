"""Skill archive download endpoint + deprecated installer wrapper tests.

The legacy bash installer (skills/.../scripts/install.sh) is
RETIRED — the single install lifecycle is GET /api/agent/install →
gms-agent install/update/rollback/enroll. What remains here:

* SkillInstallerTests      — /api/system/skills zip download semantics
                             (traversal guard, gms-remote-test root)

The retired /api/system/skills/install.sh wrapper was removed; the one-line
install path is GET /api/agent/install.sh (see agent_package_registry).
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
