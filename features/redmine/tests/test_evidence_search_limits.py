"""证据检索的限流与覆盖回归。

审核项：search 的原文/派生文本兜底曾无条件 read_text 整个文件，且对
非文本 kind（image/apk 等）也读原文。现在：非文本 kind 跳过原文读取。

#661066 复审后进一步收紧：旧实现只扫描每个文件的前 1MiB 前缀且每文件
只报首个命中。现在文本按增量块流式扫描整个文件（内存只保留滚动窗口），
报告每一次出现（≤limit），默认大小写不敏感，并如实上报覆盖情况
（coverage_complete/scanned_bytes/total_bytes）——"没搜到"和"还没搜到
的部分"是两个不同的答案。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.redmine import evidence_search_api
from features.redmine.evidence_store import EvidenceStore


class _FakeRequest:
    def __init__(self, content_type="application/json", payload=None, form_data=None):
        self.headers = {"content-type": content_type}
        self._payload = payload or {}
        self._form = form_data or {}

    async def json(self):
        return self._payload

    async def form(self):
        return self._form


class EvidenceSearchLimitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = EvidenceStore(Path(self._tmp.name))
        snapshot = self.store.create_snapshot(issue_id=648526, download_policy="all")
        self.snapshot = snapshot

        # 文本类附件：raw 原文可被兜底检索。
        self.text_artifact = self.store.create_artifact(
            snapshot_id=snapshot["snapshot_id"],
            attachment_id="9001",
            filename="logcat.txt",
            original_filename="logcat.txt",
            content_type="text/plain",
            kind="text",
        )
        self.text_rel = "648526/logcat.txt"
        self._write(self.text_rel, "boot ok\nNEEDLE-IN-RAW-TEXT\nshutdown\n")
        self.store.update_artifact(
            self.text_artifact["artifact_id"], status="ready", stored_path=self.text_rel
        )

        # 图片附件：raw 原文即使包含关键词也必须被跳过。
        self.image_artifact = self.store.create_artifact(
            snapshot_id=snapshot["snapshot_id"],
            attachment_id="9002",
            filename="screenshot.png",
            original_filename="screenshot.png",
            content_type="image/png",
            kind="image",
        )
        self.image_rel = "648526/screenshot.png"
        self._write(self.image_rel, "NEEDLE-IN-IMAGE-RAW should never match")
        self.store.update_artifact(
            self.image_artifact["artifact_id"], status="ready", stored_path=self.image_rel
        )

        # 大文本附件：命中点在 1MB 窗口之外。
        self.big_artifact = self.store.create_artifact(
            snapshot_id=snapshot["snapshot_id"],
            attachment_id="9003",
            filename="huge.log",
            original_filename="huge.log",
            content_type="text/plain",
            kind="log",
        )
        self.big_rel = "648526/huge.log"
        filler = "x" * 1024 + "\n"
        self._write(self.big_rel, filler * 1100 + "NEEDLE-BEYOND-CAP\n")
        self.store.update_artifact(
            self.big_artifact["artifact_id"], status="ready", stored_path=self.big_rel
        )

    def _write(self, rel: str, text: str) -> None:
        path = self.store.evidence_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _add_text_artifact(self, attachment_id: str, filename: str, content: str,
                           kind: str = "log"):
        rel = f"648526/{filename}"
        self._write(rel, content)
        artifact = self.store.create_artifact(
            snapshot_id=self.snapshot["snapshot_id"],
            attachment_id=attachment_id,
            filename=filename,
            original_filename=filename,
            content_type="text/plain",
            kind=kind,
        )
        self.store.update_artifact(
            artifact["artifact_id"], status="ready", stored_path=rel
        )
        return artifact

    def _search(self, needle: str):
        manifest = {"description": f"issue description {needle}-in-description", "journals": []}
        with patch.object(evidence_search_api, "require_agent_scope", lambda _scope: lambda _request: None), \
                patch.object(evidence_search_api, "_owner", return_value="owner-search-test"), \
                patch.object(evidence_search_api, "_get_snapshot", return_value={**self.snapshot, "manifest": manifest}), \
                patch.object(evidence_search_api, "owner_evidence_store", lambda _owner: self.store):
            return asyncio.run(
                evidence_search_api.search_evidence(
                    self.snapshot["snapshot_id"], request=_FakeRequest(), q=needle, limit=10
                )
            )

    def test_text_kind_raw_fallback_still_matches(self):
        result = self._search("NEEDLE-IN-RAW-TEXT")
        self.assertTrue(result["success"])
        paths = [m["kind"] for m in result["data"]["matches"]]
        self.assertIn("artifact", paths)

    def test_non_text_kind_raw_fallback_is_skipped(self):
        result = self._search("NEEDLE-IN-IMAGE-RAW")
        matches = result["data"]["matches"]
        # 描述命中保留；image 原文不得参与匹配。
        self.assertTrue(all(m["kind"] == "description" for m in matches))

    def test_match_beyond_one_megabyte_is_found(self):
        # 回归（#661066）：旧实现只扫前 1MiB，日志尾部的异常完全搜不到。
        result = self._search("NEEDLE-BEYOND-CAP")
        kinds = [m["kind"] for m in result["data"]["matches"]]
        self.assertIn("artifact", kinds)
        self.assertTrue(result["data"]["coverage_complete"])
        self.assertGreater(result["data"]["total_bytes"], 1024 * 1024)
        self.assertEqual(
            result["data"]["scanned_bytes"], result["data"]["total_bytes"]
        )

    def test_all_occurrences_are_reported(self):
        # 回归：旧实现每文件只报首个命中，多次异常看不见。
        self._add_text_artifact("9101", "multi.log", "boom\nfirst\nboom\nsecond\nboom\nlast\n")
        result = self._search("boom")
        artifacts = [m for m in result["data"]["matches"] if m["kind"] == "artifact"]
        self.assertEqual(len(artifacts), 3)
        self.assertEqual([m["char_index"] for m in artifacts], [0, 11, 23])

    def test_case_insensitive_by_default(self):
        self._add_text_artifact("9102", "case.log", "boot ok\nKernel panic: LmKd killed process\n")
        result = self._search("lmkd killed")
        artifacts = [m for m in result["data"]["matches"] if m["kind"] == "artifact"]
        self.assertEqual(len(artifacts), 1)
        # snippet 保留原文大小写。
        self.assertIn("LmKd killed process", artifacts[0]["snippet"])

    def test_limit_boundary_reports_limited(self):
        self._add_text_artifact("9103", "limit.log", "hit\n" * 8)
        result = self._search("hit")
        data = result["data"]
        self.assertEqual(len(data["matches"]), 9)  # 8 artifact + 1 description
        self.assertFalse(data["limited"])
        self._add_text_artifact("9104", "limit2.log", "hit\n" * 12)
        result = self._search("hit")
        data = result["data"]
        self.assertEqual(len(data["matches"]), 10)
        self.assertTrue(data["limited"])


if __name__ == "__main__":
    unittest.main()
