"""kkagent 完整会话回放（daily_brief_session）单元测试。"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from features.redmine import daily_brief_session as session_module


class SessionTranscriptTests(unittest.TestCase):
    def _make_db(self, tmp: Path) -> Path:
        db_path = tmp / "transcripts.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT,"
                " role TEXT, content_json TEXT, created_at TEXT, token_count INT)"
            )
            rows = [
                ("s1", "user", json.dumps([
                    {"type": "text", "text": "分析 #641965"},
                ]), "2026-09-24T01:00:00"),
                ("s1", "assistant", json.dumps([
                    {"type": "thinking", "thinking": "secret reasoning"},
                    {"type": "text", "text": "先读快照"},
                    {"type": "tool_use", "id": "c1", "name": "gms_rt_redmine_journals",
                     "input": {"snapshot_id": "ev_1"}},
                ]), "2026-09-24T01:00:05"),
                ("s1", "user", json.dumps([
                    {"type": "tool_result", "tool_use_id": "c1", "is_error": False,
                     "content": json.dumps({"ok": True, "data": {"journals": "x" * 5000}})},
                ]), "2026-09-24T01:00:06"),
                ("s1", "user", json.dumps([
                    {"type": "tool_result", "tool_use_id": "c2", "is_error": True,
                     "content": json.dumps({"ok": False, "error": "artifact 不存在"})},
                ]), "2026-09-24T01:00:07"),
                ("s2", "user", json.dumps([{"type": "text", "text": "其他会话"}]),
                 "2026-09-24T02:00:00"),
            ]
            conn.executemany(
                "INSERT INTO messages (session_id, role, content_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                rows,
            )
        return db_path

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db_path = self._make_db(Path(self._tmp.name))
        patcher = mock.patch.object(session_module, "transcripts_db_path",
                                    return_value=db_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_skips_thinking_and_clips_long_output(self):
        data = session_module.session_transcript("s1")
        self.assertIsNotNone(data)
        self.assertEqual(data["total_messages"], 4)
        kinds = [e["kind"] for e in data["events"]]
        self.assertNotIn("thinking", kinds)
        tool_results = [e for e in data["events"] if e["kind"] == "tool_result"]
        self.assertEqual(len(tool_results), 2)
        ok = tool_results[0]
        self.assertFalse(ok["is_error"])
        self.assertLessEqual(len(ok["output"]),
                             session_module.TOOL_OUTPUT_PREVIEW_CHARS + 40)
        err = tool_results[1]
        self.assertTrue(err["is_error"])

    def test_pagination_is_stable_across_pages(self):
        with mock.patch.object(
            session_module,
            "_render_blocks",
            wraps=session_module._render_blocks,
        ) as render:
            first = session_module.session_transcript("s1", offset=0, limit=2)
        self.assertEqual(first["returned"], 2)
        self.assertTrue(first["truncated"])
        self.assertIsNone(first["total_events"])
        # 首页只读取到足以确认还有下一页的位置，不再解析整个会话。
        self.assertEqual(render.call_count, 2)
        second = session_module.session_transcript("s1", offset=first["next_offset"], limit=2)
        seqs = [e["sequence"] for e in first["events"] + second["events"]]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertEqual(first["events"][0]["sequence"], 0)

    def test_last_page_reports_exact_total_events(self):
        data = session_module.session_transcript("s1", offset=4, limit=10)
        self.assertFalse(data["truncated"])
        self.assertEqual(data["total_events"], 5)
        self.assertEqual(data["next_offset"], 5)

    def test_missing_session_returns_none(self):
        self.assertIsNone(session_module.session_transcript("nope"))
        self.assertFalse(session_module.session_exists("nope"))

    def test_exists(self):
        self.assertTrue(session_module.session_exists("s1"))

    def test_malformed_content_is_tolerated(self):
        with sqlite3.connect(Path(self._tmp.name) / "transcripts.db") as conn:
            conn.execute(
                "INSERT INTO messages (session_id, role, content_json, created_at)"
                " VALUES ('s3', 'user', 'not-json{{', '2026-09-24T03:00:00')"
            )
        data = session_module.session_transcript("s3")
        self.assertEqual(len(data["events"]), 1)
        self.assertEqual(data["events"][0]["kind"], "text")


if __name__ == "__main__":
    unittest.main()
