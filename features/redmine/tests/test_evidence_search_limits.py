"""证据检索原文兜底的限流回归。

审核项：search 的原文/派生文本兜底曾无条件 read_text 整个文件，且对
非文本 kind（image/apk 等）也读原文。现在：非文本 kind 跳过原文读取，
文本读取限 1MB 前缀窗口。
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

    def test_text_read_is_capped_to_one_megabyte_prefix(self):
        result = self._search("NEEDLE-BEYOND-CAP")
        matches = result["data"]["matches"]
        self.assertTrue(all(m["kind"] == "description" for m in matches))


if __name__ == "__main__":
    unittest.main()
