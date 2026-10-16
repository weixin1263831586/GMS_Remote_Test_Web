"""Process-safe, finite debug grants and serialized operation audit records."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from foundation.error_model import ApiError
from foundation.sqlite_schema import ensure_schema


class DebugStore:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.db = root / "debug.sqlite3"
        self.connect().close()

    @staticmethod
    def _initialize_schema(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS debug_sessions (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                status TEXT NOT NULL, expires_at REAL NOT NULL, busy TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL
            )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS debug_operations (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, action TEXT NOT NULL,
                started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
                details TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}'
            )""")
        conn.execute('PRAGMA user_version = 1')

    def connect(self):
        conn = sqlite3.connect(self.db, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            ensure_schema(conn, self.db, self._initialize_schema)
        except Exception:
            conn.close()
            raise
        return conn

    @staticmethod
    def decode(row):
        if row is None:
            raise ApiError.not_found("调试会话不存在")
        payload = {**json.loads(row["payload"]), "status": row["status"],
                   "expires_at": row["expires_at"], "busy": row["busy"]}
        if payload["status"] in {"pending", "active", "approving"} and payload["expires_at"] <= time.time():
            payload["status"] = "expired"
        return payload

    def get(self, session_id, owner_id):
        with self.connect() as conn:
            return self.decode(conn.execute("SELECT * FROM debug_sessions WHERE id=? AND owner_id=?",
                                            (session_id, owner_id)).fetchone())

    def create(self, payload):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute("SELECT count(*) FROM debug_sessions WHERE owner_id=? "
                                 "AND status IN ('pending','approving','active') AND expires_at>?",
                                 (payload["owner_id"], time.time())).fetchone()[0]
            if count >= 5:
                raise ApiError.conflict("已有 5 个待审批或有效的调试会话")
            conn.execute("INSERT INTO debug_sessions (id,owner_id,actor_id,status,expires_at,payload) "
                         "VALUES (?,?,?,'pending',?,?)", (payload["id"], payload["owner_id"],
                         payload["actor_id"], payload["expires_at"], json.dumps(payload)))
        return self.get(payload["id"], payload["owner_id"])

    def reserve_approval(self, session_id, owner_id):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("UPDATE debug_sessions SET status='approving' WHERE id=? AND owner_id=? "
                            "AND status='pending' AND expires_at>?", (session_id, owner_id, time.time())).rowcount != 1:
                raise ApiError.conflict("调试会话已审批或已过期")

    def activate(self, session_id, owner_id, updates):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            payload = self.decode(conn.execute("SELECT * FROM debug_sessions WHERE id=? AND owner_id=?",
                                               (session_id, owner_id)).fetchone())
            if payload["status"] != "approving":
                raise ApiError.conflict("调试审批已过期或撤销")
            payload.update(updates)
            conn.execute("UPDATE debug_sessions SET status='active',expires_at=?,payload=? WHERE id=?",
                         (payload["expires_at"], json.dumps(payload), session_id))
        return self.get(session_id, owner_id)

    def approval_heartbeat(self, session_id):
        with self.connect() as conn:
            changed = conn.execute("UPDATE debug_sessions SET expires_at=? "
                                   "WHERE id=? AND status='approving' AND expires_at>?",
                                   (time.time() + 120, session_id, time.time())).rowcount
        if changed != 1:
            raise ApiError.conflict("调试审批租约失效，请重新申请")

    def reset_approval(self, session_id, owner_id):
        with self.connect() as conn:
            conn.execute("UPDATE debug_sessions SET status='pending' WHERE id=? AND owner_id=? AND status='approving'",
                         (session_id, owner_id))

    def revoke(self, session_id, owner_id):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.decode(conn.execute("SELECT * FROM debug_sessions WHERE id=? AND owner_id=?",
                                     (session_id, owner_id)).fetchone())
            conn.execute("UPDATE debug_sessions SET status='revoked' WHERE id=? AND owner_id=?", (session_id, owner_id))
        return self.get(session_id, owner_id)

    def begin(self, session_id, owner_id, actor_id, action, details):
        op_id = uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            payload = self.decode(conn.execute("SELECT * FROM debug_sessions WHERE id=? AND owner_id=?",
                                               (session_id, owner_id)).fetchone())
            if payload["actor_id"] != actor_id:
                raise ApiError.forbidden("调试会话绑定到另一个 Agent")
            if payload["status"] != "active":
                raise ApiError.forbidden("调试会话未批准、已到期或已撤销",
                                         next_actions=({"action": "重新申请并由用户批准调试会话"},))
            if action not in payload["operations"]:
                raise ApiError.forbidden("该操作不在已批准的调试范围内")
            if payload["busy"]:
                raise ApiError.conflict("调试会话正在执行操作，请先查询状态")
            conn.execute("UPDATE debug_sessions SET busy=? WHERE id=?", (op_id, session_id))
            conn.execute("INSERT INTO debug_operations (id,session_id,action,started_at,status,details) "
                         "VALUES (?,?,?,?,'running',?)", (op_id, session_id, action, time.time(), json.dumps(details)))
        return op_id

    def finish(self, session_id, op_id, status, result):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE debug_operations SET status=?,finished_at=?,result=? "
                         "WHERE id=? AND session_id=? AND status='running'",
                         (status, time.time(), json.dumps(result), op_id, session_id))
            conn.execute("UPDATE debug_sessions SET busy='' WHERE id=? AND busy=?", (session_id, op_id))

    def history(self, session_id, owner_id):
        self.get(session_id, owner_id)
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM debug_operations WHERE session_id=? ORDER BY started_at DESC LIMIT 20",
                                (session_id,)).fetchall()
        return [{**dict(r), "details": json.loads(r["details"]), "result": json.loads(r["result"])} for r in rows]
