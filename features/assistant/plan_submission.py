"""Agent 测试计划确认提交状态机。

同一 ``pending_plan`` 的确认请求可能因浏览器超时重发或用户重复点击到达
多次；pending→submitting→submitted/failed 的单次提交约束集中在这里，
HTTP 层只负责调用与消息呈现。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


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
    plan["submit_state"] = "submitting"
    try:
        result = await execute(plan)
    except Exception:
        plan["submit_state"] = "failed"
        raise
    if result.get("success"):
        plan["submit_state"] = "submitted"
    else:
        plan["submit_state"] = "failed"
    return result, ""
