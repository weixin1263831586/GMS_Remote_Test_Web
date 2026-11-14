"""证据读取/检索优化回归（2026-10 Redmine 660451 分析反馈）。

覆盖：
- zip 派生索引 sidecar（成员全局 char 区间 + total_chars）与
  artifact_read 的成员注记（zip_member / member_char_index / range）；
- artifact_read 负 offset（尾部计数）与超界空窗口（不报错）；
- search 的 artifact/member 过滤、collapse 折叠、--context、
  per-artifact 命中摘要（artifact_hits / total_occurrences）；
- 失败用例表解析器（test_result.xml / 汇总 HTML / zip 兜底）；
- history FTS 的测试名短语匹配。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.redmine import evidence_api, evidence_search_api
from features.redmine.evidence_search_api import COLLAPSE_GAP_CHARS
from features.redmine.evidence_text import derive_member_index
from features.redmine.repository_storage import RepositoryStorageMixin


class _EvidenceOptimizeTests(unittest.TestCase):
    """共用 fixture：owner-a + 一个 zip 归档 artifact（带派生索引）。"""

    def setUp(self):
        self.secret_env = unittest.mock.patch.dict(
            "os.environ", {"GMS_SECRET_KEY": Fernet.generate_key().decode("ascii")}
        )
        self.secret_env.start()
        self.addCleanup(self.secret_env.stop)
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

        import features.redmine.evidence_store as evidence_store
        import features.redmine.users as redmine_users

        users_patch = unittest.mock.patch.object(
            redmine_users,
            "owner_redmine_root",
            lambda owner: self.root / "owners" / str(owner),
        )
        users_patch.start()
        self.addCleanup(users_patch.stop)
        evidence_store._STORE_CACHE.clear()
        self.addCleanup(evidence_store._STORE_CACHE.clear)

        from features.redmine.evidence_store import owner_evidence_store

        store = owner_evidence_store("owner-a")
        self.snapshot = store.create_snapshot(issue_id=660451, download_policy="all")
        store.update_snapshot(
            self.snapshot["snapshot_id"],
            status="ready",
            fetched_at="2026-10-01T00:00:00",
            content_sha256="c" * 64,
            manifest_json={"issue_id": 660451, "subject": "s", "description": "", "journals": []},
            journal_count=0,
            attachment_count=1,
            downloaded_count=1,
            error_json=[],
        )
        artifact = store.create_artifact(
            snapshot_id=self.snapshot["snapshot_id"],
            attachment_id="880001",
            filename="tradefed-logs.zip",
            original_filename="tradefed-logs.zip",
            content_type="application/zip",
            kind="archive",
            declared_size=4096,
        )
        rel = (
            f"660451/{self.snapshot['snapshot_id']}/attachments/"
            f"{artifact['artifact_id']}/tradefed-logs.zip"
        )
        self.artifact_id = artifact["artifact_id"]
        member_texts = {
            "logs/test_result.xml": "<Module name='CtsSandboxSdkTests'><Test name='t' result='fail'/></Module>",
            "logs/logcat-main.log": "log line one\nlog line two\n" + "filler line\n" * 50,
        }
        # 写原始 zip（failures 端点从原文分派解析）+ 派生索引（同时产出
        # sidecar）。
        import io as _io
        import zipfile as _zf

        buf = _io.BytesIO()
        with _zf.ZipFile(buf, "w") as z:
            for name, text in member_texts.items():
                z.writestr(name, text)
        zip_target = store.resolve_internal(rel)
        zip_target.parent.mkdir(parents=True, exist_ok=True)
        zip_target.write_bytes(buf.getvalue())
        members = [
            (name, (lambda text=text: _io.BytesIO(text.encode("utf-8"))))
            for name, text in member_texts.items()
        ]
        derived_rel, errors = derive_member_index(
            store, self.artifact_id, f"660451/{self.snapshot['snapshot_id']}", members
        )
        self.assertFalse(errors)
        store.update_artifact(
            self.artifact_id,
            status="ready",
            size_bytes=4096,
            sha256="d" * 64,
            stored_path=rel,
            derived_text_path=derived_rel,
        )
        self.derived_path = store.resolve_internal(derived_rel)

        app = FastAPI()

        @app.middleware("http")
        async def authenticate(request, call_next):
            request.state.current_user = CurrentUser(
                id="owner-a", username="owner-a", role="agent_service",
                extra_permissions=frozenset({"redmine.read", "artifacts.read_own"}),
            )
            request.state.auth_method = "agent_token"
            return await call_next(request)

        app.include_router(evidence_api.router)
        app.include_router(evidence_search_api.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    # ------------------------------------------------- zip sidecar / read

    def test_zip_sidecar_written_with_member_ranges(self):
        sidecar = self.derived_path.parent / f"{self.derived_path.name}.idx.json"
        self.assertTrue(sidecar.is_file())
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 1)
        names = [m["name"] for m in payload["members"]]
        self.assertEqual(names, ["logs/test_result.xml", "logs/logcat-main.log"])
        first, second = payload["members"]
        # 成员区间单调且落在 [start, start+length)。
        self.assertLess(first["start"], second["start"])
        # total_chars 覆盖到最后一成员正文 + 结尾分隔换行。
        self.assertEqual(
            payload["total_chars"], second["start"] + second["length"] + 1
        )

    def test_read_annotates_zip_member_for_window(self):
        sidecar = json.loads(
            (self.derived_path.parent / f"{self.derived_path.name}.idx.json").read_text("utf-8")
        )
        member = sidecar["members"][1]  # logcat-main.log
        url = f"/api/redmine-agent/artifacts/{self.artifact_id}/text"
        resp = self.client.get(url, params={"offset": member["start"], "limit": 10})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["zip_member"], "logs/logcat-main.log")
        self.assertEqual(data["member_char_index"], 0)
        self.assertEqual(
            data["member_char_range"], [member["start"], member["start"] + member["length"]]
        )
        # sidecar 补全 total_chars（不再依赖整文件扫描）。
        self.assertEqual(data["total_chars"], sidecar["total_chars"])
        self.assertIn("log line", data["text"])

    def test_read_negative_offset_counts_from_tail(self):
        url = f"/api/redmine-agent/artifacts/{self.artifact_id}/text"
        resp = self.client.get(url, params={"offset": -10, "limit": 100})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["requested_offset"], -10)
        self.assertEqual(data["returned_chars"], 10)
        # 尾读给出精确 total_chars 与绝对 offset。
        self.assertEqual(
            data["offset"], data["total_chars"] - 10
        )
        self.assertFalse(data["truncated"])

    def test_read_beyond_eof_returns_empty_window(self):
        url = f"/api/redmine-agent/artifacts/{self.artifact_id}/text"
        resp = self.client.get(url, params={"offset": 10**9, "limit": 100})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["text"], "")
        self.assertEqual(data["returned_chars"], 0)

    # ------------------------------------------------- failures endpoint

    def test_failures_from_zip_test_result(self):
        url = f"/api/redmine-agent/artifacts/{self.artifact_id}/failures"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["total"], 1)
        row = data["failures"][0]
        self.assertEqual(row["module"], "CtsSandboxSdkTests")
        self.assertEqual(row["test"], "t")
        self.assertIn("source", row)

    def test_failures_from_summary_html(self):

        from features.redmine.evidence_api import _failures_from_summary_html

        html = (
            "<html><body><table>"
            "<tr><td>CtsAdServicesDevicesTests</td><td>android.adservices.foo</td><td>FAIL</td></tr>"
            "<tr><td>CtsAdServicesDevicesTests</td><td>android.adservices.foo</td><td>FAIL</td></tr>"
            "</table></body></html>"
        )
        rows = _failures_from_summary_html(html, "STS-summary.html")
        self.assertEqual(len(rows), 1)  # 同 (module, test) 去重
        self.assertEqual(rows[0]["test"], "android.adservices.foo")

    def test_failures_rejects_non_report(self):
        from features.redmine.evidence_api import _extract_failure_rows

        rows, parser = _extract_failure_rows(
            {"filename": "notes.txt", "kind": "text"},
            Path(self.directory.name) / "whatever.txt",
        )
        self.assertEqual(rows, [])
        self.assertEqual(parser, "skip")

    # ------------------------------------------------- search extras

    def _store(self):
        from features.redmine.evidence_store import owner_evidence_store

        return owner_evidence_store("owner-a")

    def test_search_collapse_folds_and_counts_raw(self):

        from features.redmine.evidence_store import owner_evidence_store

        store = owner_evidence_store("owner-a")
        artifact = store.create_artifact(
            snapshot_id=self.snapshot["snapshot_id"],
            attachment_id="880002",
            filename="repeat.log",
            original_filename="repeat.log",
            content_type="text/plain",
            kind="log",
            declared_size=256,
        )
        rel = f"660451/{self.snapshot['snapshot_id']}/attachments/{artifact['artifact_id']}/repeat.log"
        payload = "\n".join(f"LoadSdkException here {i}" for i in range(6))
        store.resolve_internal(rel).parent.mkdir(parents=True, exist_ok=True)
        store.resolve_internal(rel).write_bytes(
            payload.encode("utf-8")
        )
        store.update_artifact(
            artifact["artifact_id"], status="ready",
            size_bytes=len(payload), sha256="e" * 64, stored_path=rel,
        )

        snapshot = self._store().get_snapshot(self.snapshot["snapshot_id"])
        plain = evidence_search_api._search_all_evidence(
            store, snapshot, ["LoadSdkException"], 50, 0
        )
        self.assertEqual(plain["queries"][0]["total"], 6)
        self.assertEqual(len(plain["queries"][0]["matches"]), 6)

        folded = evidence_search_api._search_all_evidence(
            store, snapshot, ["LoadSdkException"], 50, 0, collapse=True
        )
        # 相邻命中折叠成一条，但 raw total 与 per-artifact 计数不变。
        self.assertEqual(folded["queries"][0]["total"], 6)
        self.assertEqual(len(folded["queries"][0]["matches"]), 1)
        hit = {h["artifact_id"]: h["hits"] for h in folded["artifact_hits"]}
        self.assertEqual(hit[artifact["artifact_id"]], 6)
        self.assertEqual(folded["total_occurrences"], 6)

    def test_search_collapse_pagination_always_advances(self):
        """折叠后翻页不留空洞：next_offset 恒前进、页并集覆盖全部展示条目。

        回归：旧实现按 raw 序号开窗，页内命中全被折叠时返回 0 条且
        next_offset 不动，调用方会无限重复同一页。
        """
        store = self._store()
        artifact = store.create_artifact(
            snapshot_id=self.snapshot["snapshot_id"],
            attachment_id="880003",
            filename="dense.log",
            original_filename="dense.log",
            content_type="text/plain",
            kind="log",
            declared_size=512,
        )
        rel = f"660451/{self.snapshot['snapshot_id']}/attachments/{artifact['artifact_id']}/dense.log"
        # 6 组相邻命中（每组 3 条密集重复）+ 远端 1 条独立命中。
        dense = "\n".join(f"SameStack group{i} line{j}" for i in range(2)
                          for j in range(3))
        payload = dense + "\n" + "x" * (5 * COLLAPSE_GAP_CHARS) + "\nFarStack tail\n"
        store.resolve_internal(rel).parent.mkdir(parents=True, exist_ok=True)
        store.resolve_internal(rel).write_bytes(payload.encode("utf-8"))
        store.update_artifact(
            artifact["artifact_id"], status="ready",
            size_bytes=len(payload), sha256="f" * 64, stored_path=rel,
        )
        snapshot = store.get_snapshot(self.snapshot["snapshot_id"])

        collected = []
        offset, next_offset = 0, 0
        while next_offset is not None:
            page = evidence_search_api._search_all_evidence(
                store, snapshot, ["SameStack"], 1, offset, collapse=True,
            )
            row = page["queries"][0]
            self.assertEqual(row["offset"], offset)
            self.assertTrue(
                all(hit is not None for hit in row["matches"]),
                "折叠页绝不允许出现空洞条目",
            )
            collected.extend(row["matches"])
            next_offset = row["next_offset"]
            self.assertNotEqual(
                next_offset, offset,
                "next_offset 停滞：调用方将无限重复同一页",
            )
            offset = next_offset
        # 展示条目全部可达：翻页收集数 == 全量 display 总数。
        full = evidence_search_api._search_all_evidence(
            store, snapshot, ["SameStack"], 50, 0, collapse=True,
        )["queries"][0]
        self.assertGreater(len(collected), 0)
        self.assertLess(len(collected), full["total"])
        self.assertEqual(len(collected), full["display"])

    def test_search_artifact_filter_and_context(self):
        store = self._store()
        snapshot = store.get_snapshot(self.snapshot["snapshot_id"])
        filtered = evidence_search_api._search_all_evidence(
            store, snapshot, ["log line"], 50, 0,
            artifact_filter="tradefed-logs",
        )
        self.assertEqual(filtered["queries"][0]["total"], 2)
        self.assertTrue(filtered["filters"]["artifact"])

        # 不匹配任何 artifact 的过滤器 → 零命中且不扫描。
        none = evidence_search_api._search_all_evidence(
            store, snapshot, ["log line"], 50, 0, artifact_filter="no-such-attachment",
        )
        self.assertEqual(none["queries"][0]["total"], 0)

        # member 过滤：zip 成员名子串。
        member_only = evidence_search_api._search_all_evidence(
            store, snapshot, ["log line"], 50, 0, member_filter="logcat-main",
        )
        self.assertEqual(member_only["queries"][0]["total"], 2)
        entry = member_only["queries"][0]["matches"][0]
        self.assertEqual(entry["zip_member"], "logs/logcat-main.log")
        self.assertIn("member_char_index", entry)
        self.assertIn("artifact_char_index", entry)

        wide = evidence_search_api._search_all_evidence(
            store, snapshot, ["log line"], 50, 0, context=600,
        )
        default = evidence_search_api._search_all_evidence(
            store, snapshot, ["log line"], 50, 0,
        )
        self.assertGreater(
            len(wide["queries"][0]["matches"][0]["snippet"]),
            len(default["queries"][0]["matches"][0]["snippet"]),
        )
        self.assertGreaterEqual(len(wide["queries"][0]["matches"][0]["snippet"]), 600)


class HistoryFtsQueryTests(unittest.TestCase):
    """测试名（下划线/驼峰）在 FTS 查询里转为短语匹配。"""

    def test_test_name_becomes_phrase(self):
        query = RepositoryStorageMixin._fts_query(
            "testSdkSandbox_whenBackground_blocked LoadSdkException"
        )
        self.assertIn('"whenBackground"', query)
        # 短语：三段连在一起；普通词仍是单独 OR 项。
        self.assertIn('"testSdkSandbox" "whenBackground" "blocked"', query)
        self.assertIn('OR "LoadSdkException"', query)

    def test_plain_query_unchanged(self):
        query = RepositoryStorageMixin._fts_query("camera bind failure")
        self.assertEqual(query, '"camera" OR "bind" OR "failure"')

    def test_fts_match_prefers_real_test_name(self):
        import sqlite3

        query = RepositoryStorageMixin._fts_query(
            "testSdkSandbox_whenBackground_blocked"
        )
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(doc)")
        conn.execute(
            "INSERT INTO t VALUES (?)",
            ("CtsSandboxSdkTests failed testSdkSandbox_whenBackground_blocked",),
        )
        conn.execute("INSERT INTO t VALUES (?)", ("generic doc with blocked keyword",))
        hits = [row[0] for row in conn.execute("SELECT doc FROM t WHERE t MATCH ?", (query,))]
        self.assertEqual(len(hits), 1)
        self.assertIn("testSdkSandbox", hits[0])


if __name__ == "__main__":
    unittest.main()
