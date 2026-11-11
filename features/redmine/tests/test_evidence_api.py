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

    def test_numeric_attachment_id_resolves_to_artifact(self):
        # 模型偶尔把 Redmine 数字附件 ID 当 art_* 证据 ID 传入；服务端
        # 解析别名（2026-09-24 晨报批次真实用例），跨 owner 仍不可见。
        url = "/api/redmine-agent/artifacts/776656/text?offset=0&limit=10"
        ok = self.client.get(url, headers={"x-test-owner": "owner-a"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["data"]["returned_chars"], 10)
        cross = self.client.get(url, headers={"x-test-owner": "owner-b"})
        self.assertEqual(cross.status_code, 404)

    def test_unknown_numeric_attachment_id_error_is_actionable(self):
        url = "/api/redmine-agent/artifacts/999999/text"
        response = self.client.get(url, headers={"x-test-owner": "owner-a"})
        self.assertEqual(response.status_code, 404)
        self.assertIn("gms-rt-redmine-attachments", response.json()["error"])

    def test_nonexistent_artifact_id_still_404(self):
        url = "/api/redmine-agent/artifacts/art_does_not_exist/text"
        response = self.client.get(url, headers={"x-test-owner": "owner-a"})
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
            status="ready",
            derived_status="partial",
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
        """zip 内容可检索（端点级）：archive 派生文本命中给出 zip!/member 行级引用。"""
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

    def test_search_requires_artifacts_read_own_for_agent_tokens(self):
        """搜索返回原文片段，与 artifact 读取端点同界。

        只带 redmine.read 的 Agent 不能再借搜索逐窗口提取无权读取的
        附件内容；两个 scope 都带或人类会话（user 角色）照常放行。
        """
        url = f"/api/redmine-agent/evidence/{self.snapshot['snapshot_id']}/search"
        denied = self.client.get(
            url + "?q=AssertionError",
            headers={"x-test-owner": "owner-a",
                     "x-test-scopes": "redmine.read"},
        )
        self.assertEqual(denied.status_code, 403)
        allowed = self.client.get(
            url + "?q=AssertionError",
            headers={"x-test-owner": "owner-a",
                     "x-test-scopes": "redmine.read,artifacts.read_own"},
        )
        self.assertEqual(allowed.status_code, 200)

    def test_artifact_text_window_keeps_total_chars_semantics(self):
        """流式窗口读取：窗口内容正确、窗口填满即停止读取（不为统计
        扫完整文件）；total_chars=-1 表示后续内容未计数，truncated 如实。"""
        url = f"/api/redmine-agent/artifacts/{self.artifact_id}/text"
        payload = b"AssertionError line one\nline two\n"
        first = self.client.get(url + "?offset=0&limit=10", headers={"x-test-owner": "owner-a"})
        data = first.json()["data"]
        self.assertEqual(data["text"], payload[:10].decode("utf-8"))
        self.assertEqual(data["total_chars"], -1)  # 窗口填满后提前停止计数
        self.assertTrue(data["truncated"])
        second = self.client.get(url + "?offset=10&limit=100", headers={"x-test-owner": "owner-a"})
        data = second.json()["data"]
        self.assertEqual(data["text"], payload[10:].decode("utf-8"))
        self.assertEqual(data["total_chars"], len(payload.decode("utf-8")))
        self.assertFalse(data["truncated"])

    def test_artifact_text_exactly_window_length_is_not_truncated(self):
        """审核 P2 回归：文本恰好等于 offset+limit 时不得误报 truncated。

        窗口读满即停的优化在"恰好收尾"时无法区分读完与还有后续，必须
        多探测一个字符；否则客户端按 truncated 翻页只会拿到空窗口。
        """
        from features.redmine.evidence_store import owner_evidence_store

        store = owner_evidence_store("owner-a")
        artifact = store.create_artifact(
            snapshot_id=self.snapshot["snapshot_id"],
            attachment_id="776659",
            filename="exact.log",
            original_filename="exact.log",
            content_type="text/plain",
            kind="log",
        )
        rel = f"648526/{self.snapshot['snapshot_id']}/attachments/{artifact['artifact_id']}/exact.log"
        target = store.resolve_internal(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(("x" * 20).encode("utf-8"))
        store.update_artifact(artifact["artifact_id"], status="ready", stored_path=rel)
        url = f"/api/redmine-agent/artifacts/{artifact['artifact_id']}/text"

        page = self.client.get(url + "?offset=0&limit=20", headers={"x-test-owner": "owner-a"})
        data = page.json()["data"]
        self.assertEqual(data["text"], "x" * 20)
        self.assertEqual(data["total_chars"], 20)  # 读完恰好收尾：精确计数
        self.assertFalse(data["truncated"])
        self.assertEqual(data["returned_chars"], 20)

        # 有第 21 个字符时同样窗口必须如实报 truncated
        target.write_bytes(("x" * 21).encode("utf-8"))
        page = self.client.get(url + "?offset=0&limit=20", headers={"x-test-owner": "owner-a"})
        data = page.json()["data"]
        self.assertEqual(data["text"], "x" * 20)
        self.assertTrue(data["truncated"])

    def test_artifact_text_falls_back_to_gb18030(self):
        """utf-8 失败回退 gb18030（replace），中文日志可读。"""
        from features.redmine.evidence_store import owner_evidence_store

        store = owner_evidence_store("owner-a")
        artifact = store.create_artifact(
            snapshot_id=self.snapshot["snapshot_id"],
            attachment_id="776658",
            filename="gblog.log",
            original_filename="gblog.log",
            content_type="text/plain",
            kind="log",
        )
        rel = f"648526/{self.snapshot['snapshot_id']}/attachments/{artifact['artifact_id']}/gblog.log"
        target = store.resolve_internal(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes("进程死亡 PID=1234".encode("gb18030"))
        store.update_artifact(
            artifact["artifact_id"], status="ready", stored_path=rel
        )
        response = self.client.get(
            f"/api/redmine-agent/artifacts/{artifact['artifact_id']}/text",
            headers={"x-test-owner": "owner-a"},
        )
        data = response.json()["data"]
        self.assertIn("进程死亡", data["text"])
        self.assertEqual(data["total_chars"], len("进程死亡 PID=1234"))

    def test_search_covers_zip_members_beyond_one_megabyte(self):
        """回归：v2 成员帧逐成员全文匹配，成员体越过旧
        1MiB 前缀窗口的命中同样可见。"""
        from features.redmine.evidence import zip_member_block
        from features.redmine.evidence_store import owner_evidence_store

        store = owner_evidence_store("owner-a")
        artifact = store.create_artifact(
            snapshot_id=self.snapshot["snapshot_id"],
            attachment_id="776659",
            filename="big-logs.zip",
            original_filename="big-logs.zip",
            content_type="application/zip",
            kind="archive",
        )
        derived_rel = (
            f"648526/{self.snapshot['snapshot_id']}/derived/{artifact['artifact_id']}.txt"
        )
        derived_path = store.resolve_internal(derived_rel)
        derived_path.parent.mkdir(parents=True, exist_ok=True)
        big_member = "x" * (1024 * 1024 + 128) + "process died at the very end\n"
        derived_path.write_text(
            zip_member_block("logs/logcat.txt", big_member),
            encoding="utf-8",
        )
        store.update_artifact(
            artifact["artifact_id"],
            status="ready",
            stored_path=derived_rel,
            derived_text_path=derived_rel,
            detected_content_type="application/zip",
        )
        url = f"/api/redmine-agent/evidence/{self.snapshot['snapshot_id']}/search"
        response = self.client.get(
            url + "?q=process%20died%20at%20the%20very%20end",
            headers={"x-test-owner": "owner-a"},
        )
        data = response.json()["data"]
        self.assertTrue(data["coverage_complete"])
        matches = [
            m for m in data["matches"] if m.get("zip_member") == "logs/logcat.txt"
        ]
        self.assertTrue(matches)
        self.assertIn("process died at the very end", matches[0]["snippet"])

    def test_batch_search_matches_per_query_and_reports_coverage(self):
        """批量端点：一次请求多关键词各自命中、独立截断、覆盖诚实。"""
        url = f"/api/redmine-agent/evidence/{self.snapshot['snapshot_id']}/search/batch"
        response = self.client.post(
            url,
            json={"queries": ["AssertionError", "line two", "nomatch"],
                  "per_query_limit": 5},
            headers={"x-test-owner": "owner-a"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        by_query = {row["query"]: row for row in data["queries"]}
        self.assertGreaterEqual(by_query["AssertionError"]["total"], 3)
        self.assertGreaterEqual(by_query["line two"]["total"], 1)
        self.assertFalse(by_query["nomatch"]["truncated"])
        self.assertTrue(data["coverage_complete"])
        self.assertGreaterEqual(data["scanned_bytes"], 1)
        self.assertEqual(data["scanned_bytes"], data["total_bytes"])
        # fixture 的 journal notes 含数百个 AssertionError（预算被耗尽
        # 属预期截断）；artifact 命中用 line two 断言。
        self.assertLessEqual(len(by_query["AssertionError"]["matches"]), 5)
        self.assertTrue(by_query["AssertionError"]["truncated"])
        kinds = {m["kind"] for m in by_query["line two"]["matches"]}
        self.assertIn("artifact", kinds)

    def test_batch_search_requires_artifacts_scope_and_validates_body(self):
        url = f"/api/redmine-agent/evidence/{self.snapshot['snapshot_id']}/search/batch"
        denied = self.client.post(
            url,
            json={"queries": ["AssertionError"]},
            headers={"x-test-owner": "owner-a", "x-test-scopes": "redmine.read"},
        )
        self.assertEqual(denied.status_code, 403)
        empty = self.client.post(
            url,
            json={"queries": ["   "]},
            headers={"x-test-owner": "owner-a"},
        )
        self.assertEqual(empty.status_code, 422)
        unknown = self.client.post(
            url,
            json={"queries": ["a"], "bogus": True},
            headers={"x-test-owner": "owner-a"},
        )
        self.assertEqual(unknown.status_code, 422)

    def test_latest_endpoint_resolves_newest_snapshot_for_issue(self):
        """issue-show 双参数——latest 端点按 issue 解析。"""
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
        """凭据缺失时建快照前 4xx 快速失败，不留垃圾快照。"""
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
        """base_url 缺失同样在建快照前 4xx 快速失败。"""
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
