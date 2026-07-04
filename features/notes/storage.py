"""笔记存储层：sqlite + FTS5 全文检索。

镜像 Redmine repository_schema.py / repository_storage.py 的连接与检索模式，
但合并为单文件并简化为「笔记」单一领域模型。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from foundation.config import settings

logger = logging.getLogger(__name__)


DB_PATH: Path = settings.data_root / "notes/notes.sqlite3"
UPLOAD_DIR: Path = settings.data_root / "notes/uploads"

# 预置固定分类：Wiki 的顶层知识分区。list_notebooks 会把它们合并进返回结果
# （count=0 也显示，置顶去重）。新建笔记可直接把 notebook 写成其中之一。
PRESET_NOTEBOOKS = [
    "测试问题库",
    "设备接入文档",
    "固件烧录文档",
    "Redmine问题沉淀",
    "Gerrit补丁说明",
    "FAQ",
]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize_links(raw: Any) -> str:
    """把 links 规范化为合法 JSON 字符串。

    接受 dict / JSON 字符串 / None，统一序列化成
    {"report_timestamps": [...], "redmine_issue_ids": [...], "gerrit_change_ids": [...]}。
    前端拿到时由 API 层反序列化回 dict。
    """
    if raw is None:
        raw = {}
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            raw = {}
        else:
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                raw = {}
    if not isinstance(raw, dict):
        raw = {}
    links: dict[str, list] = {
        "report_timestamps": [],
        "redmine_issue_ids": [],
        "gerrit_change_ids": [],
    }
    for key in links:
        value = raw.get(key)
        if isinstance(value, list):
            cleaned: list = []
            for v in value:
                if v is None:
                    continue
                # 数字保持数字（issue_id/change_id 可能为 int），其余保持原样。
                if isinstance(v, (int, float)):
                    cleaned.append(int(v) if isinstance(v, float) and v.is_integer() else v)
                elif str(v) != "":
                    cleaned.append(v)
            links[key] = cleaned
    return json.dumps(links, ensure_ascii=False)


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
                    raw_content,
                    tags,
                    summary,
                    keywords
                );
                """
            )
            # 非破坏性加列：老库（缺 links / related_module）自动补列，原数据填默认值。
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(notes)")}
            if "links" not in existing:
                conn.execute("ALTER TABLE notes ADD COLUMN links TEXT DEFAULT '{}'")
            if "related_module" not in existing:
                conn.execute("ALTER TABLE notes ADD COLUMN related_module TEXT DEFAULT ''")
            # FTS 表升级：老 FTS 缺 raw_content 列时重建并回填，确保大文档全文可搜。
            # FTS5 虚拟表不支持 ALTER，只能 DROP + CREATE + 重新索引。
            self._upgrade_fts_schema(conn)

    @staticmethod
    def _upgrade_fts_schema(conn: sqlite3.Connection) -> None:
        """老 FTS 表（无 raw_content 列）幂等升级为含 raw_content 的新 schema 并回填。"""
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='notes_fts'"
        ).fetchone()
        if not row:
            return  # 新库已用新 schema 建好
        sql_text = row["sql"] or ""
        if "raw_content" in sql_text:
            return  # 已是新 schema
        logger.info("[Notes] 升级 FTS schema：加入 raw_content 列并回填全文索引")
        conn.execute("DROP TABLE IF EXISTS notes_fts")
        conn.execute(
            """
            CREATE VIRTUAL TABLE notes_fts USING fts5(
                note_id UNINDEXED,
                title,
                content,
                raw_content,
                tags,
                summary,
                keywords
            )
            """
        )
        # 回填：把现有 notes 重新写入 FTS（含 raw_content 全文）。
        rows = conn.execute(
            "SELECT note_id, title, content, raw_content, tags, summary, keywords FROM notes"
        ).fetchall()
        for r in rows:
            conn.execute(
                """INSERT INTO notes_fts
                   (note_id, title, content, raw_content, tags, summary, keywords)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["note_id"],
                    r["title"] or "",
                    r["content"] or "",
                    r["raw_content"] or "",
                    r["tags"] or "",
                    r["summary"] or "",
                    r["keywords"] or "",
                ),
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
            "links": _normalize_links(payload.get("links")),
            "related_module": (payload.get("related_module") or "").strip(),
            "created_at": payload.get("created_at") or now,
            "updated_at": payload.get("updated_at") or now,
        }
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO notes (note_id, user_id, notebook, title, content,
                   raw_content, source, source_file, tags, summary, keywords,
                   links, related_module, created_at, updated_at)
                   VALUES (:note_id, :user_id, :notebook, :title, :content,
                   :raw_content, :source, :source_file, :tags, :summary, :keywords,
                   :links, :related_module, :created_at, :updated_at)""",
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
        allowed = ["notebook", "title", "content", "tags", "summary", "keywords", "related_module"]
        # links 单独规范化，不直接拼进 SET 占位符。
        fields = [f for f in allowed if f in updates]
        if "links" in updates:
            fields.append("links")
        if not fields:
            return self.get_note(user_id, note_id)
        sets = ", ".join(f"{f} = ?" for f in fields) + ", updated_at = ?"
        values: list[Any] = []
        for f in fields:
            values.append(_normalize_links(updates[f]) if f == "links" else updates[f])
        values += [_now(), note_id, user_id]
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
        if cur.rowcount > 0:
            # 清理上传原件目录（按约定路径 <upload_dir>/<user_id>/<note_id>/），避免孤儿堆积。
            self._cleanup_upload_dir(user_id, note_id)
        return cur.rowcount > 0

    def _cleanup_upload_dir(self, user_id: str, note_id: str) -> None:
        import shutil

        target = self.upload_dir / user_id / note_id
        try:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
        except Exception as exc:  # 清理失败不影响删除结果
            logger.debug("[Notes] 清理上传目录失败 %s: %s", target, exc)

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
            user_notebooks = {r["name"]: dict(r) for r in rows}

        # 合并预置分类：预置项置顶（count=0 也显示），用户自建项随后，去重。
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in PRESET_NOTEBOOKS:
            seen.add(name)
            merged.append({"name": name, "count": user_notebooks.get(name, {"count": 0})["count"]})
        for name, nb in user_notebooks.items():
            if name not in seen:
                seen.add(name)
                merged.append(nb)
        return merged

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
        fields = ("title", "content", "raw_content", "tags", "summary", "keywords", "related_module")
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
                """INSERT INTO notes_fts
                   (note_id, title, content, raw_content, tags, summary, keywords)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    payload.get("note_id"),
                    payload.get("title") or "",
                    payload.get("content") or "",
                    payload.get("raw_content") or "",
                    payload.get("tags") or "",
                    payload.get("summary") or "",
                    payload.get("keywords") or "",
                ),
            )
        except sqlite3.OperationalError as exc:
            # FTS 写失败不应阻断笔记写入，但要留日志便于排查索引不一致。
            logger.warning("[Notes] FTS 索引写入失败 note_id=%s: %s", payload.get("note_id"), exc)
