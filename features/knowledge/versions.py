from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _id() -> str:
    return f"ver_{uuid.uuid4().hex[:12]}"


class KnowledgeVersionMixin:
    """Version snapshots for KnowledgeStore, kept separate from its core CRUD."""

    def init_version_db(self) -> None:
        with self._open_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_doc_versions (
                    version_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    doc_id TEXT NOT NULL,
                    version_no INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    content_md TEXT DEFAULT '',
                    raw_content TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    tags_json TEXT DEFAULT '[]',
                    links_json TEXT DEFAULT '[]',
                    created_at TEXT DEFAULT '',
                    UNIQUE(user_id, doc_id, version_no)
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_versions_doc
                    ON knowledge_doc_versions(user_id, doc_id, version_no DESC);
                """
            )

    def list_versions(self, user_id: str, doc_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            owned = conn.execute(
                "SELECT 1 FROM knowledge_docs WHERE user_id=? AND doc_id=?", (user_id, doc_id)
            ).fetchone()
            if not owned:
                return []
            rows = conn.execute(
                """SELECT version_id, doc_id, version_no, title, summary, created_at
                   FROM knowledge_doc_versions WHERE user_id=? AND doc_id=?
                   ORDER BY version_no DESC LIMIT ?""",
                (user_id, doc_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def restore_version(self, user_id: str, doc_id: str, version_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            version = conn.execute(
                "SELECT * FROM knowledge_doc_versions WHERE user_id=? AND doc_id=? AND version_id=?",
                (user_id, doc_id, version_id),
            ).fetchone()
        if not version:
            return None
        return self.update_doc(user_id, doc_id, {
            "title": version["title"], "content_md": version["content_md"],
            "raw_content": version["raw_content"], "summary": version["summary"],
            "tags": json.loads(version["tags_json"] or "[]"),
            "links": json.loads(version["links_json"] or "[]"),
        })

    def _save_version(
        self, conn: sqlite3.Connection, user_id: str, doc_id: str, *,
        current: sqlite3.Row | None = None, title: str = "",
    ) -> None:
        current = current or conn.execute(
            """SELECT d.*, n.title FROM knowledge_docs d JOIN knowledge_nodes n ON n.node_id=d.node_id
               WHERE d.user_id=? AND d.doc_id=?""", (user_id, doc_id),
        ).fetchone()
        if not current:
            return
        version_no = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM knowledge_doc_versions WHERE user_id=? AND doc_id=?",
            (user_id, doc_id),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO knowledge_doc_versions
               (version_id,user_id,doc_id,version_no,title,content_md,raw_content,
                summary,tags_json,links_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_id(), user_id, doc_id, version_no, str(title or current["title"] or "无标题"),
             current["content_md"] or "", current["raw_content"] or "", current["summary"] or "",
             json.dumps(self._doc_tags(conn, doc_id), ensure_ascii=False),
             json.dumps(self._doc_links(conn, doc_id), ensure_ascii=False), _now()),
        )
