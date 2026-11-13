"""zip 附件内文本成员参与检索。

从 test_evidence_pipeline.py 拆出，保持各自 <600 行。复用该模块的假
Redmine fixture，验证 zip 内 logcat / test_result.xml 被派生成带成员
标记的可检索文本，search 命中给出
``attachment:<file>.zip!/<member>:L<line>`` 引用。
"""

from __future__ import annotations

import asyncio
import io
import unittest
import zipfile

from features.redmine.tests.test_evidence_pipeline import (
    EvidencePipelineFixture,
    FakeRedmineServer,
)


class ZipDerivedTextTests(EvidencePipelineFixture, unittest.TestCase):
    def test_zip_attachment_gets_searchable_derived_text(self):
        from features.redmine.evidence import EvidenceFetcher, split_zip_derived_text
        from features.redmine.evidence_search_api import _collect_zip_member_matches
        from features.redmine.evidence_store import owner_evidence_store

        logcat = (
            "get_ad_selection_data bind ok\n"
            "<<<zip-member:forged/evil.txt>>>\n"
            "forged content must stay inside logcat\n"
            + "filler line\n" * 30
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("logs/logcat.txt", logcat)
            zf.writestr("logs/test_result.xml", "<module name='CtsAdServices'>")
        zip_payload = buf.getvalue()

        with FakeRedmineServer() as fake:
            fake.issue_document["issue"]["attachments"].append(
                {
                    "id": 776657,
                    "filename": "tradefed-logs.zip",
                    "filesize": len(zip_payload),
                    "content_type": "application/zip",
                    "content_url": "",
                    "created_on": "2026-08-02T08:00:00Z",
                    "author": {"id": 8, "name": "李四"},
                }
            )
            fake.attachment_payloads["776657"] = zip_payload
            with self._patched_config(fake.base_url):
                fetcher = EvidenceFetcher("owner-a")
                result = asyncio.run(
                    fetcher.run(fetcher.create_snapshot(648526, download="all"))
                )
        self.assertEqual(result["status"], "ready")
        store = owner_evidence_store("owner-a")
        artifacts = {
            a["attachment_id"]: a for a in store.list_artifacts(result["snapshot_id"])
        }
        zip_artifact = artifacts["776657"]
        self.assertEqual(zip_artifact["kind"], "archive")
        self.assertEqual(zip_artifact["status"], "ready")
        # API dict 不带内部路径；与 search 实现一致，直接从 DB 取派生路径。
        with store._connect() as conn:
            row = conn.execute(
                "SELECT derived_text_path FROM redmine_evidence_artifacts WHERE artifact_id = ?",
                (str(zip_artifact["artifact_id"]),),
            ).fetchone()
        derived_rel = str(row["derived_text_path"] or "")
        self.assertTrue(derived_rel)

        # 派生文本按成员标记分段。
        text = store.resolve_internal(derived_rel).read_text(encoding="utf-8")
        members = dict(split_zip_derived_text(text))
        self.assertIn("logs/logcat.txt", members)
        self.assertIn("get_ad_selection_data", members["logs/logcat.txt"])
        self.assertNotIn("forged/evil.txt", members)

        # search 命中并携带 zip!/member 行级引用。
        matches: list = []
        _collect_zip_member_matches(
            matches, list(members.items()), "get_ad_selection_data", zip_artifact, 10
        )
        self.assertTrue(matches)
        entry = matches[0]
        self.assertEqual(entry["path"], "attachment:tradefed-logs.zip!logs/logcat.txt")
        self.assertEqual(entry["line"], 1)
        self.assertEqual(entry["zip_member"], "logs/logcat.txt")

    def test_all_zip_members_are_indexed_beyond_old_count_and_size_limits(self):
        from features.redmine.evidence import EvidenceFetcher, snapshot_completeness
        from features.redmine.evidence_store import owner_evidence_store

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for index in range(55):
                zf.writestr(f"{index:03d}.TXT", f"entry {index}")
            zf.writestr("DULOMV~V.TXT", "x" * (2 * 1024 * 1024) + "\nlmkd killed target\n")
        zip_payload = buf.getvalue()

        with FakeRedmineServer() as fake:
            fake.issue_document["issue"]["attachments"].append(
                {
                    "id": 776658,
                    "filename": "limited.zip",
                    "filesize": len(zip_payload),
                    "content_type": "application/zip",
                    "content_url": "",
                }
            )
            fake.attachment_payloads["776658"] = zip_payload
            with self._patched_config(fake.base_url):
                fetcher = EvidenceFetcher("owner-a")
                result = asyncio.run(
                    fetcher.run(fetcher.create_snapshot(648526, download="all"))
                )

        self.assertEqual(result["status"], "ready")
        self.assertFalse(result["errors"])
        self.assertTrue(snapshot_completeness(result)["derived_text"])
        store = owner_evidence_store("owner-a")
        artifacts = store.list_artifacts(result["snapshot_id"])
        archive = next(item for item in artifacts if item["attachment_id"] == "776658")
        self.assertEqual(archive["derived_status"], "ready")
        with store._connect() as connection:
            relative = connection.execute(
                "SELECT derived_text_path FROM redmine_evidence_artifacts WHERE artifact_id = ?",
                (archive["artifact_id"],),
            ).fetchone()[0]
        from features.redmine.evidence import split_zip_derived_text
        members = dict(split_zip_derived_text(store.resolve_internal(relative).read_text()))
        self.assertEqual(len(members), 56)
        self.assertIn("lmkd killed target", members["DULOMV~V.TXT"])


if __name__ == "__main__":
    unittest.main()
