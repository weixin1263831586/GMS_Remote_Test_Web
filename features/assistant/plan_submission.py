"""Agent 测试计划确认提交状态机。

同一 ``pending_plan`` 的确认请求可能因浏览器超时重发或用户重复点击到达
多次；pending→submitting→submitted/failed 的单次提交约束集中在这里，
HTTP 层只负责调用与消息呈现。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from .session_registry import finish_run_state, monitor_run_is_current


def superseded_plan_result(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keep any queued job receipt without reviving an obsolete run."""
    return {
        "success": False,
        "error": "该轮运行已取消或被新计划替换；若测试已启动，请在测试界面处理。",
        "data": (payload or {}).get("data") or {},
    }


async def execute_plan_start(
    session: dict[str, Any], request: Any, plan: dict[str, Any], monitor_token: str,
    *, pre_actions: Callable[..., Awaitable[dict[str, Any]]],
    start_test: Callable[..., Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Only the current run may advance after a delayed startup step."""
    try:
        result = await pre_actions(session, plan, request)
        if not monitor_run_is_current(session, monitor_token):
            return superseded_plan_result()
        if not result.get("success"):
            finish_run_state(session, "error")
            return result
        result = await start_test(session, request, plan)
        if not monitor_run_is_current(session, monitor_token):
            return superseded_plan_result(result)
        if not result.get("success"):
            finish_run_state(session, "error")
        return result
    except (asyncio.CancelledError, Exception) as exc:
        if monitor_run_is_current(session, monitor_token):
            finish_run_state(session, "cancelled" if isinstance(exc, asyncio.CancelledError) else "error")
        raise


async def submit_pending_plan_once(
    plan: dict[str, Any],
    execute: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> tuple[dict[str, Any] | None, str]:
    """同一计划只允许提交一次；返回 ``(提交结果, 重复确认提示语)``。

    - 结果非 ``None``：本次调用真正执行了提交（成功或失败），调用方按
      既有失败分支呈现错误；
    - 结果为 ``None``：重复确认被状态机吸收，提示语可直接追加为助手
      消息，不再次进入启动逻辑。

    失败把计划置回 ``failed`` 并保留在会话上，允许修正后重新确认；
    ``submitted`` 是终态，新一轮计划生成时会整体覆盖。
    """
    state = str(plan.get("submit_state") or "pending")
    if state == "submitting":
        return None, "该计划正在提交中，请勿重复确认。"
    if state == "submitted":
        return None, "该计划已提交执行，请勿重复确认；当前运行进度见会话状态。"
    if not plan.get("operation_id"):
        plan["operation_id"] = str(uuid.uuid4())
    plan["submit_state"] = "submitting"
    try:
        result = await execute(plan)
    except (asyncio.CancelledError, Exception):
        plan["submit_state"] = "failed"
        raise
    if result.get("success"):
        plan["submit_state"] = "submitted"
    else:
        plan["submit_state"] = "failed"
    return result, ""
