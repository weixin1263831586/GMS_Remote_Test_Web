"""Tests for FTS full-text search over raw_content (large-doc search coverage)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from features.notes.storage import NotesStorage


def _build_old_fts_db(path: Path) -> None:
    """构造一个 notes 表完整但 FTS 表「缺 raw_content 列」的老库。

    模拟升级前的真实线上库：FTS schema 是旧的（无 raw_content），notes 行的
    raw_content 全文存在但未被索引。验证 init_db 自动升级 FTS 并回填全文索引。
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE notes (
            note_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, notebook TEXT DEFAULT '',
            title TEXT NOT NULL, content TEXT NOT NULL, raw_content TEXT DEFAULT '',
            source TEXT DEFAULT 'manual', source_file TEXT DEFAULT '', tags TEXT DEFAULT '',
            summary TEXT DEFAULT '', keywords TEXT DEFAULT '', created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT '', links TEXT DEFAULT '{}', related_module TEXT DEFAULT ''
        );
        CREATE TABLE notebooks (user_id TEXT, name TEXT, PRIMARY KEY (user_id, name));
        CREATE VIRTUAL TABLE notes_fts USING fts5(
            note_id UNINDEXED, title, content, tags, summary, keywords
        );
        INSERT INTO notes (note_id, user_id, notebook, title, content, raw_content)
            VALUES ('R1', 'u1', '测试问题库', 'GMS指南', '精炼开头', '前文PAB关键词在后半段test_suites');
        INSERT INTO notes_fts (note_id, title, content, tags, summary, keywords)
            VALUES ('R1', 'GMS指南', '精炼开头', '', '', '');
        """
    )
    conn.commit()
    conn.close()


class FtsFullTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "notes.sqlite3"
        self.upload_dir = Path(self._tmp.name) / "uploads"
        self.storage = NotesStorage(db_path=self.db_path, upload_dir=self.upload_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_search_finds_keyword_only_in_raw_content(self) -> None:
        """content 里没有的关键词，只在 raw_content 全文里——升级后必须能搜到。"""
        note = self.storage.create_note(
            "u1",
            {
                "title": "GMS 配置",
                "content": "这是 AI 精炼后的简短正文。",  # 不含目标关键词
                "raw_content": "正文开头……\n\n后面才是关键：test_suites_arm64 下载地址与 PAB 版本说明。",
            },
        )
        hits = self.storage.search("u1", "test_suites", limit=10)
        ids = [h["note_id"] for h in hits]
        self.assertIn(note["note_id"], ids, "raw_content 里的关键词必须可被 FTS 命中")

    def test_search_keyword_in_pab(self) -> None:
        note = self.storage.create_note(
            "u1",
            {
                "title": "x",
                "content": "开头",
                "raw_content": "android17 PAB 版本 CTS 测试",
            },
        )
        hits = self.storage.search("u1", "PAB", limit=10)
        self.assertIn(note["note_id"], [h["note_id"] for h in hits])

    def test_old_fts_schema_upgraded_and_backfilled(self) -> None:
        """老库 FTS 缺 raw_content 列：init_db 应升级 schema 并回填，使老行的全文可搜。"""
        self._tmp.cleanup()  # 丢弃新库
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "notes.sqlite3"
        upload_dir = Path(self._tmp.name) / "uploads"
        _build_old_fts_db(db_path)

        storage = NotesStorage(db_path=db_path, upload_dir=upload_dir)

        # 老行 R1 的 raw_content 含 test_suites/PAB，但旧 FTS 没索引过——升级后应能搜到。
        hits = storage.search("u1", "test_suites", limit=10)
        ids = [h["note_id"] for h in hits]
        self.assertIn("R1", ids)
        # 验证 FTS schema 已含 raw_content
        sql = storage._connect().execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='notes_fts'"
        ).fetchone()
        self.assertIn("raw_content", sql["sql"])


class OrphanCleanupTests(unittest.TestCase):
    def test_delete_note_removes_upload_dir(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            db_path = Path(tmp.name) / "n.sqlite3"
            upload_dir = Path(tmp.name) / "uploads"
            storage = NotesStorage(db_path=db_path, upload_dir=upload_dir)
            note = storage.create_note("u1", {"title": "t", "content": "c"})
            # 模拟 api 层建立的约定上传目录 <upload>/<user>/<note_id>/
            target = upload_dir / "u1" / note["note_id"]
            target.mkdir(parents=True, exist_ok=True)
            (target / "orig.pdf").write_text("x")
            self.assertTrue(target.exists())
            ok = storage.delete_note("u1", note["note_id"])
            self.assertTrue(ok)
            self.assertFalse(target.exists(), "删除笔记应清理其上传原件目录")
        finally:
            tmp.cleanup()

    def test_delete_nonexistent_returns_false(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            storage = NotesStorage(
                db_path=Path(tmp.name) / "n.sqlite3", upload_dir=Path(tmp.name) / "u"
            )
            self.assertFalse(storage.delete_note("u1", "nope"))
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
