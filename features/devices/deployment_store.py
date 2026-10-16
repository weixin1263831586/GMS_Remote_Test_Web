"""Durable, owner-scoped APK approval requests; credentials never enter storage."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from foundation.error_model import ApiError
from foundation.sqlite_schema import ensure_schema


class DeploymentStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.db = root / "requests.sqlite3"
        self.connect().close()

    @staticmethod
    def _initialize_schema(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS deployments (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                status TEXT NOT NULL, expires_at REAL NOT NULL,
                payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
                execution_token TEXT NOT NULL DEFAULT ''
            )""")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(deployments)")}
        if "execution_token" not in columns:
            conn.execute("ALTER TABLE deployments ADD COLUMN execution_token TEXT NOT NULL DEFAULT ''")
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

    def create(self, payload: dict):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute(
                "SELECT count(*) FROM deployments WHERE owner_id=? "
                "AND status IN ('pending', 'queued', 'running') AND expires_at>?",
                (payload["owner_id"], time.time()),
            ).fetchone()[0]
            if count >= 5:
                raise ApiError.conflict("已有 5 个待审批或正在执行的 APK 部署请求")
            conn.execute("INSERT INTO deployments (id,owner_id,status,expires_at,payload,result) "
                         "VALUES (?,?,'pending',?,?, '{}')", (
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
        if result["status"] in {"queued", "running"} and row["expires_at"] <= time.time():
            prior_state = row["status"]
            error = ApiError.dependency_timeout(
                "部署执行租约已失效；请核实设备包名、签名及 APK SHA256，禁止盲目重放安装",
            ).envelope()
            error["execution_phase"] = prior_state
            with self.connect() as conn:
                conn.execute("UPDATE deployments SET status='interrupted',result=? "
                             "WHERE id=? AND status IN ('queued','running') AND expires_at<=?",
                             (json.dumps({**result["result"], **error}), request_id, time.time()))
            # A concurrent heartbeat or completion may have won the update.
            return self.get(request_id, owner_id)
        return result

    def start(self, request_id: str, owner_id: str, approved_by: str):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE deployments SET status='queued', result=?, expires_at=? "
                "WHERE id=? AND owner_id=? AND status='pending' AND expires_at>?",
                (json.dumps({"approved_by": approved_by, "started_at": time.time()}),
                 time.time() + 120, request_id, owner_id, time.time()),
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
                             "WHERE id=? AND owner_id=? AND status='queued'",
                             (json.loads(row["payload"])["expires_at"], request_id, owner_id))

    def begin_execution(self, request_id, execution_token=""):
        expired = False
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute("UPDATE deployments SET status='running',expires_at=?,execution_token=? "
                                   "WHERE id=? AND status='queued' AND expires_at>?",
                                   (time.time() + 120, execution_token, request_id, time.time())).rowcount
            if changed != 1:
                row = conn.execute("SELECT status,expires_at,result FROM deployments WHERE id=?",
                                   (request_id,)).fetchone()
                if row and row["status"] == "queued" and row["expires_at"] <= time.time():
                    error = ApiError.dependency_timeout(
                        "审批后的部署排队超时；请核实设备状态后重新提交，禁止盲目重放安装",
                    ).envelope()
                    error["execution_phase"] = "queued"
                    conn.execute("UPDATE deployments SET status='interrupted',result=?,execution_token=? "
                                 "WHERE id=? AND status='queued' AND expires_at<=?",
                                 (json.dumps({**json.loads(row["result"]), **error}), execution_token,
                                  request_id, time.time()))
                    expired = True
        if changed != 1:
            if expired:
                return False
            raise ApiError.conflict("部署已启动或执行租约失效，禁止重放安装")
        return True

    def heartbeat(self, request_id, execution_token=None):
        token_clause = " AND execution_token=?" if execution_token is not None else ""
        params = (time.time() + 120, request_id, time.time())
        if execution_token is not None:
            params += (execution_token,)
        with self.connect() as conn:
            changed = conn.execute("UPDATE deployments SET expires_at=? "
                                   "WHERE id=? AND status='running' AND expires_at>?" + token_clause,
                                   params).rowcount
        if changed != 1:
            raise ApiError.conflict("部署执行租约失效，请核对设备状态")

    def approval_heartbeat(self, request_id):
        with self.connect() as conn:
            changed = conn.execute("UPDATE deployments SET expires_at=? "
                                   "WHERE id=? AND status='queued' AND expires_at>?",
                                   (time.time() + 120, request_id, time.time())).rowcount
        if changed != 1:
            raise ApiError.conflict("部署审批租约失效，请重新审批")

    def record_precheck(self, request_id, metadata):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT result FROM deployments WHERE id=? AND status='queued'",
                               (request_id,)).fetchone()
            if not row:
                raise ApiError.conflict("部署请求已开始，无法记录审批前身份核验")
            result = {**json.loads(row["result"]), "installed_identity_precheck": metadata}
            conn.execute("UPDATE deployments SET result=? WHERE id=? AND status='queued'",
                         (json.dumps(result), request_id))

    def reconcile(self, request_id, owner_id, result, *, completed=False):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT result FROM deployments WHERE id=? AND owner_id=? AND status='interrupted'",
                               (request_id, owner_id)).fetchone()
            if row:
                previous = json.loads(row["result"])
                if completed:
                    previous.pop("error", None)
                    previous.pop("code", None)
                conn.execute("UPDATE deployments SET status=?,result=? WHERE id=? AND status='interrupted'",
                             ("completed" if completed else "interrupted", json.dumps({**previous, **result}), request_id))

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

    def fail_start(self, request_id, execution_token, result):
        """Claim cleanup only when no competing executor owns the running row."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute("UPDATE deployments SET status='failed',execution_token=?,result=? "
                                   "WHERE id=? AND status='queued'",
                                   (execution_token, json.dumps(result), request_id)).rowcount
            if changed:
                return True
            row = conn.execute("SELECT status,execution_token FROM deployments WHERE id=?",
                               (request_id,)).fetchone()
            if row and row["status"] == "interrupted" and not row["execution_token"]:
                changed = conn.execute("UPDATE deployments SET execution_token=? WHERE id=? "
                                       "AND status='interrupted' AND execution_token=''",
                                       (execution_token, request_id)).rowcount
                return changed == 1
            return bool(row and row["status"] == "running" and row["execution_token"] == execution_token)

    def claim_expired_queue_cleanup(self, request_id, owner_id, execution_token):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT result FROM deployments WHERE id=? AND owner_id=? "
                               "AND status='interrupted' AND execution_token=''",
                               (request_id, owner_id)).fetchone()
            if not row or json.loads(row["result"]).get("execution_phase") != "queued":
                return False
            return conn.execute("UPDATE deployments SET execution_token=? WHERE id=? "
                                "AND status='interrupted' AND execution_token=''",
                                (execution_token, request_id)).rowcount == 1

    def finish(self, request_id: str, status: str, result: dict, execution_token=None):
        with self.connect() as conn:
            row = conn.execute("SELECT result FROM deployments WHERE id=?", (request_id,)).fetchone()
            prior = json.loads(row["result"]) if row else {}
            token_clause = " AND execution_token=?" if execution_token is not None else ""
            params = (status, json.dumps({**prior, **result}), request_id)
            if execution_token is not None:
                params += (execution_token,)
            conn.execute("UPDATE deployments SET status=?, result=? WHERE id=? "
                         "AND status IN ('queued','running')" + token_clause, params)
