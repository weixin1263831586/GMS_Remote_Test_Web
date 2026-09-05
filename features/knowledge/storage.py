from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from foundation.config import settings

from .schema import KNOWLEDGE_REQUIRED_TABLES, initialize_knowledge_schema
from .versions import KnowledgeVersionMixin


logger = logging.getLogger(__name__)
DB_PATH: Path = settings.data_root / "knowledge/knowledge.sqlite3"
ATTACHMENT_DIR: Path = settings.data_root / "knowledge/attachments"
DEFAULT_SPACES = [
    ("gms", "GMS测试", "book-open"),
    ("devices", "设备接入", "plug"),
    ("issues", "问题沉淀", "ticket"),
]
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _user_default_space_id(user_id: str, base_id: str) -> str:
    # This digest is a stable namespace suffix, not a security primitive.
    digest = hashlib.sha1(
        user_id.encode("utf-8", errors="ignore"), usedforsecurity=False
    ).hexdigest()[:10]
    return f"{base_id}_{digest}"


def _terms(query: str) -> list[str]:
    text = (query or "").strip().lower()
    if not text:
        return []
    candidates: list[str] = []
    candidates.extend(t for t in re.split(r"\s+", text) if t)
    candidates.extend(re.findall(r"[a-z0-9][a-z0-9_.:+-]*", text))
    candidates.extend(re.findall(r"[\u4e00-\u9fff]{2,}", text))
    for cjk in re.findall(r"[\u4e00-\u9fff]{3,}", text):
        candidates.extend(cjk[i : i + 2] for i in range(len(cjk) - 1))
    out: list[str] = []
    seen: set[str] = set()
    for term in candidates:
        term = term.strip().strip('"')
        if term and term not in seen:
            seen.add(term)
            out.append(term)
    return out[:24]


def _fts_query(query: str) -> str:
    parts = []
    for term in _terms(query)[:12]:
        parts.append(f'"{term}"' if len(term) >= 2 else f'"{term}"*')
    return " OR ".join(parts)


def search_terms(query: str) -> list[str]:
    """Public tokenizer for ranking/snippet extraction."""
    return _terms(query)


def _csv(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw = value
    else:
        raw = re.split(r"[,，;；\n]+", str(value))
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        tag = str(item or "").strip()
        if tag and tag not in seen:
            seen.add(tag)
            result.append(tag)
    return result


class KnowledgeStore(KnowledgeVersionMixin):
    _REQUIRED_TABLES = KNOWLEDGE_REQUIRED_TABLES

    def __init__(self, db_path: Path = DB_PATH, attachment_dir: Path = ATTACHMENT_DIR) -> None:
        self.db_path = Path(db_path)
        self.attachment_dir = Path(attachment_dir)
        self._schema_lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.attachment_dir.mkdir(parents=True, exist_ok=True)
        # 已确保播种默认空间的用户集合（避免每次读操作都跑 COUNT 探测）。
        self._ensured_users: set[str] = set()
        self._initialize_schema()

    def _open_connection(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.attachment_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _connect(self) -> sqlite3.Connection:
        conn = self._open_connection()
        existing_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not self._REQUIRED_TABLES.issubset(existing_tables):
            conn.close()
            self._initialize_schema()
            conn = self._open_connection()
        return conn

    def _initialize_schema(self) -> None:
        with self._schema_lock:
            self.init_db()
            self.init_version_db()
            self._ensured_users.clear()

    def init_db(self) -> None:
        with self._open_connection() as conn:
            initialize_knowledge_schema(conn)

    def ensure_default_spaces(self, user_id: str) -> None:
        if user_id in self._ensured_users:
            # Opening the connection detects a runtime data/ deletion and
            # clears the cache before deciding whether seeding can be skipped.
            with self._connect():
                pass
            if user_id in self._ensured_users:
                return
        now = _now()
        with self._connect() as conn:
            for idx, (sid, name, icon) in enumerate(DEFAULT_SPACES):
                hashed_id = _user_default_space_id(user_id, sid)
                owned = conn.execute(
                    "SELECT 1 FROM knowledge_spaces WHERE user_id=? AND space_id IN (?, ?)",
                    (user_id, sid, hashed_id),
                ).fetchone()
                if owned:
                    continue
                exists = conn.execute(
                    "SELECT user_id FROM knowledge_spaces WHERE space_id = ?",
                    (sid,),
                ).fetchone()
                space_id = sid if not exists else hashed_id
                conn.execute(
                    """INSERT OR IGNORE INTO knowledge_spaces
                       (space_id, user_id, name, icon, sort_order, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (space_id, user_id, name, icon, idx * 10, now, now),
                )
            conn.commit()
        self._ensured_users.add(user_id)

    def default_space_id(self, user_id: str, base_id: str = "gms") -> str:
        self.ensure_default_spaces(user_id)
        hashed_id = _user_default_space_id(user_id, base_id)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT space_id FROM knowledge_spaces
                   WHERE user_id = ? AND space_id IN (?, ?)
                   ORDER BY CASE WHEN space_id = ? THEN 0 ELSE 1 END LIMIT 1""",
                (user_id, base_id, hashed_id, base_id),
            ).fetchone()
            if row:
                return row["space_id"]
            row = conn.execute(
                "SELECT space_id FROM knowledge_spaces WHERE user_id = ? ORDER BY sort_order, updated_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            return row["space_id"] if row else base_id

    def list_spaces(self, user_id: str) -> list[dict[str, Any]]:
        self.ensure_default_spaces(user_id)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT s.*,
                          (SELECT COUNT(*) FROM knowledge_docs d WHERE d.user_id=s.user_id AND d.space_id=s.space_id) AS doc_count
                   FROM knowledge_spaces s
                   WHERE s.user_id = ?
                   ORDER BY s.sort_order, s.updated_at DESC""",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def create_space(self, user_id: str, name: str, icon: str = "") -> dict[str, Any]:
        now = _now()
        record = {
            "space_id": _id("sp"),
            "user_id": user_id,
            "name": (name or "新知识库").strip()[:80],
            "icon": (icon or "book-open").strip()[:40],
            "sort_order": 1000,
            "created_at": now,
            "updated_at": now,
        }
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO knowledge_spaces
                   (space_id, user_id, name, icon, sort_order, created_at, updated_at)
                   VALUES (:space_id, :user_id, :name, :icon, :sort_order, :created_at, :updated_at)""",
                record,
            )
            conn.commit()
        record.pop("user_id", None)
        return record

    def list_tree(self, user_id: str, space_id: str) -> list[dict[str, Any]]:
        self.ensure_default_spaces(user_id)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT n.*,
                          d.doc_id,
                          d.summary,
                          d.favorite,
                          d.source,
                          COALESCE((SELECT COUNT(*) FROM knowledge_nodes c WHERE c.parent_id=n.node_id AND c.archived=0), 0) AS child_count
                   FROM knowledge_nodes n
                   LEFT JOIN knowledge_docs d ON d.node_id = n.node_id
                   WHERE n.user_id = ? AND n.space_id = ? AND n.archived = 0
                   ORDER BY n.parent_id, n.sort_order, n.updated_at DESC""",
                (user_id, space_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def create_folder(self, user_id: str, space_id: str, title: str, parent_id: str = "") -> dict[str, Any]:
        return self._create_node(user_id, space_id, "folder", title, parent_id)

    def create_doc(
        self,
        user_id: str,
        *,
        space_id: str,
        title: str,
        content_md: str,
        parent_id: str = "",
        tags: Any = None,
        summary: str = "",
        raw_content: str = "",
        source: str = "manual",
        source_file: str = "",
        links: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        node = self._create_node(user_id, space_id, "doc", title, parent_id, now=now)
        record = {
            "doc_id": _id("doc"),
            "user_id": user_id,
            "space_id": space_id,
            "node_id": node["node_id"],
            "content_md": content_md or "",
            "raw_content": raw_content or content_md or "",
            "summary": summary or "",
            "source": source or "manual",
            "source_file": source_file or "",
            "favorite": 0,
            "created_at": now,
            "updated_at": now,
        }
        tag_names = _csv(tags)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO knowledge_docs
                   (doc_id, user_id, space_id, node_id, content_md, raw_content, summary, source,
                    source_file, favorite, created_at, updated_at)
                   VALUES (:doc_id, :user_id, :space_id, :node_id, :content_md, :raw_content,
                    :summary, :source, :source_file, :favorite, :created_at, :updated_at)""",
                record,
            )
            self._replace_tags(conn, user_id, record["doc_id"], tag_names)
            self._replace_links(conn, user_id, record["doc_id"], links or [])
            self._replace_fts(conn, record["doc_id"])
            self._save_version(conn, user_id, record["doc_id"], title=title)
            conn.commit()
        return self.get_doc(user_id, record["doc_id"]) or {}

    def _create_node(
        self,
        user_id: str,
        space_id: str,
        node_type: str,
        title: str,
        parent_id: str = "",
        *,
        now: str | None = None,
    ) -> dict[str, Any]:
        now = now or _now()
        record = {
            "node_id": _id("node"),
            "user_id": user_id,
            "space_id": space_id,
            "parent_id": parent_id or "",
            "type": node_type,
            "title": (title or ("新文档" if node_type == "doc" else "新目录")).strip()[:200],
            "sort_order": 1000,
            "archived": 0,
            "created_at": now,
            "updated_at": now,
        }
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO knowledge_nodes
                   (node_id, user_id, space_id, parent_id, type, title, sort_order, archived, created_at, updated_at)
                   VALUES (:node_id, :user_id, :space_id, :parent_id, :type, :title,
                    :sort_order, :archived, :created_at, :updated_at)""",
                record,
            )
            conn.commit()
        record.pop("user_id", None)
        return record

    def get_doc(self, user_id: str, doc_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT d.*, n.title, n.parent_id, n.type, n.archived
                   FROM knowledge_docs d
                   JOIN knowledge_nodes n ON n.node_id = d.node_id
                   WHERE d.user_id = ? AND d.doc_id = ? AND n.archived = 0""",
                (user_id, doc_id),
            ).fetchone()
            if not row:
                return None
            doc = dict(row)
            doc["tags"] = self._doc_tags(conn, doc_id)
            doc["links"] = self._doc_links(conn, doc_id)
            doc["attachments"] = self._doc_attachments(conn, doc_id)
            return doc

    def update_doc(self, user_id: str, doc_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        now = _now()
        allowed_doc = {"content_md", "raw_content", "summary", "source", "source_file", "favorite"}
        with self._connect() as conn:
            current = conn.execute(
                "SELECT d.*, n.title FROM knowledge_docs d JOIN knowledge_nodes n ON n.node_id=d.node_id WHERE d.user_id=? AND d.doc_id=?",
                (user_id, doc_id),
            ).fetchone()
            if not current:
                return None
            if "title" in updates:
                conn.execute(
                    "UPDATE knowledge_nodes SET title=?, updated_at=? WHERE node_id=? AND user_id=?",
                    (str(updates["title"] or "无标题")[:200], now, current["node_id"], user_id),
                )
            fields = [f for f in allowed_doc if f in updates]
            if fields:
                sets = ", ".join(f"{f}=?" for f in fields) + ", updated_at=?"
                values = [updates[f] for f in fields] + [now, doc_id, user_id]
                conn.execute(
                    f"UPDATE knowledge_docs SET {sets} WHERE doc_id=? AND user_id=?",
                    values,
                )
            if "tags" in updates:
                self._replace_tags(conn, user_id, doc_id, _csv(updates.get("tags")))
            if "links" in updates:
                self._replace_links(conn, user_id, doc_id, list(updates.get("links") or []))
            conn.execute("UPDATE knowledge_nodes SET updated_at=? WHERE node_id=? AND user_id=?", (now, current["node_id"], user_id))
            self._replace_fts(conn, doc_id)
            self._save_version(conn, user_id, doc_id)
            conn.commit()
        return self.get_doc(user_id, doc_id)
    def move_node(self, user_id: str, node_id: str, parent_id: str = "", sort_order: int | None = None) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE knowledge_nodes
                   SET parent_id=?, sort_order=COALESCE(?, sort_order), updated_at=?
                   WHERE user_id=? AND node_id=?""",
                (parent_id or "", sort_order, _now(), user_id, node_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def delete_node(self, user_id: str, node_id: str) -> bool:
        with self._connect() as conn:
            rows = conn.execute(
                """WITH RECURSIVE descendants(node_id, type) AS (
                       SELECT node_id, type FROM knowledge_nodes
                       WHERE user_id=? AND node_id=?
                       UNION ALL
                       SELECT child.node_id, child.type
                       FROM knowledge_nodes child
                       JOIN descendants parent ON child.parent_id=parent.node_id
                       WHERE child.user_id=?
                   )
                   SELECT node_id, type FROM descendants""",
                (user_id, node_id, user_id),
            ).fetchall()
            ids = {r["node_id"] for r in rows}
            if not ids:
                return False
            doc_ids = [
                r["doc_id"]
                for r in conn.execute(
                    f"SELECT doc_id FROM knowledge_docs WHERE node_id IN ({','.join('?' for _ in ids)})",
                    list(ids),
                ).fetchall()
            ]
            for did in doc_ids:
                self._delete_doc_files(user_id, did)
                for tbl in (
                    "knowledge_fts",
                    "knowledge_doc_tags",
                    "knowledge_attachments",
                    "knowledge_links",
                    "knowledge_docs",
                ):
                    conn.execute(f"DELETE FROM {tbl} WHERE doc_id=?", (did,))
            cur = conn.execute(
                f"DELETE FROM knowledge_nodes WHERE user_id=? AND node_id IN ({','.join('?' for _ in ids)})",
                [user_id, *list(ids)],
            )
            conn.commit()
            return cur.rowcount > 0

    def list_docs(
        self,
        user_id: str,
        *,
        space_id: str = "",
        parent_id: str | None = None,
        tag: str = "",
        favorite: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = """SELECT d.*, n.title, n.parent_id FROM knowledge_docs d
                 JOIN knowledge_nodes n ON n.node_id=d.node_id
                 WHERE d.user_id=? AND n.archived=0"""
        params: list[Any] = [user_id]
        if space_id:
            sql += " AND d.space_id=?"
            params.append(space_id)
        if parent_id is not None:
            sql += " AND n.parent_id=?"
            params.append(parent_id)
        if favorite:
            sql += " AND d.favorite=1"
        if tag:
            sql += """ AND EXISTS (
                SELECT 1 FROM knowledge_doc_tags dt JOIN knowledge_tags t ON t.tag_id=dt.tag_id
                WHERE dt.doc_id=d.doc_id AND t.name=?
            )"""
            params.append(tag)
        sql += " ORDER BY d.updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            docs = [dict(r) for r in rows]
            tags_by_doc = self._tags_for_docs(conn, [doc["doc_id"] for doc in docs])
            for doc in docs:
                doc["tags"] = tags_by_doc.get(doc["doc_id"], [])
            return docs

    def search(self, user_id: str, query: str, *, space_id: str = "", tag: str = "", limit: int = 50) -> list[dict[str, Any]]:
        fts = _fts_query(query)
        if not fts:
            return self.list_docs(user_id, space_id=space_id, tag=tag, limit=limit)
        sql = """SELECT d.*, n.title, n.parent_id, bm25(knowledge_fts) AS rank
                 FROM knowledge_fts f
                 JOIN knowledge_docs d ON d.doc_id=f.doc_id
                 JOIN knowledge_nodes n ON n.node_id=d.node_id
                 WHERE knowledge_fts MATCH ? AND d.user_id=? AND n.archived=0"""
        params: list[Any] = [fts, user_id]
        if space_id:
            sql += " AND d.space_id=?"
            params.append(space_id)
        if tag:
            sql += """ AND EXISTS (
                SELECT 1 FROM knowledge_doc_tags dt JOIN knowledge_tags t ON t.tag_id=dt.tag_id
                WHERE dt.doc_id=d.doc_id AND t.name=?
            )"""
            params.append(tag)
        sql += " ORDER BY rank, d.updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            docs = [dict(r) for r in rows]
            tags_by_doc = self._tags_for_docs(conn, [doc["doc_id"] for doc in docs])
            for doc in docs:
                doc["tags"] = tags_by_doc.get(doc["doc_id"], [])
            return docs

    def retrieve_contexts(
        self,
        user_id: str,
        query: str,
        *,
        space_id: str = "",
        limit: int = 6,
        window: int = 900,
    ) -> list[dict[str, Any]]:
        """Return ranked snippets from full documents for RAG answers."""
        docs = self.search(user_id, query, space_id=space_id, limit=max(limit * 3, 12))
        # 预先小写化分词，避免在内层循环里对每个文档重复 t.lower()。
        terms_lower = [t.lower() for t in search_terms(query) if t]
        contexts: list[dict[str, Any]] = []
        for doc in docs:
            full = str(doc.get("raw_content") or doc.get("content_md") or "")
            if not full:
                full = str(doc.get("summary") or "")
            lower = full.lower()
            positions = []
            for t_lower in terms_lower:
                idx = lower.find(t_lower)
                if idx >= 0:
                    positions.append(idx)
            pos = min(positions) if positions else 0
            start = max(0, pos - window // 3)
            end = min(len(full), start + window)
            snippet = full[start:end].strip()
            matched = [t for t in terms_lower if t in lower]
            contexts.append({
                "doc_id": doc.get("doc_id"),
                "title": doc.get("title"),
                "space_id": doc.get("space_id"),
                "summary": doc.get("summary") or "",
                "snippet": snippet,
                "matched_terms": matched,
                "updated_at": doc.get("updated_at") or "",
                "tags": doc.get("tags") or [],
            })
            if len(contexts) >= limit:
                break
        return contexts

    def list_tags(self, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT t.name AS tag, COUNT(dt.doc_id) AS count
                   FROM knowledge_tags t
                   JOIN knowledge_doc_tags dt ON dt.tag_id=t.tag_id
                   WHERE t.user_id=?
                   GROUP BY t.name
                   ORDER BY count DESC, t.name""",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def add_attachment(
        self,
        user_id: str,
        doc_id: str,
        *,
        source_path: str,
        original_name: str,
        mime: str = "",
        extracted_text: str = "",
    ) -> dict[str, Any]:
        doc = self.get_doc(user_id, doc_id)
        if not doc:
            raise ValueError("文档不存在")
        att_id = _id("att")
        dest_dir = self.attachment_dir / user_id / doc_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe_name = os.path.basename(original_name) or "attachment"
        dest = dest_dir / f"{att_id}_{safe_name}"
        shutil.copyfile(source_path, dest)
        record = {
            "attachment_id": att_id,
            "user_id": user_id,
            "doc_id": doc_id,
            "original_name": safe_name,
            "path": str(dest),
            "mime": mime,
            "size": dest.stat().st_size,
            "extracted_text": extracted_text,
            "created_at": _now(),
        }
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO knowledge_attachments
                   (attachment_id, user_id, doc_id, original_name, path, mime, size, extracted_text, created_at)
                   VALUES (:attachment_id, :user_id, :doc_id, :original_name, :path, :mime, :size, :extracted_text, :created_at)""",
                record,
            )
            self._replace_fts(conn, doc_id)
            conn.commit()
        record.pop("user_id", None)
        return record

    def _replace_tags(self, conn: sqlite3.Connection, user_id: str, doc_id: str, tags: list[str]) -> None:
        conn.execute("DELETE FROM knowledge_doc_tags WHERE doc_id=?", (doc_id,))
        for name in tags:
            tag_id = _id("tag")
            conn.execute(
                "INSERT OR IGNORE INTO knowledge_tags (tag_id, user_id, name) VALUES (?, ?, ?)",
                (tag_id, user_id, name),
            )
            row = conn.execute(
                "SELECT tag_id FROM knowledge_tags WHERE user_id=? AND name=?",
                (user_id, name),
            ).fetchone()
            if row:
                conn.execute(
                    "INSERT OR IGNORE INTO knowledge_doc_tags (doc_id, tag_id) VALUES (?, ?)",
                    (doc_id, row["tag_id"]),
                )

    def _replace_links(self, conn: sqlite3.Connection, user_id: str, doc_id: str, links: list[dict[str, Any]]) -> None:
        conn.execute("DELETE FROM knowledge_links WHERE doc_id=?", (doc_id,))
        for link in links:
            target_type = str(link.get("target_type") or "").strip()
            target_id = str(link.get("target_id") or "").strip()
            if not target_type or not target_id:
                continue
            conn.execute(
                """INSERT INTO knowledge_links
                   (link_id, user_id, doc_id, target_type, target_id, title, url, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _id("lnk"),
                    user_id,
                    doc_id,
                    target_type[:60],
                    target_id[:200],
                    str(link.get("title") or "")[:200],
                    str(link.get("url") or "")[:500],
                    _now(),
                ),
            )

    def _replace_fts(self, conn: sqlite3.Connection, doc_id: str) -> None:
        row = conn.execute(
            """SELECT d.doc_id, d.user_id, d.space_id, n.title, d.content_md, d.raw_content, d.summary
               FROM knowledge_docs d JOIN knowledge_nodes n ON n.node_id=d.node_id
               WHERE d.doc_id=?""",
            (doc_id,),
        ).fetchone()
        if not row:
            return
        tags = " ".join(self._doc_tags(conn, doc_id))
        attachments = " ".join(
            r["extracted_text"] or r["original_name"] or ""
            for r in conn.execute(
                "SELECT original_name, extracted_text FROM knowledge_attachments WHERE doc_id=?",
                (doc_id,),
            ).fetchall()
        )
        links = " ".join(
            f"{r['target_type']} {r['target_id']} {r['title'] or ''}"
            for r in conn.execute(
                "SELECT target_type, target_id, title FROM knowledge_links WHERE doc_id=?",
                (doc_id,),
            ).fetchall()
        )
        conn.execute("DELETE FROM knowledge_fts WHERE doc_id=?", (doc_id,))
        conn.execute(
            """INSERT INTO knowledge_fts
               (doc_id, user_id, space_id, title, content_md, raw_content, summary, tags, attachments, links)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["doc_id"],
                row["user_id"],
                row["space_id"],
                row["title"] or "",
                row["content_md"] or "",
                row["raw_content"] or "",
                row["summary"] or "",
                tags,
                attachments,
                links,
            ),
        )

    def _tags_for_docs(self, conn: sqlite3.Connection, doc_ids: list[str]) -> dict[str, list[str]]:
        """单次查询批量取回多文档的标签，避免 list/search 中的 N+1 查询。"""
        if not doc_ids:
            return {}
        placeholders = ",".join("?" for _ in doc_ids)
        rows = conn.execute(
            f"""SELECT dt.doc_id AS doc_id, t.name AS name
                FROM knowledge_doc_tags dt
                JOIN knowledge_tags t ON t.tag_id=dt.tag_id
                WHERE dt.doc_id IN ({placeholders})""",
            doc_ids,
        ).fetchall()
        result: dict[str, list[str]] = {doc_id: [] for doc_id in doc_ids}
        for r in rows:
            result[r["doc_id"]].append(r["name"])
        for names in result.values():
            names.sort()
        return result

    def _doc_tags(self, conn: sqlite3.Connection, doc_id: str) -> list[str]:
        return [
            r["name"]
            for r in conn.execute(
                """SELECT t.name FROM knowledge_doc_tags dt
                   JOIN knowledge_tags t ON t.tag_id=dt.tag_id
                   WHERE dt.doc_id=? ORDER BY t.name""",
                (doc_id,),
            ).fetchall()
        ]

    def _doc_links(self, conn: sqlite3.Connection, doc_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT link_id, target_type, target_id, title, url FROM knowledge_links WHERE doc_id=? ORDER BY created_at",
                (doc_id,),
            ).fetchall()
        ]
    def _doc_attachments(self, conn: sqlite3.Connection, doc_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT attachment_id, original_name, mime, size, created_at FROM knowledge_attachments WHERE doc_id=? ORDER BY created_at",
                (doc_id,),
            ).fetchall()
        ]

    def _delete_doc_files(self, user_id: str, doc_id: str) -> None:
        target = self.attachment_dir / user_id / doc_id
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
