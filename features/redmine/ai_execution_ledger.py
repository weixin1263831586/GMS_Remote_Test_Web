"""AI Execution Ledger：AI 逻辑调用账本（receipt）。

Daily Brief 已有 per-attempt 的执行轨迹审计
（``daily_brief_repository.record_ai_execution``，事后追加），但它回答不了
治理层面的问题：

- 同一逻辑调用（同一 issue + 同一证据快照 + 同一 prompt/analyzer/model）
  会不会因为页面重复点击 / job lease 丢失后的 worker 重试，而**重复发送
  第二次 AI 请求**？
- 进程在 AI 调用中途被杀死 / 断电 / 取消时，这次调用的结果处于
  **不确定态（unknown）**，而不是干净的 failed——后续重试必须显式知道
  前一次 outcome 未知。

本模块借鉴 AIHOT 的 execution receipt 状态机，给出唯一答案：

    pending ──► received ──► completed
       │            │
       │            ├──► failed
       │            └──► unknown
       └──► completed / failed / unknown

- ``logical_key`` = sha256(owner, issue, input_hash, prompt_version,
  analyzer_version, model)。同一逻辑键存在活跃 receipt（pending/received）
  时 ``begin()`` 返回 ``duplicate=True``，调用方不得重复发送。
- pending/received 的 receipt 有租约上限（``RECEIPT_LEASE_SECONDS``）：
  超时视为持有进程已死，置为 unknown 并允许新建 receipt，避免僵尸
  pending 永久阻塞重试。
- unknown 是**终态**：不允许改写，只允许新建 receipt（携带递增 attempt）。
  "诊断不设预算"（ADR 0013 相关决策）不受影响——本账本是操作安全层，
  不截断任何一次分析的取证深度。

存储：随 per-owner ``daily_brief.sqlite3``；建表用 ``BEGIN IMMEDIATE``
（进程安全/idempotent，Web/Worker/CLI 并发迁移安全）。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import threading
import uuid
from typing import Any

from foundation.database import connect_sqlite

from .users import _now


logger = logging.getLogger(__name__)

#: receipt 活跃租约：超过该时长的 pending/received 视为持有进程已死亡。
RECEIPT_LEASE_SECONDS = 4 * 3600

STATUS_PENDING = "pending"
STATUS_RECEIVED = "received"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"

_TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_UNKNOWN)
_ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RECEIVED)

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS redmine_ai_execution_receipts ("
    " receipt_id TEXT PRIMARY KEY,"
    " logical_key TEXT NOT NULL,"
    " owner_id TEXT NOT NULL,"
    " purpose TEXT NOT NULL,"
    " subject TEXT NOT NULL DEFAULT '',"
    " issue_id INTEGER NOT NULL DEFAULT 0,"
    " provider TEXT NOT NULL DEFAULT '',"
    " model TEXT NOT NULL DEFAULT '',"
    " prompt_version TEXT NOT NULL DEFAULT '',"
    " analyzer_version TEXT NOT NULL DEFAULT '',"
    " input_hash TEXT NOT NULL DEFAULT '',"
    " status TEXT NOT NULL,"
    " attempt INTEGER NOT NULL DEFAULT 1,"
    " session_id TEXT NOT NULL DEFAULT '',"
    " provider_request_id TEXT NOT NULL DEFAULT '',"
    " usage_json TEXT NOT NULL DEFAULT '',"
    " latency_ms INTEGER,"
    " error TEXT NOT NULL DEFAULT '',"
    " created_at TEXT NOT NULL,"
    " received_at TEXT NOT NULL DEFAULT '',"
    " finished_at TEXT NOT NULL DEFAULT '',"
    " updated_at TEXT NOT NULL"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_ai_receipts_logical "
    "ON redmine_ai_execution_receipts(owner_id, logical_key, status)",
)


def logical_key(*parts: Any) -> str:
    """稳定派生逻辑调用键；任何输入变化都会产生新键。"""
    joined = "\x00".join(str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _lease_expired(row: dict[str, Any], now: str) -> bool:
    """活跃 receipt 是否超出租约（ISO 时间字符串字典序比较）。"""
    started = str(row.get("updated_at") or row.get("created_at") or "")
    if not started:
        return False
    return started < _lease_cutoff(now)


def _lease_cutoff(now: str) -> str:
    moment = datetime.datetime.fromisoformat(now)
    cutoff = moment - datetime.timedelta(seconds=RECEIPT_LEASE_SECONDS)
    return cutoff.isoformat()


class AIExecutionLedger:
    """per-owner SQLite 账本。线程安全；所有写入走 BEGIN IMMEDIATE。"""

    def __init__(self, db_path: Any):
        self.db_path = db_path
        self._lock = threading.Lock()

    # ------------------------------------------------------------- internals

    def _connect(self):
        return connect_sqlite(self.db_path)

    # ------------------------------------------------------------- lifecycle

    def begin(
        self,
        *,
        owner_id: str,
        purpose: str,
        issue_id: int,
        subject: str = "",
        provider: str = "kkagent",
        model: str = "",
        prompt_version: str = "",
        analyzer_version: str = "",
        input_hash: str = "",
    ) -> dict[str, Any]:
        """登记一次逻辑 AI 调用；活跃 receipt 已存在时返回 duplicate。

        返回的 dict 含 ``duplicate`` 布尔位：True 时调用方**不得**再次
        发送 AI 请求（防重复发送），并应把 ``receipt_id`` 记入日志。
        """
        key = logical_key(
            owner_id, issue_id, input_hash, prompt_version, analyzer_version, model
        )
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA:
                conn.execute(statement)
            active = conn.execute(
                "SELECT * FROM redmine_ai_execution_receipts "
                "WHERE owner_id=? AND logical_key=? AND status IN (?, ?) "
                "ORDER BY created_at DESC LIMIT 1",
                (owner_id, key, STATUS_PENDING, STATUS_RECEIVED),
            ).fetchone()
            if active is not None:
                row = dict(active)
                if _lease_expired(row, now):
                    # 租约过期：持有进程已死。置 unknown（不确定态）并
                    # 继续新建，绝不复用僵尸 receipt。
                    self._transition(
                        conn, str(row["receipt_id"]), STATUS_UNKNOWN,
                        error=f"lease expired after {RECEIPT_LEASE_SECONDS}s",
                    )
                else:
                    row["duplicate"] = True
                    return row
            attempt_row = conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) AS n FROM redmine_ai_execution_receipts "
                "WHERE owner_id=? AND logical_key=?",
                (owner_id, key),
            ).fetchone()
            attempt = int(attempt_row["n"]) + 1 if attempt_row is not None else 1
            receipt_id = "receipt_" + uuid.uuid4().hex
            conn.execute(
                "INSERT INTO redmine_ai_execution_receipts "
                "(receipt_id, logical_key, owner_id, purpose, subject, issue_id, "
                " provider, model, prompt_version, analyzer_version, input_hash, "
                " status, attempt, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt_id, key, owner_id, purpose, subject, int(issue_id),
                    provider, model, prompt_version, analyzer_version, input_hash,
                    STATUS_PENDING, attempt, now, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM redmine_ai_execution_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            result = dict(row) if row is not None else {"receipt_id": receipt_id}
            result["duplicate"] = False
            return result

    def mark_received(
        self, receipt_id: str, *, session_id: str = "", provider_request_id: str = ""
    ) -> None:
        """provider 已接受请求（pending → received）。终态后调用是 no-op 日志。"""
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = self._transition(
                conn, receipt_id, STATUS_RECEIVED,
                session_id=session_id,
                provider_request_id=provider_request_id,
                received_at=now,
                allowed_from=(STATUS_PENDING,),
            )
            if not changed:
                logger.info("receipt %s cannot move to received (terminal or missing)", receipt_id)

    def finish(
        self,
        receipt_id: str,
        *,
        ok: bool,
        error: str = "",
        usage: dict[str, Any] | None = None,
        latency_ms: int | None = None,
        session_id: str = "",
    ) -> None:
        """确定终态（completed / failed）。unknown/completed/failed 不可改写。"""
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = self._transition(
                conn, receipt_id,
                STATUS_COMPLETED if ok else STATUS_FAILED,
                error=str(error or ""),
                usage_json=json.dumps(usage or {}, ensure_ascii=False, sort_keys=True),
                latency_ms=int(latency_ms) if latency_ms is not None else None,
                session_id=session_id,
                finished_at=now,
            )
            if not changed:
                logger.info("receipt %s already terminal; keeping first outcome", receipt_id)

    def mark_unknown(self, receipt_id: str, *, reason: str) -> None:
        """调用结果不确定（崩溃/取消/断电）时的终态。不可改写为其他终态。"""
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = self._transition(
                conn, receipt_id, STATUS_UNKNOWN,
                error=str(reason or "")[:1000],
                finished_at=now,
            )
            if not changed:
                logger.info("receipt %s already terminal; unknown not applied", receipt_id)

    # ------------------------------------------------------------- queries

    def get(self, receipt_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM redmine_ai_execution_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def latest_for_key(self, owner_id: str, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM redmine_ai_execution_receipts "
                "WHERE owner_id=? AND logical_key=? "
                "ORDER BY created_at DESC, attempt DESC LIMIT 1",
                (owner_id, key),
            ).fetchone()
            return dict(row) if row is not None else None

    def stats(self, owner_id: str) -> dict[str, Any]:
        """治理视图：按状态计数 + 累计 usage（供后续 provider 预算扩展）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM redmine_ai_execution_receipts "
                "WHERE owner_id=? GROUP BY status",
                (owner_id,),
            ).fetchall()
            by_status = {str(row["status"]): int(row["n"]) for row in rows}
        return {
            "by_status": by_status,
            "total": sum(by_status.values()),
            "unknown": by_status.get(STATUS_UNKNOWN, 0),
        }

    # ------------------------------------------------------------- transition

    def _transition(
        self,
        conn: Any,
        receipt_id: str,
        to_status: str,
        *,
        allowed_from: tuple[str, ...] = _ACTIVE_STATUSES,
        **fields: Any,
    ) -> bool:
        columns = {
            "status": to_status,
            "updated_at": _now(),
        }
        for name, value in fields.items():
            if value is not None:
                columns[name] = value
        assignments = ", ".join(f"{name}=?" for name in columns)
        cursor = conn.execute(
            f"UPDATE redmine_ai_execution_receipts SET {assignments} "
            f"WHERE receipt_id=? AND status IN ({', '.join('?' * len(allowed_from))})",
            [*columns.values(), receipt_id, *allowed_from],
        )
        return cursor.rowcount == 1

