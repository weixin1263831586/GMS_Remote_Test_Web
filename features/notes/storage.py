"""笔记存储层：sqlite + FTS5 全文检索。

镜像 Redmine repository_schema.py / repository_storage.py 的连接与检索模式，
但合并为单文件并简化为「笔记」单一领域模型。
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from foundation.config import settings


DB_PATH: Path = settings.data_root / "notes/notes.sqlite3"
UPLOAD_DIR: Path = settings.data_root / "notes/uploads"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# FTS5 查询：长 token 精确匹配，短 token 前缀匹配，OR 连接（仿 redmine _fts_query）。
def _fts_query(query: str) -> str:
    tokens = _search_terms(query)
    if not tokens:
        return ""
    parts: list[str] = []
    for t in tokens[:12]:
        parts.append(f'"{t}"' if len(t) >= 2 else f'"{t}"*')
    return " OR ".join(parts)


def _search_terms(query: str) -> list[str]:
    """把中文/英文混合查询拆成更适合本地检索的词。

    SQLite FTS5 默认分词对「GMS测试」这类连续混合词不友好，用户输入
    通常也不会主动加空格。这里保留原始词，同时拆出英文数字串和连续中文片段。
    """
    text = (query or "").strip().lower()
    if not text:
        return []
    candidates: list[str] = []
    candidates.extend(t for t in re.split(r"\s+", text) if t)
    candidates.extend(re.findall(r"[a-z0-9][a-z0-9_.+-]*", text))
    candidates.extend(re.findall(r"[\u4e00-\u9fff]{2,}", text))

    # 对较长中文片段补二字滑窗，覆盖「认证测试」中搜「测试」的场景。
    for cjk in re.findall(r"[\u4e00-\u9fff]{3,}", text):
        candidates.extend(cjk[i : i + 2] for i in range(len(cjk) - 1))

    terms: list[str] = []
    seen: set[str] = set()
    for term in candidates:
        term = term.strip().strip('"')
        if not term or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return terms[:24]


class NotesStorage:
    """笔记数据库读写 + FTS5 全文检索。线程安全：每次调用新建连接。"""

    def __init__(self, db_path: Path = DB_PATH, upload_dir: Path = UPLOAD_DIR) -> None:
        self.db_path = Path(db_path)
        self.upload_dir = Path(upload_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.init_db()

    # ---------- schema ----------
    def init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    note_id      TEXT PRIMARY KEY,
                    user_id      TEXT NOT NULL,
                    notebook     TEXT DEFAULT '',
                    title        TEXT NOT NULL,
                    content      TEXT NOT NULL,
                    raw_content  TEXT DEFAULT '',
                    source       TEXT DEFAULT 'manual',
                    source_file  TEXT DEFAULT '',
                    tags         TEXT DEFAULT '',
                    summary      TEXT DEFAULT '',
                    keywords     TEXT DEFAULT '',
                    created_at   TEXT DEFAULT '',
                    updated_at   TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS notebooks (
                    user_id TEXT,
                    name    TEXT,
                    PRIMARY KEY (user_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_notes_user ON notes(user_id);
                CREATE INDEX IF NOT EXISTS idx_notes_notebook ON notes(user_id, notebook);
                CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                    note_id UNINDEXED,
                    title,
                    content,
                    tags,
                    summary,
                    keywords
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------- CRUD ----------
    def create_note(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        note_id = payload.get("note_id") or uuid.uuid4().hex[:12]
        now = _now()
        record = {
            "note_id": note_id,
            "user_id": user_id,
            "notebook": (payload.get("notebook") or "").strip(),
            "title": (payload.get("title") or "无标题").strip()[:200],
            "content": payload.get("content") or "",
            "raw_content": payload.get("raw_content") or "",
            "source": payload.get("source") or "manual",
            "source_file": payload.get("source_file") or "",
            "tags": payload.get("tags") or "",
            "summary": payload.get("summary") or "",
            "keywords": payload.get("keywords") or "",
            "created_at": payload.get("created_at") or now,
            "updated_at": payload.get("updated_at") or now,
        }
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO notes (note_id, user_id, notebook, title, content,
                   raw_content, source, source_file, tags, summary, keywords,
                   created_at, updated_at)
                   VALUES (:note_id, :user_id, :notebook, :title, :content,
                   :raw_content, :source, :source_file, :tags, :summary, :keywords,
                   :created_at, :updated_at)""",
                record,
            )
            if record["notebook"]:
                conn.execute(
                    "INSERT OR IGNORE INTO notebooks (user_id, name) VALUES (?, ?)",
                    (user_id, record["notebook"]),
                )
            self._replace_fts(conn, record)
            conn.commit()
        record.pop("user_id", None)
        return record

    def update_note(self, user_id: str, note_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        allowed = ["notebook", "title", "content", "tags", "summary", "keywords"]
        fields = [f for f in allowed if f in updates]
        if not fields:
            return self.get_note(user_id, note_id)
        sets = ", ".join(f"{f} = ?" for f in fields) + ", updated_at = ?"
        values = [updates[f] for f in fields] + [_now(), note_id, user_id]
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE notes SET {sets} WHERE note_id = ? AND user_id = ?", values
            )
            if cur.rowcount == 0:
                conn.commit()
                return None
            if "notebook" in updates and updates["notebook"]:
                conn.execute(
                    "INSERT OR IGNORE INTO notebooks (user_id, name) VALUES (?, ?)",
                    (user_id, updates["notebook"]),
                )
            # 同步 FTS：重写整行。
            row = conn.execute(
                "SELECT * FROM notes WHERE note_id = ? AND user_id = ?",
                (note_id, user_id),
            ).fetchone()
            if row:
                self._replace_fts(conn, dict(row))
            conn.commit()
            return dict(row) if row else None

    def delete_note(self, user_id: str, note_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM notes WHERE note_id = ? AND user_id = ?", (note_id, user_id)
            )
            conn.execute("DELETE FROM notes_fts WHERE note_id = ?", (note_id,))
            conn.commit()
            return cur.rowcount > 0

    def get_note(self, user_id: str, note_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notes WHERE note_id = ? AND user_id = ?", (note_id, user_id)
            ).fetchone()
            return dict(row) if row else None

    def list_notes(
        self,
        user_id: str,
        *,
        notebook: str = "",
        tag: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM notes WHERE user_id = ?"
        params: list[Any] = [user_id]
        if notebook:
            sql += " AND notebook = ?"
            params.append(notebook)
        if tag:
            # 标签按逗号分隔存储，用 LIKE 匹配整词边界。
            sql += " AND (',' || tags || ',' LIKE ?)"
            params.append(f"%,{tag},%")
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def list_notebooks(self, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT n.name AS name,
                          (SELECT COUNT(*) FROM notes x WHERE x.user_id = n.user_id AND x.notebook = n.name) AS count
                   FROM notebooks n WHERE n.user_id = ?
                   ORDER BY n.name COLLATE NOCASE""",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_tags(self, user_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT tags FROM notes WHERE user_id = ? AND tags != ''", (user_id,)
            ).fetchall()
        counter: dict[str, int] = {}
        for r in rows:
            for t in (r["tags"] or "").split(","):
                t = t.strip()
                if t:
                    counter[t] = counter.get(t, 0) + 1
        return [{"tag": k, "count": v} for k, v in sorted(counter.items(), key=lambda kv: -kv[1])[:limit]]

    # ---------- 检索 ----------
    def search(self, user_id: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
        fts = _fts_query(query)
        if not fts:
            return self.list_notes(user_id, limit=limit)
        sql = """
            SELECT n.*, bm25(notes_fts) AS rank
            FROM notes_fts f
            JOIN notes n ON n.note_id = f.note_id
            WHERE notes_fts MATCH ? AND n.user_id = ?
            ORDER BY rank
            LIMIT ?
        """
        terms = _search_terms(query)
        with self._connect() as conn:
            rows: list[sqlite3.Row] = []
            try:
                rows = conn.execute(sql, (fts, user_id, limit)).fetchall()
            except sqlite3.OperationalError:
                # FTS 查询语法异常时回退到 LIKE。
                rows = []
            merged: dict[str, dict[str, Any]] = {}
            for row in rows:
                item = dict(row)
                merged[item["note_id"]] = item
            if len(merged) < limit:
                for item in self._like_search(conn, user_id, terms, limit * 2):
                    merged.setdefault(item["note_id"], item)
                    if len(merged) >= limit:
                        break
            return list(merged.values())[:limit]

    @staticmethod
    def _like_search(
        conn: sqlite3.Connection,
        user_id: str,
        terms: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        if not terms:
            return []
        fields = ("title", "content", "raw_content", "tags", "summary", "keywords")
        where_parts: list[str] = []
        params: list[Any] = [user_id]
        for term in terms[:12]:
            like = f"%{term}%"
            where_parts.append("(" + " OR ".join(f"{field} LIKE ?" for field in fields) + ")")
            params.extend([like] * len(fields))
        params.append(limit)
        rows = conn.execute(
            f"""SELECT *, 0 AS rank FROM notes
                WHERE user_id = ? AND ({' OR '.join(where_parts)})
                ORDER BY updated_at DESC LIMIT ?""",
            params,
        ).fetchall()
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            item = dict(row)
            haystack = " ".join(str(item.get(field) or "").lower() for field in fields)
            score = sum(1 for term in terms if term and term in haystack)
            scored.append((score, item))
        scored.sort(key=lambda pair: -pair[0])
        return [item for _, item in scored]

    # ---------- 内部 ----------
    @staticmethod
    def _replace_fts(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
        try:
            conn.execute("DELETE FROM notes_fts WHERE note_id = ?", (payload.get("note_id"),))
            conn.execute(
                """INSERT INTO notes_fts (note_id, title, content, tags, summary, keywords)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    payload.get("note_id"),
                    payload.get("title") or "",
                    payload.get("content") or "",
                    payload.get("tags") or "",
                    payload.get("summary") or "",
                    payload.get("keywords") or "",
                ),
            )
        except sqlite3.OperationalError:
            pass
