"""kkagent 会话回放（daily_brief_session）单元测试。"""

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
                    {"type": "redacted_thinking", "data": "private blob"},
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
                ("s1", "assistant", json.dumps([
                    {"type": "text", "text": "## 结论\n\n证据不足。"},
                ]), "2026-09-24T01:00:08"),
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

    def test_groups_messages_and_tool_results_into_turns(self):
        data = session_module.session_transcript("s1")
        self.assertIsNotNone(data)
        self.assertEqual(data["format"], "turns-v1")
        self.assertEqual(data["total_messages"], 5)
        self.assertEqual(data["total_turns"], 3)
        context, tool_turn, final_turn = data["turns"]
        self.assertEqual(context["kind"], "context")
        self.assertEqual(tool_turn["kind"], "assistant")
        self.assertEqual(final_turn["kind"], "assistant")
        self.assertTrue(final_turn["is_final"])
        self.assertNotIn("thinking", str(data))

        text, tool, orphan_result = tool_turn["blocks"]
        self.assertEqual(text["kind"], "text")
        self.assertEqual(tool["kind"], "tool_use")
        self.assertEqual(tool["tool_call_id"], "c1")
        self.assertFalse(tool["result"]["is_error"])
        self.assertLessEqual(len(tool["result"]["output"]),
                             session_module.TOOL_OUTPUT_PREVIEW_CHARS + 40)
        self.assertEqual(orphan_result["kind"], "tool_result")
        self.assertTrue(orphan_result["is_error"])

    def test_pagination_is_stable_across_turns(self):
        with mock.patch.object(
            session_module,
            "_parse_message_blocks",
            wraps=session_module._parse_message_blocks,
        ) as parse:
            first = session_module.session_transcript("s1", offset=0, limit=2)
        self.assertEqual(first["returned"], 2)
        self.assertTrue(first["truncated"])
        self.assertIsNone(first["total_turns"])
        # 回合生成器按需读取；不会为总数另行重复扫描会话。
        self.assertEqual(parse.call_count, 5)
        second = session_module.session_transcript("s1", offset=first["next_offset"], limit=2)
        seqs = [e["sequence"] for e in first["turns"] + second["turns"]]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertEqual(first["turns"][0]["sequence"], 0)
        self.assertEqual(second["total_turns"], 3)

    def test_last_page_reports_exact_total_turns(self):
        data = session_module.session_transcript("s1", offset=2, limit=10)
        self.assertFalse(data["truncated"])
        self.assertEqual(data["total_turns"], 3)
        self.assertEqual(data["next_offset"], 3)

    def test_raw_messages_are_untrimmed_but_private_thinking_is_omitted(self):
        data = session_module.session_raw_messages("s1", offset=1, limit=2)

        self.assertEqual(data["format"], "raw-messages-v1")
        self.assertEqual(data["total_messages"], 5)
        self.assertEqual(data["next_offset"], 3)
        self.assertTrue(data["truncated"])
        self.assertEqual(
            data["omitted_block_types"],
            ["redacted_thinking", "thinking"],
        )
        assistant, tool_result = data["messages"]
        self.assertEqual(assistant["sequence"], 1)
        self.assertEqual(
            [block["type"] for block in assistant["content"]],
            ["text", "tool_use"],
        )
        untrimmed = tool_result["content"][0]["content"]
        self.assertGreater(len(untrimmed), session_module.TOOL_OUTPUT_PREVIEW_CHARS)
        self.assertNotIn("截断", untrimmed)

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
        self.assertEqual(len(data["turns"]), 1)
        self.assertEqual(data["turns"][0]["kind"], "context")
        self.assertEqual(data["turns"][0]["blocks"][0]["kind"], "text")
        raw = session_module.session_raw_messages("s3")
        self.assertEqual(raw["messages"][0]["content"], "not-json{{")


if __name__ == "__main__":
    unittest.main()
