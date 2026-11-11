"""邮件发件台账（outbox ledger）：每封出站邮件一行，可追溯。

三条发送链路（手动/API、自动化流水线通知、运维告警）统一在
:func:`features.email.service.send_email` 内收口写一行，记录时间、
触发通道与主体、收件人、主题、结果。正文只存 500 字符预览（ traceback
与附件不落库），台账用于审计与排障，不是邮件重发队列。

存储位置 ``data/email/outbox.sqlite3``；迁移遵循仓库硬规则：
``BEGIN IMMEDIATE`` 写锁串行化建表，语句幂等，Web/Worker/CLI 多进程
重复执行安全。超过 ``_MAX_ROWS`` 后按时间滚动裁剪，台账不无限增长。
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foundation.config import settings


# 正文预览长度：足够辨认是哪封邮件，不足以成为第二个正文存储。
_BODY_PREVIEW_CHARS = 500
_MAX_ROWS = 5000
_PRUNE_BATCH = 500


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EmailOutboxStore:
    _REQUIRED_TABLES = frozenset({"email_outbox"})

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._schema_lock = threading.RLock()
        self._init_schema()

    def _open_connection(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
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
            self._init_schema()
            conn = self._open_connection()
        return conn

    def _init_schema(self) -> None:
        # 进程安全（仓库硬规则）：BEGIN IMMEDIATE 串行化建表迁移，语句幂等。
        with self._schema_lock, self._open_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS email_outbox (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'unknown',
                    owner_id TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    ref_type TEXT NOT NULL DEFAULT '',
                    ref_id TEXT NOT NULL DEFAULT '',
                    from_addr TEXT NOT NULL DEFAULT '',
                    to_addrs TEXT NOT NULL DEFAULT '[]',
                    cc_addrs TEXT NOT NULL DEFAULT '[]',
                    subject TEXT NOT NULL DEFAULT '',
                    body_preview TEXT NOT NULL DEFAULT '',
                    is_html INTEGER NOT NULL DEFAULT 0,
                    attachments TEXT NOT NULL DEFAULT '[]',
                    sent INTEGER NOT NULL DEFAULT 0,
                    mode TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_email_outbox_created "
                "ON email_outbox(created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_email_outbox_owner "
                "ON email_outbox(owner_id, created_at DESC)"
            )
            conn.execute("PRAGMA user_version = 1")

    def record(self, entry: dict[str, Any]) -> str:
        """Insert one ledger row; never raises to the sender.

        写台账失败绝不能影响发送结果（send_email 已把本方法包在
        try/except 里），因此任何数据库错误都就地降级为返回空 id。
        """
        email_id = "email-" + uuid.uuid4().hex[:12]
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """INSERT INTO email_outbox
                       (id, created_at, channel, owner_id, username,
                        ref_type, ref_id, from_addr, to_addrs, cc_addrs,
                        subject, body_preview, is_html, attachments,
                        sent, mode, error)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        email_id,
                        utc_now_iso(),
                        str(entry.get("channel") or "unknown"),
                        str(entry.get("owner_id") or ""),
                        str(entry.get("username") or ""),
                        str(entry.get("ref_type") or ""),
                        str(entry.get("ref_id") or ""),
                        str(entry.get("from_addr") or ""),
                        _as_json(entry.get("to_addrs")),
                        _as_json(entry.get("cc_addrs")),
                        str(entry.get("subject") or ""),
                        str(entry.get("body_preview") or "")[:_BODY_PREVIEW_CHARS],
                        1 if entry.get("is_html") else 0,
                        _as_json(entry.get("attachments")),
                        1 if entry.get("sent") else 0,
                        str(entry.get("mode") or ""),
                        str(entry.get("error") or ""),
                    ),
                )
            self._prune_if_needed()
        except Exception:
            # mkdir/磁盘/权限等非 sqlite3 错误同样不允许外泄（OSError 继承
            # 自 Exception 而非 sqlite3.Error）；台账失败降级为不记录。
            return ""
        return email_id

    def _prune_if_needed(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """DELETE FROM email_outbox WHERE id IN (
                           SELECT id FROM email_outbox
                           ORDER BY created_at DESC
                           LIMIT -1 OFFSET ?
                       )""",
                    (_MAX_ROWS,),
                )
        except Exception:
            # 裁剪是维护动作：任何失败（含 OSError）都不影响主流程。
            pass

    def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        channel: str = "",
        sent: bool | None = None,
        owner_id: str | None = None,
        query: str = "",
    ) -> dict[str, Any]:
        """Newest-first page of ledger rows.

        ``owner_id`` 传 ``None`` 表示不过滤（管理员/服务端内部使用）；
        传字符串则只返回该资源所有者的记录（owner 视图，ADR 0010）。
        ``query`` 对收件人 JSON 与主题做大小写不敏感的子串匹配。
        """
        limit = max(1, min(int(limit or 50), 200))
        offset = max(0, int(offset or 0))
        clauses: list[str] = []
        params: list[Any] = []
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        if sent is not None:
            clauses.append("sent = ?")
            params.append(1 if sent else 0)
        if owner_id is not None:
            # owner 视图只返回该资源所有者的记录；系统级记录（owner_id=''，
            # ops 运维告警等）是面向运维的全局告警，不进入普通用户的历史
            # 邮件列表，由 admin_view 全量视图查看。
            clauses.append("owner_id = ?")
            params.append(owner_id)
        if query:
            # LIKE 的通配符（%/_)按字面匹配：手工转义并显式 ESCAPE。
            needle = "%" + query.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
            clauses.append(
                "(subject LIKE ? ESCAPE '\\' OR to_addrs LIKE ? ESCAPE '\\'"
                " OR username LIKE ? ESCAPE '\\')"
            )
            params.extend([needle, needle, needle])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM email_outbox {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"""SELECT * FROM email_outbox {where}
                    ORDER BY created_at DESC, id DESC
                    LIMIT ? OFFSET ?""",
                [*params, limit, offset],
            ).fetchall()
        return {"total": int(total), "items": [_row_to_dict(row) for row in rows]}

    def get(self, email_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM email_outbox WHERE id = ?", (email_id,)
            ).fetchone()
        return _row_to_dict(row) if row is not None else None


def _as_json(value: Any) -> str:
    import json

    items = value if isinstance(value, list) else []
    return json.dumps([str(item) for item in items], ensure_ascii=False)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    import json

    def _loads(raw: Any) -> list[str]:
        try:
            parsed = json.loads(str(raw or "[]"))
            return parsed if isinstance(parsed, list) else []
        except ValueError:
            return []

    return {
        "id": str(row["id"]),
        "created_at": str(row["created_at"]),
        "channel": str(row["channel"]),
        "owner_id": str(row["owner_id"]),
        "username": str(row["username"]),
        "ref_type": str(row["ref_type"]),
        "ref_id": str(row["ref_id"]),
        "from_addr": str(row["from_addr"]),
        "to_addrs": _loads(row["to_addrs"]),
        "cc_addrs": _loads(row["cc_addrs"]),
        "subject": str(row["subject"]),
        "body_preview": str(row["body_preview"]),
        "is_html": bool(row["is_html"]),
        "attachments": _loads(row["attachments"]),
        "sent": bool(row["sent"]),
        "mode": str(row["mode"]),
        "error": str(row["error"]),
    }


_STORE: EmailOutboxStore | None = None
_STORE_LOCK = threading.Lock()


def get_email_outbox_store() -> EmailOutboxStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = EmailOutboxStore(settings.data_root / "email" / "outbox.sqlite3")
        return _STORE


def reset_email_outbox_store() -> None:
    """Test seam: drop the process singleton so tests can isolate storage."""
    global _STORE
    with _STORE_LOCK:
        _STORE = None
