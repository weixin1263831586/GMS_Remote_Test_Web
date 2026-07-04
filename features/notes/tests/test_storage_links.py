"""Tests for notes storage: links/related_module persistence, old-DB migration, preset notebooks."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from features.notes.storage import PRESET_NOTEBOOKS, NotesStorage


def _build_old_db(path: Path) -> None:
    """构造一个缺 links / related_module 列的旧 notes 表，验证 init_db 自动补列。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE notes (
            note_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            notebook TEXT DEFAULT '',
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            raw_content TEXT DEFAULT '',
            source TEXT DEFAULT 'manual',
            source_file TEXT DEFAULT '',
            tags TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            keywords TEXT DEFAULT '',
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        );
        CREATE TABLE notebooks (user_id TEXT, name TEXT, PRIMARY KEY (user_id, name));
        CREATE VIRTUAL TABLE notes_fts USING fts5(
            note_id UNINDEXED, title, content, tags, summary, keywords
        );
        INSERT INTO notes (note_id, user_id, notebook, title, content)
            VALUES ('OLD1', 'u1', '测试问题库', '旧标题', '旧内容');
        INSERT INTO notebooks (user_id, name) VALUES ('u1', '测试问题库');
        """
    )
    conn.commit()
    conn.close()


class StorageLinksTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "notes.sqlite3"
        self.upload_dir = Path(self._tmp.name) / "uploads"
        self.storage = NotesStorage(db_path=self.db_path, upload_dir=self.upload_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_create_persists_links_and_related_module(self) -> None:
        note = self.storage.create_note(
            "u1",
            {
                "title": "测试笔记",
                "content": "正文",
                "notebook": "Redmine问题沉淀",
                "related_module": "CameraModule::testOpen",
                "links": {"redmine_issue_ids": [123, 456], "report_timestamps": ["2026-07-03"]},
            },
        )
        # 返回字段
        self.assertIn("links", note)
        self.assertEqual(note["related_module"], "CameraModule::testOpen")
        self.assertIn("123", note["links"])

        # 持久化读回
        got = self.storage.get_note("u1", note["note_id"])
        self.assertIsNotNone(got)
        assert got is not None  # for type checker
        self.assertEqual(got["related_module"], "CameraModule::testOpen")
        self.assertIn("123", got["links"])
        self.assertIn("456", got["links"])

    def test_update_links_and_related_module(self) -> None:
        note = self.storage.create_note("u1", {"title": "t", "content": "c"})
        self.storage.update_note(
            "u1",
            note["note_id"],
            {
                "related_module": "WiFiModule::testScan",
                "links": {"gerrit_change_ids": [789]},
            },
        )
        got = self.storage.get_note("u1", note["note_id"])
        assert got is not None
        self.assertEqual(got["related_module"], "WiFiModule::testScan")
        self.assertIn("789", got["links"])

    def test_list_notebooks_includes_presets(self) -> None:
        notebooks = self.storage.list_notebooks("u1")
        names = [n["name"] for n in notebooks]
        for preset in PRESET_NOTEBOOKS:
            self.assertIn(preset, names)
        # 预置项置顶
        self.assertEqual(names[: len(PRESET_NOTEBOOKS)], PRESET_NOTEBOOKS)

    def test_old_db_migration_adds_columns_and_preserves_data(self) -> None:
        # 重新指向旧库：先关闭当前 tmp，新建一个旧库再打开
        self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "notes.sqlite3"
        upload_dir = Path(self._tmp.name) / "uploads"
        _build_old_db(db_path)

        storage = NotesStorage(db_path=db_path, upload_dir=upload_dir)

        # 老数据完好
        got = storage.get_note("u1", "OLD1")
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got["title"], "旧标题")
        # 补列后默认值合法：related_module 空串，links 为合法空 JSON 对象
        # （storage 层返回原始字符串，反序列化为含全部 3 个键的 dict 由 API 层 _decorate_note 负责）
        self.assertEqual(got["related_module"], "")
        self.assertEqual(got["links"], "{}")

        # 补列后能正常写入新字段
        new = storage.create_note(
            "u1",
            {
                "title": "迁移后新笔记",
                "content": "x",
                "related_module": "M::t",
                "links": {"redmine_issue_ids": [1]},
            },
        )
        self.assertEqual(new["related_module"], "M::t")


if __name__ == "__main__":
    unittest.main()
