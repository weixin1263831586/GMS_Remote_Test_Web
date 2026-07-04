"""Tests for large-document chunked structuring in features.notes.ai."""

from __future__ import annotations

import unittest
from unittest import mock

from features.notes import ai


class ChunkTextTests(unittest.TestCase):
    def test_short_text_single_chunk(self) -> None:
        self.assertEqual(ai._chunk_text("hello", 100), ["hello"])

    def test_long_text_split_at_newline_boundary(self) -> None:
        text = "line1\nline2\nline3\nline4"
        chunks = ai._chunk_text(text, 11)  # 大约每两行一块
        self.assertTrue(len(chunks) >= 2)
        # 切点应在换行处，块的拼接覆盖全文
        self.assertEqual("".join(chunks), text)

    def test_empty(self) -> None:
        self.assertEqual(ai._chunk_text("", 10), [])


class StructureNoteLargeTests(unittest.TestCase):
    def test_short_text_uses_single_call(self) -> None:
        with mock.patch.object(ai, "_structure_note_single") as single, \
             mock.patch.object(ai, "_structure_note_large") as large:
            ai.structure_note("短笔记内容")
            single.assert_called_once()
            large.assert_not_called()

    def test_long_text_uses_chunked_path(self) -> None:
        long_text = "x" * (ai._STRUCTURE_LARGE_THRESHOLD + 100)
        with mock.patch.object(ai, "_structure_note_single") as single, \
             mock.patch.object(ai, "_structure_note_large") as large:
            ai.structure_note(long_text)
            large.assert_called_once()
            single.assert_not_called()

    def test_large_merges_segments_and_dedups_tags(self) -> None:
        """分段精炼：每段返回独立 content/tags，合并后去重、首段提供 title/summary。"""
        captured_first_chars: list[str] = []

        def fake_single(text, **kw):
            captured_first_chars.append(text[:3])
            return {
                "title": "段标题",
                "tags": "gms,rockchip",
                "keywords": "k1,k2",
                "summary": "段摘要",
                "content": f"## {text[:3]}\n正文",
            }
        with mock.patch.object(ai, "_structure_note_single", side_effect=fake_single):
            # 三段明显分离的长文本，确保 _chunk_text 切出多块
            text = "AAA段\n" + ("x" * 6000) + "\nBBB段\n" + ("y" * 6000) + "\nCCC段"
            result = ai._structure_note_large(text)
        self.assertEqual(result["title"], "段标题")
        self.assertEqual(result["summary"], "段摘要")
        # tags 应去重
        self.assertEqual(result["tags"], "gms,rockchip")
        # 至少两块，content 含每块的精炼正文
        self.assertGreaterEqual(len(captured_first_chars), 2)
        for ch in captured_first_chars:
            self.assertIn(f"## {ch}\n正文", result["content"])

    def test_large_segment_failure_falls_back_to_raw(self) -> None:
        """某段精炼失败时，用该段原文兜底，不丢失内容。"""
        calls = {"n": 0}

        def fake_single(text, **kw):
            calls["n"] += 1
            if calls["n"] == 2:  # 第二段失败
                return {}
            return {"title": "t", "tags": "a", "keywords": "k", "summary": "s", "content": "精炼" + text[:3]}

        with mock.patch.object(ai, "_structure_note_single", side_effect=fake_single):
            text = "AAA\n" + ("x" * 6000) + "\nBBB_UNIQUE_MARKER\n" + ("y" * 6000)
            result = ai._structure_note_large(text)
        # 失败段的原文标记应保留在 content 中
        self.assertIn("BBB_UNIQUE_MARKER", result["content"])


if __name__ == "__main__":
    unittest.main()
