"""对话 Agent 会话持久化：恢复锚点 + 脱敏对话记录。

两层持久化职责不同：
- 锚点（``agent_sessions.json``）：会话元数据 + 活跃运行关联，供
  Controller 重启后续接监控，不含消息正文；
- 对话记录（``agent_transcripts/<session_id>.json``）：每会话一文件的
  脱敏 messages/steps 历史，供重启后恢复对话正文。消息进入会话时
  已经 ``display_record`` 脱敏，落盘不新增敏感面；不落盘
  asyncio.Task、pending_plan 或凭据。两者都原子替换、0600、按 TTL
  清扫，量级 = 会话数，与 ``data/security_audit.json`` 等既有运行时
  文件同级。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import threading
from pathlib import Path
from typing import Any


_LOCK = threading.Lock()
_MAX_RECORDS = 256
# 对话记录裁剪上限：记录是恢复用途的 UI 历史，不是审计日志，超限裁最旧。
_MAX_TRANSCRIPT_MESSAGES = 200
_MAX_TRANSCRIPT_STEPS = 300
# 会话 ID 一律是 uuid4；落盘文件名只接受该形态，阻断路径注入。
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

# 会话字段里参与持久化恢复锚点的白名单；其余字段（消息、步骤、
# pending_plan 等）重启后不还原，由 UI 按「过期」处理。
_SESSION_ANCHOR_FIELDS = (
    "session_id",
    "client_id",
    "owner_id",
    "title",
    "status",
    "created_at",
    "updated_at",
    "active_run",
    "workspace_context",
)


def default_sessions_path() -> Path:
    from foundation.config import PROJECT_ROOT

    data_dir = Path(PROJECT_ROOT) / "data" / "assistant"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "agent_sessions.json"


def default_transcripts_dir() -> Path:
    from foundation.config import PROJECT_ROOT

    data_dir = Path(PROJECT_ROOT) / "data" / "assistant" / "agent_transcripts"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _normalize_active_run(active_run: Any) -> dict[str, Any] | None:
    if not isinstance(active_run, dict):
        return None
    # 恢复锚点只需要关联 ID 与阶段；plan 里可能带 pre_actions 密码，
    # 明确剥离，不落盘。
    return {
        key: active_run.get(key)
        for key in (
            "cluster_job_id",
            # 对话 Agent 工作流 run 的恢复锚点（generic run 用 cluster_job_id）。
            "automation_run_id",
            "attempt_id",
            "worker_id",
            "baseline_report_timestamp",
            "started_at",
        )
        if active_run.get(key)
    } or None


def session_anchor(session: dict[str, Any]) -> dict[str, Any]:
    """Extract the persistence anchor from a live session dict."""
    anchor: dict[str, Any] = {}
    for key in _SESSION_ANCHOR_FIELDS:
        if key == "active_run":
            anchor[key] = _normalize_active_run(session.get("active_run"))
        elif key == "workspace_context":
            workspace = session.get("workspace_context")
            anchor[key] = {k: v for k, v in (workspace or {}).items() if isinstance(v, (str, int, float, bool))}
        elif session.get(key):
            anchor[key] = session[key]
    return anchor


def load_anchors(path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Load persisted anchors; tolerate truncation/corruption (best effort)."""
    target = Path(path) if path else default_sessions_path()
    anchors = _read_store_unlocked(target)
    return {
        str(sid): anchor
        for sid, anchor in anchors.items()
        if isinstance(anchor, dict) and anchor.get("session_id")
    }


def save_anchor(session: dict[str, Any], path: Path | str | None = None) -> None:
    """Merge one session's anchor into the store atomically."""
    anchor = session_anchor(session)
    if not anchor.get("session_id"):
        return
    _write_store({str(anchor["session_id"]): anchor}, path, replace_all=False)


def drop_anchor(session_id: str, path: Path | str | None = None) -> None:
    """Remove an anchor after the session is cancelled/expired cleanly."""
    target = Path(path) if path else default_sessions_path()
    with _LOCK, _store_file_lock(target):
        anchors = _read_store_unlocked(target)
        if session_id not in anchors:
            return
        anchors.pop(session_id, None)
        _write_store_unlocked(target, anchors)


@contextlib.contextmanager
def _store_file_lock(target: Path):
    """Cross-process advisory lock for the anchor store.

    ``_LOCK`` 只保证单进程内互斥；多 Uvicorn Worker 进程并发执行
    「读入 → 更新 → 替换」时会发生后写覆盖先写。
    与 ``foundation/security_audit.py`` 同款 ``fcntl.flock`` 侧车锁，
    使读改写序列在部署内的多个进程之间串行化。文件锁本身不保护
    ``load_anchors()`` 的纯读路径——``os.replace`` 原子替换保证读者
    看到完整的新旧文件之一。
    """
    lock_path = target.with_suffix(".json.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_store(
    update: dict[str, dict[str, Any]],
    path: Path | str | None,
    *,
    replace_all: bool,
) -> None:
    target = Path(path) if path else default_sessions_path()
    with _LOCK, _store_file_lock(target):
        anchors = {} if replace_all else _read_store_unlocked(target)
        anchors.update(update)
        _write_store_unlocked(target, anchors)


def _read_store_unlocked(target: Path | str) -> dict[str, dict[str, Any]]:
    target = Path(target)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    sessions = raw.get("sessions") if isinstance(raw, dict) else None
    if not isinstance(sessions, dict):
        return {}
    return sessions
def _write_store_unlocked(target: Path, anchors: dict[str, dict[str, Any]]) -> None:
    # 超量时按 updated_at 淘汰最旧的锚点，防止长期运行无限增长。
    if len(anchors) > _MAX_RECORDS:
        ranked = sorted(
            anchors.items(),
            key=lambda item: str(item[1].get("updated_at") or ""),
            reverse=True,
        )
        anchors = dict(ranked[:_MAX_RECORDS])
    payload = json.dumps(
        {"version": 1, "sessions": anchors},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    _atomic_write_json(target, payload)


def _atomic_write_json(target: Path, payload: str, *, unique_tmp: bool = False) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = f".{os.getpid()}.tmp" if unique_tmp else ".tmp"
    tmp_path = target.with_suffix(suffix)
    # 锚点/记录虽不含明文凭据，但含 owner/client/Job 关联，按受限权限创建。
    file_descriptor = os.open(
        tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # fdopen 失败时描述符仍打开；已关闭时重复 close 仅报 EBADF。
        with contextlib.suppress(OSError):
            os.close(file_descriptor)
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
    # umask 可能压制 open 的 mode 参数，替换前显式 chmod 确定收敛 0600。
    os.chmod(tmp_path, 0o600)
    try:
        # os.replace 保证原子性；与 security_audit.json 同款写法。
        os.replace(tmp_path, target)
    except OSError:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def save_transcript(session: dict[str, Any], directory: Path | str | None = None) -> None:
    """Persist one session's redacted chat history (messages + steps)."""
    session_id = str(session.get("session_id") or "")
    # 未注册到客户端的临时会话（纯计算/测试裸字典）没有属主，不落盘。
    if not session.get("client_id") or not _SESSION_ID_RE.match(session_id):
        return
    messages = session.get("messages") or []
    steps = session.get("steps") or []
    if not messages and not steps:
        return
    workspace = session.get("workspace_context")
    record = {
        "version": 1,
        "session_id": session_id,
        "client_id": session.get("client_id") or "",
        "owner_id": session.get("owner_id") or "",
        "title": session.get("title") or "",
        "status": session.get("status") or "idle",
        # pending_plan 含 pre_actions 密码，明确不落盘；重启后待确认计划失效。
        "workspace_context": {
            k: v for k, v in (workspace or {}).items()
            if isinstance(v, (str, int, float, bool))
        },
        "created_at": session.get("created_at") or "",
        "updated_at": session.get("updated_at") or "",
        "messages": messages[-_MAX_TRANSCRIPT_MESSAGES:],
        "steps": steps[-_MAX_TRANSCRIPT_STEPS:],
    }
    target = (Path(directory) if directory else default_transcripts_dir()) / f"{session_id}.json"
    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    # 每会话一文件：写入互不共享，原子替换即可，无需文件锁。
    _atomic_write_json(target, payload, unique_tmp=True)


def load_transcripts(directory: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Load all persisted transcripts (best effort; corrupt files skipped)."""
    root = Path(directory) if directory else default_transcripts_dir()
    transcripts: dict[str, dict[str, Any]] = {}
    try:
        files = list(root.glob("*.json"))
    except OSError:
        return transcripts
    for path in files:
        if not _SESSION_ID_RE.match(path.stem):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("session_id"):
            transcripts[str(record["session_id"])] = record
    return transcripts


def load_transcript(session_id: str, directory: Path | str | None = None) -> dict[str, Any] | None:
    if not _SESSION_ID_RE.match(session_id or ""):
        return None
    root = Path(directory) if directory else default_transcripts_dir()
    try:
        record = json.loads((root / f"{session_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("session_id") else None


def drop_transcript(session_id: str, directory: Path | str | None = None) -> None:
    if not _SESSION_ID_RE.match(session_id or ""):
        return
    root = Path(directory) if directory else default_transcripts_dir()
    with contextlib.suppress(OSError):
        (root / f"{session_id}.json").unlink()
