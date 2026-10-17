"""对话 Agent 会话注册表：内存生命周期 + 崩溃恢复锚点。

外部深度评审：``_agent_sessions`` 等状态此前内嵌在 HTTP 入口模块里，
Controller 重启后 session_id 与 cluster_job_id / attempt_id / 执行阶段
的关联丢失，监控无法恢复。本模块把会话生命周期从路由层分离，并通过
``session_store`` 持久化最小恢复锚点（不含消息正文、asyncio.Task 或
pre_actions 密码）；消息/步骤等完整 UI 状态仍是可丢弃的。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from fastapi import Request

from features.assistant import session_store


AGENT_SESSION_TTL_SECONDS = 6 * 60 * 60
AGENT_SESSION_MAX_LIFETIME_SECONDS = 24 * 60 * 60

# 单进程内的会话表与监控任务表；跨进程共享属于未来部署事项（外部评审报告），
# 当前契约：本表为可丢弃 UI 状态，恢复只依赖 session_store 锚点。
_agent_sessions: dict[str, dict[str, Any]] = {}
_agent_sessions_lock = asyncio.Lock()
_agent_monitor_tasks: dict[str, asyncio.Task] = {}
_last_session_cleanup = 0.0


class AgentRequestShim:
    """Minimal request object for reusing existing route handlers."""

    def __init__(
        self,
        request: Request,
        query_params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ):
        self.headers = request.headers
        self.client = request.client
        self.cookies = getattr(request, "cookies", {})
        self.state = getattr(request, "state", SimpleNamespace())
        self.method = getattr(request, "method", "POST")
        self.url = getattr(request, "url", None)
        self.query_params = query_params if query_params is not None else getattr(request, "query_params", {})
        self._json_body = dict(json_body or {})

    async def form(self):
        return {}

    async def json(self):
        return dict(self._json_body)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def expired_session_payload(session_id: str) -> dict[str, Any]:
    return {
        "expired": True,
        "session": {
            "session_id": session_id,
            "status": "idle",
            "messages": [],
            "steps": [],
            "pending_plan": None,
            "active_run": None,
        },
    }


async def get_or_create_session(session_id: str | None, client_id: str) -> dict[str, Any]:
    async with _agent_sessions_lock:
        existing = _agent_sessions.get(session_id or "")
        sid = session_id if existing and existing.get("client_id") == client_id else str(uuid.uuid4())
        session = _agent_sessions.get(sid)
        if not session:
            session = {
                "session_id": sid,
                "client_id": client_id,
                "status": "idle",
                "messages": [],
                "steps": [],
                "pending_plan": None,
                "active_run": None,
                "workspace_context": {},
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            }
            _agent_sessions[sid] = session
        session["updated_at"] = _now_iso()
        return session


def cleanup_due() -> bool:
    """Rate-limit the periodic sweep (called on every chat request)."""
    global _last_session_cleanup
    if time.monotonic() - _last_session_cleanup < 50:
        return False
    _last_session_cleanup = time.monotonic()
    return True


async def cleanup_sessions() -> None:
    now = time.time()
    cutoff = now - AGENT_SESSION_TTL_SECONDS
    # running/monitoring 会话不受空闲 TTL 约束，但监控协程可能早已死亡
    # （进程重启、异常退出），不能让它们无限占用内存：超过最长存活时间
    # 后一并纳入清扫，并取消遗留的监控任务。
    max_lifetime_cutoff = now - AGENT_SESSION_MAX_LIFETIME_SECONDS
    async with _agent_sessions_lock:
        stale_ids = []
        for sid, session in _agent_sessions.items():
            try:
                updated = datetime.fromisoformat(session.get("updated_at", "")).timestamp()
            except Exception:
                updated = 0
            try:
                created = datetime.fromisoformat(session.get("created_at", "")).timestamp()
            except Exception:
                created = updated
            if created < max_lifetime_cutoff or (
                updated < cutoff
                and session.get("status") not in {"running", "monitoring"}
            ):
                stale_ids.append(sid)
        for sid in stale_ids:
            _agent_sessions.pop(sid, None)
    for sid in stale_ids:
        task = _agent_monitor_tasks.pop(sid, None)
        if task and not task.done():
            task.cancel()
        # 被清扫的会话同步清掉持久锚点，避免残留可恢复入口。
        try:
            session_store.drop_anchor(sid)
        except Exception:
            pass


def session_from_anchor(anchor: dict[str, Any]) -> dict[str, Any] | None:
    """Rebuild a minimal monitoring session from a persisted anchor."""
    sid = str(anchor.get("session_id") or "")
    active_run = anchor.get("active_run") or {}
    if not sid or not active_run.get("cluster_job_id"):
        return None
    return {
        "session_id": sid,
        "client_id": anchor.get("client_id") or "",
        "owner_id": anchor.get("owner_id") or "",
        "status": "monitoring",
        "messages": [],
        "steps": [],
        "pending_plan": None,
        "active_run": dict(active_run),
        "workspace_context": dict(anchor.get("workspace_context") or {}),
        "created_at": anchor.get("created_at") or _now_iso(),
        "updated_at": _now_iso(),
        "recovered": True,
    }


def restore_session(session_id: str, client_id: str, owner_id: str = "") -> dict[str, Any] | None:
    """Restore a session for its owner after a Controller restart.

    锚点存的是会话创建时的 ``client_id``（浏览器稳定 ID）；而路由收到的
    请求主体身份可能是 agent/自动化 actor id（``agent:tok`` 等，ADR 0010
    下与 resource_owner_id 不同）。调用方需同时传入请求主体的
    resource_owner_id 做属主判定：client_id 直接命中，或 owner 命中，
    才允许恢复；恢复出的会话以请求主体身份为准，否则同属主重启后将
    永远「过期」。
    """
    anchor = session_store.load_anchors().get(session_id)
    if not anchor:
        return None
    owner_matches = bool(owner_id) and anchor.get("owner_id") == owner_id
    if anchor.get("client_id") != client_id and not owner_matches:
        return None
    session = session_from_anchor(anchor)
    if session:
        session["client_id"] = client_id
        if owner_id:
            session["owner_id"] = owner_id
        _agent_sessions[session_id] = session
    return session
