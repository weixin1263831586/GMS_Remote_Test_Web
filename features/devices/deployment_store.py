"""Durable, owner-scoped APK approval requests; credentials never enter storage."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from foundation.error_model import ApiError


class DeploymentStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.db = root / "requests.sqlite3"
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE IF NOT EXISTS deployments (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                status TEXT NOT NULL, expires_at REAL NOT NULL,
                payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}'
            )""")

    def connect(self):
        conn = sqlite3.connect(self.db, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def create(self, payload: dict):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute(
                "SELECT count(*) FROM deployments WHERE owner_id=? "
                "AND status IN ('pending', 'running') AND expires_at>?",
                (payload["owner_id"], time.time()),
            ).fetchone()[0]
            if count >= 5:
                raise ApiError.conflict("已有 5 个待审批或正在执行的 APK 部署请求")
            conn.execute("INSERT INTO deployments VALUES (?,?,'pending',?,?, '{}')", (
                payload["id"], payload["owner_id"], payload["expires_at"], json.dumps(payload),
            ))
        return self.get(payload["id"], payload["owner_id"])

    def get(self, request_id: str, owner_id: str):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM deployments WHERE id=? AND owner_id=?", (request_id, owner_id),
            ).fetchone()
        if row is None:
            raise ApiError.not_found("APK 部署请求不存在")
        result = {**json.loads(row["payload"]), "status": row["status"],
                  "result": json.loads(row["result"])}
        if result["status"] == "pending" and result["expires_at"] <= time.time():
            result["status"] = "expired"
        if result["status"] == "running" and row["expires_at"] <= time.time():
            error = ApiError.dependency_timeout(
                "部署执行已中断或超时，请核对设备安装状态后重新提交",
            ).envelope()
            self.finish(request_id, "failed", error)
            result["status"] = "failed"
            result["result"].update(error)
        return result

    def start(self, request_id: str, owner_id: str, approved_by: str):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE deployments SET status='running', result=?, expires_at=? "
                "WHERE id=? AND owner_id=? AND status='pending' AND expires_at>?",
                (json.dumps({"approved_by": approved_by, "started_at": time.time()}),
                 time.time() + 1200, request_id, owner_id, time.time()),
            ).rowcount
            if changed != 1:
                raise ApiError.conflict("部署请求已审批、已过期或正在执行")

    def reset_before_execution(self, request_id: str, owner_id: str):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload FROM deployments WHERE id=? AND owner_id=?",
                               (request_id, owner_id)).fetchone()
            if row:
                conn.execute("UPDATE deployments SET status='pending', result='{}', expires_at=? "
                             "WHERE id=? AND owner_id=? AND status='running'",
                             (json.loads(row["payload"])["expires_at"], request_id, owner_id))

    def expire_pending(self):
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT id FROM deployments WHERE status='pending' AND expires_at<=?",
                                (now,)).fetchall()
            conn.execute("UPDATE deployments SET status='expired' WHERE status='pending' AND expires_at<=?",
                         (now,))
        return [row["id"] for row in rows]

    def discard_before_execution(self, request_id, owner_id):
        with self.connect() as conn:
            conn.execute("UPDATE deployments SET status='failed',result=? WHERE id=? AND owner_id=? AND status='pending'",
                         (json.dumps({"error": "调试授权校验失败，请重新提交"}), request_id, owner_id))

    def finish(self, request_id: str, status: str, result: dict):
        with self.connect() as conn:
            row = conn.execute("SELECT result FROM deployments WHERE id=?", (request_id,)).fetchone()
            prior = json.loads(row["result"]) if row else {}
            conn.execute("UPDATE deployments SET status=?, result=? WHERE id=? AND status='running'", (
                status, json.dumps({**prior, **result}), request_id,
            ))
