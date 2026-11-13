"""Search budgets, overlapping terms and coverage across stream boundaries."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.redmine import evidence_search_api as api
from features.redmine.evidence import zip_member_block
from features.redmine.evidence_store import EvidenceStore


class BatchEvidenceSearchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = EvidenceStore(Path(tmp.name))
        self.snapshot = self.store.create_snapshot(issue_id=1, download_policy="all")
        self.manifest = {}

    def _artifact(self, text, *, archive=False):
        filename = "logs.zip" if archive else "case.log"
        artifact = self.store.create_artifact(
            snapshot_id=self.snapshot["snapshot_id"], attachment_id="1",
            filename=filename, original_filename=filename,
            content_type="application/zip" if archive else "text/plain",
            kind="archive" if archive else "log",
        )
        rel = f"{artifact['artifact_id']}.txt"
        path = self.store.resolve_internal(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.store.update_artifact(
            artifact["artifact_id"], status="ready", stored_path=rel,
            derived_text_path=rel if archive else "",
        )

    def _batch(self, queries, limit=1, offset=0):
        with patch.object(api, "require_agent_scope", lambda scope: lambda request: None), \
                patch.object(api, "_owner", return_value="test-owner"), \
                patch.object(api, "_get_snapshot", return_value={**self.snapshot, "manifest": self.manifest}), \
                patch.object(api, "owner_evidence_store", return_value=self.store):
            result = asyncio.run(api.search_evidence_batch(
                self.snapshot["snapshot_id"], api.BatchSearchRequest(queries=queries, per_query_limit=limit, per_query_offset=offset), object(),
            ))
        return result["data"]

    def test_frequent_term_does_not_hide_rare_term_in_same_chunk(self):
        self._artifact("frequent\n" * 50 + "rare\n")
        data = self._batch(["frequent", "rare"])
        rows = {row["query"]: row for row in data["queries"]}
        self.assertTrue(rows["frequent"]["truncated"])
        self.assertEqual(rows["rare"]["total"], 1)
        self.assertIn("rare", rows["rare"]["matches"][0]["snippet"])
        self.assertTrue(data["coverage_complete"])

    def test_rare_term_is_found_after_multiple_chunks(self):
        self._artifact("frequent\n" * 100000 + "rare\n")
        data = self._batch(["frequent", "rare", "absent"])
        rows = {row["query"]: row for row in data["queries"]}
        self.assertEqual(rows["rare"]["total"], 1)
        self.assertEqual(rows["absent"]["total"], 0)
        self.assertTrue(data["coverage_complete"])
        self.assertEqual(data["scanned_bytes"], data["total_bytes"])

    def test_overlapping_and_case_equivalent_queries_are_independent(self):
        self._artifact("error: closed\n")
        data = self._batch(["error", "error: closed", "ERROR", "closed"])
        self.assertTrue(all(row["total"] == 1 for row in data["queries"]))
        self.assertTrue(data["index_complete"])

    def test_manifest_uses_independent_budgets_and_overlap_matching(self):
        self.manifest = {"description": "error: closed\n" * 50 + "rare\n"}
        data = self._batch(["error", "error: closed", "rare"])
        rows = {row["query"]: row for row in data["queries"]}
        self.assertTrue(rows["error"]["truncated"])
        self.assertTrue(rows["error: closed"]["truncated"])
        self.assertEqual(rows["rare"]["total"], 1)

    def test_zip_members_share_remaining_query_budgets(self):
        self._artifact(
            zip_member_block("first.log", "frequent\n" * 50)
            + zip_member_block("second.log", "rare\n"),
            archive=True,
        )
        data = self._batch(["frequent", "rare"])
        rare = next(row for row in data["queries"] if row["query"] == "rare")
        self.assertEqual(rare["matches"][0]["zip_member"], "second.log")
        self.assertEqual(rare["matches"][0]["line"], 1)
        self.assertTrue(data["coverage_complete"])

    def test_page_limit_does_not_stop_matching_before_eof(self):
        self._artifact("hit\n" * 100)
        data = self._batch(["hit"])
        self.assertEqual(data["scanned_bytes"], data["total_bytes"])
        self.assertTrue(data["coverage_complete"])
        self.assertTrue(data["index_complete"])
        self.assertEqual(data["queries"][0]["total"], 100)
        self.assertEqual(data["queries"][0]["next_offset"], 1)
        page = self._batch(["hit"], offset=99)["queries"][0]
        self.assertEqual(page["matches"][0]["char_index"], 396)
        self.assertIsNone(page["next_offset"])


class TextStreamSearchTests(unittest.TestCase):
    def test_chunk_boundaries_preserve_offsets_lines_and_full_context(self):
        text = "prefix\nerror: closed\naaab\nERROR suffix" + "x" * 30
        for chunk_size in (1, 3, 7, 16, 100):
            with self.subTest(chunk_size=chunk_size):
                hits = api._scan_text_multi(
                    (text[i:i + chunk_size] for i in range(0, len(text), chunk_size)),
                    ["error", "error: closed", "aa"], stop_after=100, context=6, with_lines=True,
                )
                by_query = {
                    needle: [occ for query, occ in hits if query == needle]
                    for needle in ("error", "error: closed", "aa")
                }
                self.assertEqual([occ.line for occ in by_query["error"]], [2, 4])
                self.assertEqual(len(by_query["error: closed"]), 1)
                self.assertEqual(len(by_query["aa"]), 2)
                for query, occ in hits:
                    self.assertEqual(text[occ.offset:occ.offset + len(query)].lower(), query.lower())
                    self.assertEqual(occ.snippet, text[max(0, occ.offset - 6):occ.offset + len(query) + 6])

    def test_utf8_bytes_split_across_file_chunks_keep_character_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "utf8.log"
            text = "前缀\n错误 error: closed\n"
            path.write_text(text, encoding="utf-8")
            with patch.object(api, "_SEARCH_CHUNK_BYTES", 3):
                hits, complete, scanned, size = api._scan_file_multi(
                    path, ["error", "error: closed"], stop_after=10,
                )
            self.assertTrue(complete)
            self.assertEqual(scanned, size)
            self.assertEqual([occ.offset for _, occ in hits], [text.index("error")] * 2)
