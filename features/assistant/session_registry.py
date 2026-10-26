"""对话 Agent 会话注册表：内存生命周期 + 崩溃恢复锚点。

外部深度评审：``_agent_sessions`` 等状态此前内嵌在 HTTP 入口模块里，
Controller 重启后 session_id 与 cluster_job_id / attempt_id / 执行阶段
的关联丢失，监控无法恢复。本模块把会话生命周期从路由层分离，并通过
``session_store`` 持久化最小恢复锚点（不含消息正文、asyncio.Task 或
pre_actions 密码）；消息/步骤等完整 UI 状态仍是可丢弃的。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from fastapi import Request

from features.assistant import session_store
from features.users import owner_id_from_request


AGENT_SESSION_TTL_SECONDS = 6 * 60 * 60
AGENT_SESSION_MAX_LIFETIME_SECONDS = 24 * 60 * 60

# 单进程内的会话表与监控任务表；跨进程共享属于未来部署事项（外部评审报告），
# 当前契约：本表为可丢弃 UI 状态，恢复只依赖 session_store 锚点。
_agent_sessions: dict[str, dict[str, Any]] = {}
_agent_sessions_lock = asyncio.Lock()
_agent_monitor_tasks: dict[str, asyncio.Task] = {}
# 会话恢复临界锁：查找内存会话 → 决定复用/重建 → 登记回注册表必须
# 原子完成，避免两个客户端同时恢复同一 session_id 时互相覆盖。
# 恢复路径无 await，线程锁足够。
_restore_lock = threading.Lock()
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


_logger = logging.getLogger(__name__)


def monitor_run_is_current(session: dict[str, Any], monitor_token: str | None) -> bool:
    """仅当 active_run 仍是本监控任务登记的那一轮时才允许改共享状态。

    同一会话连续提交新计划时，旧监控任务的取消回调可能晚于新一轮
    ``active_run`` 注册（外部评审报告项），旧任务据此识别自己已过期，
    不清空新任务的 active_run / 恢复锚点。恢复会话的 active_run 来自
    锚点、没有 token 字段，与恢复监控的 None token 匹配。active_run
    不存在（终态已清理）时必须返回 False：避免"空运行 + None token"
    意外匹配成当前轮。
    """
    active_run = session.get("active_run")
    if not active_run:
        return False
    return active_run.get("monitor_token") == monitor_token


def save_session_anchor(session: dict[str, Any]) -> None:
    """Persist the crash-recovery anchor，失败时显式标示「不可恢复」。

    持久化失败不能静默降级：Controller 重启后的恢复保证已失效，
    必须在日志与会话诊断里可见（外部评审报告项）。下一次保存成功时
    清除失败标记，磁盘短暂不可写恢复后 UI 不再显示过期状态
    。
    """
    try:
        session_store.save_anchor(session)
        session.pop("recovery_anchor_failed", None)
    except Exception:
        _logger.warning(
            "保存 Agent 会话恢复锚点失败，Controller 重启后该会话不可恢复",
            exc_info=True,
        )
        session["recovery_anchor_failed"] = True


def clear_session_anchor(session: dict[str, Any]) -> None:
    try:
        session_store.drop_anchor(session.get("session_id") or "")
        session.pop("recovery_anchor_failed", None)
    except Exception:
        _logger.warning("清理 Agent 会话恢复锚点失败", exc_info=True)


def owner_id_from_request_shim(request_shim: Any) -> str:
    """Resolve the ADR 0010 owner from a request-like object, tolerantly.

    监控/恢复路径可能拿到没有 ``state`` 的极简请求对象（回归测试 shim、
    恢复会话的重建监控），此时返回空串，由调用方回退会话持久化的
    ``owner_id``，不因缺请求身份而中断监控。
    """
    try:
        return str(owner_id_from_request(request_shim) or "")
    except (AttributeError, TypeError):
        return ""


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

    同属主已有内存会话时必须复用现有对象（只重绑请求主体身份），不能
    从锚点重建覆盖：内存对象正被活跃监控任务持有并更新，重建会让
    监控任务继续写旧对象、新查询拿到停滞的副本。
    """
    with _restore_lock:
        anchor = session_store.load_anchors().get(session_id)
        if not anchor:
            return None
        owner_matches = bool(owner_id) and anchor.get("owner_id") == owner_id
        if anchor.get("client_id") != client_id and not owner_matches:
            return None
        existing = _agent_sessions.get(session_id)
        if existing and str(existing.get("owner_id") or "") == str(anchor.get("owner_id") or ""):
            existing["client_id"] = client_id
            if owner_id:
                existing["owner_id"] = owner_id
            return existing
        session = session_from_anchor(anchor)
        if session:
            session["client_id"] = client_id
            if owner_id:
                session["owner_id"] = owner_id
            _agent_sessions[session_id] = session
        return session
