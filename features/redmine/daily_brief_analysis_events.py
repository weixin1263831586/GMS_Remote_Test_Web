"""Daily Brief 分析进度事件（append-only 时间线）。

独立于 DailyBriefRepository 的 run/issue 主表：分析执行期间由 Worker
进程追加结构化进度事件，Web 进程按 ``after_sequence`` 增量读取（Worker
与 Web 共享同一 per-owner SQLite，WAL 模式下跨进程立即可见）。「查看
分析」弹框据此从一次性 ⏳ 快照升级为实时执行时间线。

事件词表（AnalysisEvent，UI 唯一消费面，不暴露 kkagent 原始协议）：
``analysis_started`` / ``stage_changed`` / ``tool_started`` /
``tool_completed`` / ``tool_failed`` / ``progress`` /
``analysis_completed`` / ``analysis_failed``。

数据安全边界（与 kkagent/trace.py 的隐私约束一致）：
- 只保存 allowlist 字段：event_type/stage/tool_name/status/summary/
  duration_ms/created_at；
- 工具输出、模型 reasoning、完整 tool input 一律不落库；summary 只由
  :func:`describe_tool_call` 从身份字段（issue/device/query/path 等）
  构建，并经 :func:`scrub_secrets` 清洗。

迁移约束：本表不进入 ``DailyBriefRepository._init_db`` 的
``user_version`` 快路径，而是以整体幂等的
``CREATE TABLE/INDEX IF NOT EXISTS`` 自举，且全部 DDL 置于
``BEGIN IMMEDIATE`` 写锁内——Web/Worker/CLI 多进程并发初始化安全，
跨进程幂等（AGENTS.md 的 SQLite 迁移规则）。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .users import _now


logger = logging.getLogger(__name__)

# 进度事件保留天数：终态 run 的事件超期后由 Worker 启动时清理，避免
# 随分析次数无限增长（最终结论已完整保存在 runs/issues/ai_executions）。
ANALYSIS_EVENT_RETENTION_DAYS = 30

EVENT_TYPES = (
    "analysis_started",
    "stage_changed",
    "tool_started",
    "tool_completed",
    "tool_failed",
    "progress",
    "analysis_completed",
    "analysis_failed",
)

# summary 入库上限（描述性文本，不含任何工具输出正文）。
_SUMMARY_MAX_CHARS = 160

_SCHEMA_DDL = (
    """
    CREATE TABLE IF NOT EXISTS redmine_daily_brief_analysis_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        issue_id INTEGER NOT NULL,
        sequence INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        stage TEXT NOT NULL DEFAULT '',
        tool_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        duration_ms INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_brief_analysis_events_seq
    ON redmine_daily_brief_analysis_events(run_id, issue_id, sequence)
    """,
)

_EVENT_COLUMNS = (
    "sequence", "event_type", "stage", "tool_name", "status",
    "summary", "duration_ms", "created_at",
)

# summary 清洗：任何疑似凭据赋值片段（token=... / password: ...）整体
# 打码。进度 summary 理论上不含这些内容，这里做纵深防御。
_SECRET_PATTERN = re.compile(
    r"(?i)\b(token|password|passwd|secret|api[_-]?key|authorization|cookie)\b\s*[=:]\s*\S+"
)

# describe_tool_call 的输入身份字段 allowlist：只有这些 key 的值会进入
# 进度 summary（其余 input 内容——附件正文、快照参数全文等——不外显）。
# keywords 放在 path 前：codesearch 类检索的关键词比命中路径更能说明
# 「当前在查什么」。
_IDENTITY_INPUT_KEYS = (
    "issue_id", "issue", "device", "device_serial", "serial", "query", "q",
    "keywords", "path", "source", "revision", "artifact_id", "snapshot_id", "mode", "limit",
)


def scrub_secrets(text: str) -> str:
    """打码 summary 里疑似凭据的片段并截断到入库上限。"""
    cleaned = _SECRET_PATTERN.sub(r"\1=***", str(text or ""))
    return cleaned[:_SUMMARY_MAX_CHARS]


def describe_tool_call(tool_name: str, tool_input: Any) -> str:
    """从工具名 + 身份字段构建人类可读、可入库的调用摘要。"""
    label = str(tool_name or "").strip() or "tool"
    parts: list[str] = []
    input_map = tool_input if isinstance(tool_input, dict) else {}
    for key in _IDENTITY_INPUT_KEYS:
        value = input_map.get(key)
        if value is None or isinstance(value, (dict, list)):
            continue
        text = str(value).strip()
        if not text:
            continue
        parts.append(f"{key}={scrub_secrets(text)}")
        if len(parts) >= 3:
            break
    return scrub_secrets(f"{label}({', '.join(parts)})" if parts else label)


class DailyBriefAnalysisEventStore:
    """per-owner 库上的分析事件存储（线程安全，跨进程幂等自举）。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._ready = False

    # ---------------------------------------------------------------- schema

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """幂等 DDL，全部在 BEGIN IMMEDIATE 写锁内执行（见模块 docstring）。"""
        if self._ready:
            return
        conn.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA_DDL:
            conn.execute(statement)
        conn.commit()
        self._ready = True

    # ----------------------------------------------------------------- write

    def reset(self, run_id: str, issue_id: int) -> None:
        """清空一次 (run, issue) 的旧时间线：每次重分析从干净时间线开始。"""
        with self._lock, self._connect() as conn:
            self._ensure_schema(conn)
            conn.execute(
                "DELETE FROM redmine_daily_brief_analysis_events "
                "WHERE run_id=? AND issue_id=?",
                (run_id, int(issue_id)),
            )

    def append(self, run_id: str, issue_id: int, event: dict[str, Any]) -> int:
        """追加一条事件；返回单调递增的 sequence（从 1 开始）。"""
        event_type = str(event.get("event_type") or "")
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown analysis event type: {event_type}")
        with self._lock, self._connect() as conn:
            self._ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next "
                "FROM redmine_daily_brief_analysis_events "
                "WHERE run_id=? AND issue_id=?",
                (run_id, int(issue_id)),
            ).fetchone()
            sequence = int(row["next"])
            conn.execute(
                "INSERT INTO redmine_daily_brief_analysis_events "
                "(run_id, issue_id, sequence, event_type, stage, tool_name, "
                " status, summary, duration_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    int(issue_id),
                    sequence,
                    event_type,
                    scrub_secrets(event.get("stage", ""))[:64],
                    scrub_secrets(event.get("tool_name", ""))[:128],
                    scrub_secrets(event.get("status", ""))[:32],
                    scrub_secrets(event.get("summary", "")),
                    max(0, int(event.get("duration_ms") or 0)),
                    str(event.get("created_at") or _now()),
                ),
            )
            conn.commit()
            return sequence

    # ------------------------------------------------------------------ read

    def list_after(
        self, run_id: str, issue_id: int, *, after_sequence: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        """按 sequence 增量读取；轮询方携带上次返回的最大 sequence。"""
        with self._lock, self._connect() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                f"SELECT {', '.join(_EVENT_COLUMNS)} "
                "FROM redmine_daily_brief_analysis_events "
                "WHERE run_id=? AND issue_id=? AND sequence>? "
                "ORDER BY sequence LIMIT ?",
                (run_id, int(issue_id), max(0, int(after_sequence)), max(1, min(int(limit), 200))),
            ).fetchall()
            return [dict(row) for row in rows]

    def purge_expired(self, *, days: int = ANALYSIS_EVENT_RETENTION_DAYS, now: str = "") -> int:
        """删除终态 run 的过期事件与孤儿事件；返回删除行数。

        - 终态 run（completed/partial/failed/cancelled）且 finished_at 已
          超过保留期 → 删除其全部进度事件（最终结论在 runs/issues 里）；
        - run 行已不存在（历史清理残留）→ 一并删除，避免永久孤儿。
        """
        cutoff = _cutoff_iso(now or _now(), days=int(days))
        with self._lock, self._connect() as conn:
            self._ensure_schema(conn)
            cursor = conn.execute(
                "DELETE FROM redmine_daily_brief_analysis_events "
                "WHERE (run_id IN (SELECT run_id FROM redmine_daily_brief_runs "
                "WHERE status IN ('completed','partial','failed','cancelled') "
                "AND finished_at != '' AND finished_at < ?)) "
                "OR NOT EXISTS (SELECT 1 FROM redmine_daily_brief_runs AS run "
                "WHERE run.run_id=redmine_daily_brief_analysis_events.run_id)",
                (cutoff,),
            )
            return cursor.rowcount


def _cutoff_iso(now_iso: str, *, days: int) -> str:
    from datetime import datetime, timedelta

    try:
        moment = datetime.fromisoformat(now_iso)
    except ValueError:
        return ""
    return (moment - timedelta(days=max(0, days))).isoformat(timespec="seconds")


# repository 不持有事件存储（该文件体积已顶架构预算）：按 db_path 缓存
# store 实例，Web/Worker/CLI 进程内复用同一连接对象。store 的 schema
# 自举幂等且跨进程安全（见模块 docstring 的迁移约束）。
_EVENT_STORES: dict[str, DailyBriefAnalysisEventStore] = {}
_EVENT_STORES_LOCK = threading.Lock()


def event_store_for_repository(repository: Any) -> DailyBriefAnalysisEventStore:
    """返回该 repository（per-owner SQLite）对应的分析事件存储。"""
    key = str(getattr(repository, "db_path", "") or "")
    with _EVENT_STORES_LOCK:
        store = _EVENT_STORES.get(key)
        if store is None:
            store = DailyBriefAnalysisEventStore(Path(key))
            _EVENT_STORES[key] = store
        return store


class AnalysisProgressRecorder:
    """分析进度事件的高层出口；所有方法吞异常，进度不得影响分析本身。

    一次 (run, issue) 分析一个实例：构造时清空旧时间线并写入
    ``analysis_started``，结束时调用 :meth:`analysis_finished` 写入终态
    事件。时间线天然是 compact 形态（summary-only，无原始输出），保留
    期由 :meth:`DailyBriefAnalysisEventStore.purge_expired` 管理。
    """

    def __init__(
        self, store: DailyBriefAnalysisEventStore, run_id: str, issue_id: int,
        subject: str = "", model_name: str = "",
    ):
        self.store = store
        self.run_id = str(run_id)
        self.issue_id = int(issue_id)
        self.subject = scrub_secrets(subject)[:120]
        self.model_name = str(model_name or "").strip()
        self.tool_started_count = 0
        self.tool_completed_count = 0
        self.tool_failed_count = 0
        try:
            self.store.reset(self.run_id, self.issue_id)
            self._emit("analysis_started", summary=f"开始分析 #{self.issue_id}".strip())
        except Exception:
            logger.debug("analysis progress start failed", exc_info=True)

    # ------------------------------------------------------------------ api

    def stage_changed(self, summary: str, *, stage: str = "") -> None:
        self._emit("stage_changed", stage=stage, summary=summary)

    def tool_started(self, tool_name: str, tool_input: Any = None, *, stage: str = "kkagent") -> None:
        self.tool_started_count += 1
        self._emit(
            "tool_started", stage=stage, tool_name=str(tool_name or ""),
            summary=describe_tool_call(tool_name, tool_input),
        )

    def tool_finished(
        self, tool_name: str, tool_input: Any = None, *, ok: bool, stage: str = "kkagent",
        duration_ms: int | None = None, failure_kind: str = "",
    ) -> None:
        label = describe_tool_call(tool_name, tool_input)
        if ok:
            self.tool_completed_count += 1
            self._emit(
                "tool_completed", stage=stage, tool_name=str(tool_name or ""), status="success",
                summary=label, duration_ms=int(duration_ms or 0),
            )
        else:
            self.tool_failed_count += 1
            reason = scrub_secrets(failure_kind or "failed")
            self._emit(
                "tool_failed", stage=stage, tool_name=str(tool_name or ""), status="failed",
                summary=f"{label} · {reason}", duration_ms=int(duration_ms or 0),
            )

    def progress(self, summary: str) -> None:
        self._emit("progress", summary=summary)

    def analysis_finished(
        self, *, ok: bool, cancelled: bool = False, error_type: str = "", model_name: str = "",
    ) -> None:
        counts = f"已执行 {self.tool_started_count} 次工具调用"
        if self.tool_failed_count:
            counts += f" · {self.tool_failed_count} 失败"
        if cancelled:
            self._emit("analysis_failed", status="cancelled", summary=f"分析已停止 · {counts}")
            return
        if ok:
            suffix = f" · 模型 {model_name}" if model_name else ""
            self._emit("analysis_completed", status="success", summary=f"分析完成 · {counts}{suffix}")
            return
        reason = scrub_secrets(error_type or "analysis failed")
        self._emit("analysis_failed", status="failed", summary=f"分析失败（{reason}） · {counts}")

    # --------------------------------------------------------------- helper

    def _emit(self, event_type: str, *, stage: str = "", tool_name: str = "", status: str = "",
              summary: str = "", duration_ms: int = 0) -> None:
        try:
            self.store.append(self.run_id, self.issue_id, {
                "event_type": event_type,
                "stage": stage,
                "tool_name": tool_name,
                "status": status,
                "summary": scrub_secrets(summary),
                "duration_ms": max(0, int(duration_ms or 0)),
                "created_at": _now(),
            })
        except Exception:
            logger.debug("analysis progress emit failed (%s)", event_type, exc_info=True)


def start_analysis_progress(
    repository: Any, run: Any, issue_id: int, record: Any = None,
) -> AnalysisProgressRecorder | None:
    """为一次分析创建进度记录器；失败时返回 None（进度永不阻断分析）。"""
    store = getattr(repository, "events", None)
    if store is None:
        store = event_store_for_repository(repository)
    try:
        return AnalysisProgressRecorder(
            store, str(getattr(run, "run_id", "") or ""), int(issue_id),
            subject=str(getattr(record, "subject", "") or ""),
            model_name=str(getattr(run, "model_name", "") or ""),
        )
    except Exception:
        logger.debug("analysis progress recorder unavailable", exc_info=True)
        return None


def finish_analysis_progress(
    recorder: AnalysisProgressRecorder | None, *, ok: bool, cancelled: bool = False,
    error_type: str = "", model_name: str = "",
) -> None:
    if recorder is None:
        return
    try:
        recorder.analysis_finished(
            ok=ok, cancelled=cancelled, error_type=error_type, model_name=model_name,
        )
    except Exception:
        logger.debug("analysis progress finish failed", exc_info=True)


def progress_to_payload(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """API 出口整形：只保留 allowlist 字段（纵深防御，防未来列漂移）。"""
    return [{key: row[key] for key in _EVENT_COLUMNS if key in row} for row in events]


__all__ = [
    "ANALYSIS_EVENT_RETENTION_DAYS",
    "EVENT_TYPES",
    "AnalysisProgressRecorder",
    "DailyBriefAnalysisEventStore",
    "describe_tool_call",
    "event_store_for_repository",
    "finish_analysis_progress",
    "progress_to_payload",
    "scrub_secrets",
    "start_analysis_progress",
]
