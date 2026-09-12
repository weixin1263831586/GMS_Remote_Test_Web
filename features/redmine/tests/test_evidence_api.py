"""evidence_api HTTP 层测试：scope、owner 隔离、分页、文本分段、图片。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.redmine import evidence_api, evidence_search_api


def _make_config_manager(base_url: str):
    from foundation.secrets import encrypt_secret

    runtime = {
        "redmine": {"base_url": base_url, "domain": ""},
        "redmine_auth": {
            "username": "tester",
            "encrypted_password": encrypt_secret("pw"),
        },
    }
    manager = SimpleNamespace(
        get_runtime_config=lambda: runtime,
        get_redmine_base_url=lambda: base_url,
        for_owner=lambda owner: SimpleNamespace(
            get_runtime_config=lambda: runtime,
            get_redmine_base_url=lambda: base_url,
        ),
    )
    return manager


class EvidenceApiTests(unittest.TestCase):
    def setUp(self):
        self.secret_env = patch.dict(
            "os.environ", {"GMS_SECRET_KEY": Fernet.generate_key().decode("ascii")}
        )
        self.secret_env.start()
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)

        import features.redmine.evidence_store as evidence_store
        import features.redmine.users as redmine_users

        self.users_patch = patch.object(
            redmine_users,
            "owner_redmine_root",
            lambda owner: self.root / "owners" / str(owner),
        )
        self.users_patch.start()
        evidence_store._STORE_CACHE.clear()

        # 预置一个 ready 快照（绕过网络，直接写 store + 文件）。
        from features.redmine.evidence_store import owner_evidence_store

        store = owner_evidence_store("owner-a")
        self.snapshot = store.create_snapshot(issue_id=648526, download_policy="all")
        long_notes = "start\n" + " AssertionError padding " * 400 + "\nend"
        manifest = {
            "issue_id": 648526,
            "subject": "s",
            "description": "desc with AssertionError marker",
            "status": "New",
            "journals": [
                {"id": 1, "user_name": "u", "notes": "first"},
                {"id": 2, "user_name": "u", "notes": long_notes},
                {"id": 3, "user_name": "u", "notes": "third"},
            ],
            "attachments": [],
        }
        store.update_snapshot(
            self.snapshot["snapshot_id"],
            status="ready",
            source_updated_on="2026-09-01T00:00:00Z",
            fetched_at="2026-09-08T00:00:00",
            content_sha256="a" * 64,
            manifest_json=manifest,
            journal_count=3,
            attachment_count=1,
            downloaded_count=1,
            error_json=[],
        )
        # 附件：文本 artifact，含 stored + derived 路径
        artifact = store.create_artifact(
            snapshot_id=self.snapshot["snapshot_id"],
            attachment_id="776656",
            filename="logcat.log",
            original_filename="logcat.log",
            content_type="application/octet-stream",
            kind="log",
            declared_size=32,
        )
        rel = f"648526/{self.snapshot['snapshot_id']}/attachments/{artifact['artifact_id']}/logcat.log"
        target = store.resolve_internal(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = b"AssertionError line one\nline two\n"
        target.write_bytes(payload)
        derived_rel = f"648526/{self.snapshot['snapshot_id']}/derived/{artifact['artifact_id']}.txt"
        store.resolve_internal(derived_rel).parent.mkdir(parents=True, exist_ok=True)
        store.resolve_internal(derived_rel).write_bytes(payload)
        store.update_artifact(
            artifact["artifact_id"],
            status="ready",
            size_bytes=len(payload),
            sha256="b" * 64,
            stored_path=rel,
            derived_text_path=derived_rel,
        )
        self.artifact_id = artifact["artifact_id"]

        app = FastAPI()

        @app.middleware("http")
        async def authenticate(request, call_next):
            owner = request.headers.get("x-test-owner", "owner-a")
            scopes = request.headers.get("x-test-scopes", "user")
            role = "user" if scopes == "user" else "agent_service"
            extra = frozenset() if role == "user" else frozenset(scopes.split(","))
            request.state.current_user = CurrentUser(
                id=owner, username=owner, role=role, extra_permissions=extra
            )
            request.state.auth_method = "session"
            return await call_next(request)

        app.include_router(evidence_api.router)
        app.include_router(evidence_search_api.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.users_patch.stop()
        import features.redmine.evidence_store as evidence_store

        evidence_store._STORE_CACHE.clear()
        self.directory.cleanup()
        self.secret_env.stop()

    # ------------------------------------------------------------------ tests

    def test_status_requires_redmine_read_for_agent_tokens(self):
        response = self.client.get(
            f"/api/redmine-agent/evidence/{self.snapshot['snapshot_id']}",
            headers={"x-test-owner": "owner-a", "x-test-scopes": "devices.read"},
        )
        self.assertEqual(response.status_code, 403)

    def test_status_ok_for_owner_and_reports_completeness(self):
        response = self.client.get(
            f"/api/redmine-agent/evidence/{self.snapshot['snapshot_id']}",
            headers={"x-test-owner": "owner-a"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        data = body["data"]
        self.assertEqual(data["status"], "ready")
        self.assertTrue(data["complete"])
        self.assertEqual(data["journal_count"], 3)
        self.assertEqual(data["completeness"]["requested_downloads"], True)

    def test_cross_owner_snapshot_invisible(self):
        response = self.client.get(
            f"/api/redmine-agent/evidence/{self.snapshot['snapshot_id']}",
            headers={"x-test-owner": "owner-b"},
        )
        self.assertEqual(response.status_code, 404)

    def test_journals_pagination(self):
        url = f"/api/redmine-agent/evidence/{self.snapshot['snapshot_id']}/journals"
        first = self.client.get(url + "?limit=2", headers={"x-test-owner": "owner-a"})
        body = first.json()["data"]
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["returned"], 2)
        self.assertEqual(body["next_cursor"], "2")
        self.assertFalse(body["truncated"])
        second = self.client.get(
            url + "?limit=2&cursor=2", headers={"x-test-owner": "owner-a"}
        )
        body2 = second.json()["data"]
        self.assertEqual(body2["returned"], 1)
        self.assertIsNone(body2["next_cursor"])
        self.assertEqual(body2["journals"][0]["notes"], "third")

    def test_long_journal_notes_untruncated(self):
        url = f"/api/redmine-agent/evidence/{self.snapshot['snapshot_id']}/journals"
        response = self.client.get(url + "?limit=3", headers={"x-test-owner": "owner-a"})
        journals = response.json()["data"]["journals"]
        self.assertIn("AssertionError padding", journals[1]["notes"])
        self.assertGreater(len(journals[1]["notes"]), 2000)

    def test_artifact_text_windowing(self):
        url = f"/api/redmine-agent/artifacts/{self.artifact_id}/text"
        response = self.client.get(url + "?offset=0&limit=10", headers={"x-test-owner": "owner-a"})
        data = response.json()["data"]
        self.assertEqual(data["returned_chars"], 10)
        self.assertTrue(data["truncated"])

    def test_cross_owner_artifact_invisible(self):
        response = self.client.get(
            f"/api/redmine-agent/artifacts/{self.artifact_id}/text",
            headers={"x-test-owner": "owner-b"},
        )
        self.assertEqual(response.status_code, 404)

    def test_search_returns_evidence_refs(self):
        url = f"/api/redmine-agent/evidence/{self.snapshot['snapshot_id']}/search"
        response = self.client.get(
            url + "?q=AssertionError", headers={"x-test-owner": "owner-a"}
        )
        data = response.json()["data"]
        self.assertGreaterEqual(data["total"], 2)
        kinds = {item["kind"] for item in data["matches"]}
        self.assertIn("description", kinds)
        self.assertIn("journal", kinds)
        self.assertIn("artifact", kinds)

    def test_search_reports_incomplete_archive_index(self):
        from features.redmine.evidence_store import owner_evidence_store

        store = owner_evidence_store("owner-a")
        artifact = store.create_artifact(
            snapshot_id=self.snapshot["snapshot_id"],
            attachment_id="zip-limited",
            filename="limited.zip",
            original_filename="limited.zip",
            content_type="application/zip",
            kind="archive",
        )
        store.update_artifact(
            artifact["artifact_id"],
            status="partial",
            error="zip 文本成员数超过上限，检索索引不完整",
        )
        response = self.client.get(
            f"/api/redmine-agent/evidence/{self.snapshot['snapshot_id']}/search?q=none",
            headers={"x-test-owner": "owner-a"},
        )
        data = response.json()["data"]
        self.assertFalse(data["index_complete"])
        self.assertEqual(data["index_warnings"][0]["filename"], "limited.zip")

    def test_search_scans_stored_path_when_derived_text_missing(self):
        """空 derived_text_path 不得阻断 stored_path 扫描（真机 #648526 缺陷）。

        把 artifact 的 derived_text_path 清空：如果搜索把空候选当 break，
        stored_path 里的独有关键词就会 0 命中。
        """
        from features.redmine.evidence_store import owner_evidence_store

        store = owner_evidence_store("owner-a")
        store.update_artifact(self.artifact_id, derived_text_path="")
        url = f"/api/redmine-agent/evidence/{self.snapshot['snapshot_id']}/search"
        response = self.client.get(
            url + "?q=line%20two", headers={"x-test-owner": "owner-a"}
        )
        data = response.json()["data"]
        kinds = {item["kind"] for item in data["matches"]}
        self.assertIn("artifact", kinds)

    def test_search_cites_zip_member_with_line(self):
        """2026-09-11 反馈（zip 内容可检索）端点级：archive 派生文本命中给出 zip!/member 行级引用。"""
        from features.redmine.evidence import zip_member_marker
        from features.redmine.evidence_store import owner_evidence_store

        store = owner_evidence_store("owner-a")
        artifact = store.create_artifact(
            snapshot_id=self.snapshot["snapshot_id"],
            attachment_id="776657",
            filename="tradefed-logs.zip",
            original_filename="tradefed-logs.zip",
            content_type="application/zip",
            kind="archive",
            declared_size=128,
        )
        derived_rel = (
            f"648526/{self.snapshot['snapshot_id']}/derived/{artifact['artifact_id']}.txt"
        )
        derived_path = store.resolve_internal(derived_rel)
        derived_path.parent.mkdir(parents=True, exist_ok=True)
        derived_path.write_text(
            f"{zip_member_marker('logs/logcat.txt')}\n"
            "noise line\n"
            "get_ad_selection_data bind ok\n",
            encoding="utf-8",
        )
        store.update_artifact(
            artifact["artifact_id"],
            status="ready",
            size_bytes=128,
            sha256="c" * 64,
            stored_path=derived_rel,
            derived_text_path=derived_rel,
            detected_content_type="application/zip",
        )
        url = f"/api/redmine-agent/evidence/{self.snapshot['snapshot_id']}/search"
        response = self.client.get(
            url + "?q=get_ad_selection_data", headers={"x-test-owner": "owner-a"}
        )
        matches = response.json()["data"]["matches"]
        self.assertTrue(matches)
        entry = matches[0]
        self.assertEqual(
            entry["path"], "attachment:tradefed-logs.zip!logs/logcat.txt"
        )
        self.assertEqual(entry["line"], 2)
        self.assertEqual(entry["zip_member"], "logs/logcat.txt")

    def test_latest_endpoint_resolves_newest_snapshot_for_issue(self):
        """2026-09-11 反馈：issue-show 双参数——latest 端点按 issue 解析。"""
        from features.redmine.evidence_store import owner_evidence_store

        store = owner_evidence_store("owner-a")
        store.create_snapshot(issue_id=648526, download_policy="none")
        second = store.create_snapshot(issue_id=648526, download_policy="none")
        store.update_snapshot(
            second["snapshot_id"], status="ready", content_sha256="d" * 64
        )
        response = self.client.get(
            "/api/redmine-agent/issues/648526/evidence/latest",
            headers={"x-test-owner": "owner-a"},
        )
        data = response.json()["data"]
        self.assertEqual(data["snapshot_id"], second["snapshot_id"])
        self.assertEqual(data["status"], "ready")
        self.assertTrue(data["ready"])

    def test_latest_endpoint_404_with_fetch_hint_when_no_snapshot(self):
        response = self.client.get(
            "/api/redmine-agent/issues/999999/evidence/latest",
            headers={"x-test-owner": "owner-a"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("issue-fetch", response.json()["error"])

    def test_artifact_download_streams_original(self):
        response = self.client.get(
            f"/api/redmine-agent/artifacts/{self.artifact_id}/download",
            headers={"x-test-owner": "owner-a"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"AssertionError line one\nline two\n")
        self.assertEqual(response.headers.get("x-evidence-sha256"), "b" * 64)

    def test_create_rejects_bad_download_policy(self):
        response = self.client.post(
            "/api/redmine-agent/issues/648526/evidence",
            json={"download": "everything"},
            headers={"x-test-owner": "owner-a"},
        )
        self.assertEqual(response.status_code, 422)

    def test_create_rejects_non_numeric_issue(self):
        response = self.client.post(
            "/api/redmine-agent/issues/not-an-issue/evidence",
            json={"download": "none"},
            headers={"x-test-owner": "owner-a"},
        )
        self.assertEqual(response.status_code, 422)

    def test_create_preflight_blocks_missing_credentials_without_snapshot(self):
        """2026-09-11 反馈：凭据缺失时建快照前 4xx 快速失败，不留垃圾快照。"""
        from features.redmine.evidence_store import owner_evidence_store

        store = owner_evidence_store("owner-a")
        _owner_creds = __import__(
            "features.redmine.evidence", fromlist=["_OwnerCredentials"]
        )._OwnerCredentials
        before_ids = {
            item["snapshot_id"]
            for item in store.list_snapshots_for_issue(648526, limit=100)
        }
        with patch(
            "features.redmine.evidence.load_owner_credentials",
            lambda owner: _owner_creds(),
        ):
            response = self.client.post(
                "/api/redmine-agent/issues/648526/evidence",
                json={"download": "none", "refresh": True},
                headers={"x-test-owner": "owner-a"},
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            {
                item["snapshot_id"]
                for item in store.list_snapshots_for_issue(648526, limit=100)
            },
            before_ids,
        )

    def test_create_preflight_blocks_missing_base_url_without_snapshot(self):
        """2026-09-11 反馈：base_url 缺失同样在建快照前 4xx 快速失败。"""
        from features.redmine.evidence_store import owner_evidence_store

        store = owner_evidence_store("owner-a")
        before_ids = {
            item["snapshot_id"]
            for item in store.list_snapshots_for_issue(648526, limit=100)
        }
        with patch("features.redmine.evidence.owner_base_url", lambda owner: ""):
            response = self.client.post(
                "/api/redmine-agent/issues/648526/evidence",
                json={"download": "none", "refresh": True},
                headers={"x-test-owner": "owner-a"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            {
                item["snapshot_id"]
                for item in store.list_snapshots_for_issue(648526, limit=100)
            },
            before_ids,
        )

    def test_dry_run_probes_issue_and_does_not_create_snapshot(self):
        from features.redmine.evidence import _OwnerCredentials
        from features.redmine.evidence_fetch import EvidenceFetcher
        from features.redmine.evidence_store import owner_evidence_store

        store = owner_evidence_store("owner-a")
        before = store.latest_snapshot_for_issue(123456)
        probe = AsyncMock(return_value=None)
        with patch(
            "features.redmine.evidence.owner_base_url",
            lambda owner: "https://redmine.example",
        ), patch(
            "features.redmine.evidence.load_owner_credentials",
            lambda owner: _OwnerCredentials(api_key="k"),
        ), patch.object(EvidenceFetcher, "probe_issue", probe):
            response = self.client.post(
                "/api/redmine-agent/issues/123456/evidence",
                json={"download": "all", "dry_run": True},
                headers={"x-test-owner": "owner-a"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["preconditions"]["issue_reference_valid"])
        probe.assert_awaited_once_with(123456)
        self.assertEqual(store.latest_snapshot_for_issue(123456), before)

    def test_preflight_owner_fetch_returns_base_url_when_configured(self):
        from features.redmine.evidence import preflight_owner_fetch

        with patch(
            "features.redmine.evidence.owner_base_url",
            lambda owner: "http://redmine.example/",
        ), patch(
            "features.redmine.evidence.load_owner_credentials",
            lambda owner: __import__(
                "features.redmine.evidence", fromlist=["_OwnerCredentials"]
            )._OwnerCredentials(api_key="k"),
        ):
            self.assertEqual(
                preflight_owner_fetch("owner-a"), "http://redmine.example/"
            )


if __name__ == "__main__":
    unittest.main()
