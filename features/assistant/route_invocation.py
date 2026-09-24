"""executor_ref 直调 FastAPI 路由的执行层。

从 executor.py 拆出：route discovery → route dependency 守卫
（human-only 等策略）→ 参数绑定 → 执行 → ToolResult 归一化。
编排（resolve tool → authorize → dispatch）仍留在 executor.py。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from typing import Any

from fastapi import HTTPException

from features.assistant.execution_result import ToolResult
from features.assistant.route_binding import build_call_kwargs
from features.assistant.tools import AgentTool

from .executor_formatting import format_payload as _format_payload
from .executor_formatting import json_body as _json_body


logger = logging.getLogger(__name__)

_TOOL_PAGES = {
    "devices": "devices",
    "test": "test",
    "reports": "reports",
    "report": "reports",
    "desktop": "desktop",
    "terminal": "terminal",
    "vpn": "api-docs",
    "usbip": "devices",
    "ssh": "api-docs",
    "burn": "devices",
    "config": "api-docs",
    "system": "api-docs",
    "apk": "apk-analysis",
    "assets": "websites",
    "redmine": "redmine-agent",
    "gerrit": "gerrit-dashboard",
    "automation": "automation",
    "cluster": "cluster",
    "build": "automation",
    "knowledge": "notes",
}

_UNSUPPORTED_DIRECT_TOOLS = {
    "apk_upload",
    "terminal_push",
    "test_logs_stream",
    "system_websocket_{client_id}",
    "burn_firmware",
    "burn_gsi",
}


async def enforce_route_dependencies(module: Any, func: Any, request: Any) -> None:
    routers = [value for value in vars(module).values() if value.__class__.__name__ == "APIRouter"]
    for router in routers:
        for route in router.routes:
            if getattr(route, "endpoint", None) is not func:
                continue
            for dependency_parameter in getattr(route, "dependencies", ()):
                if request is None:
                    raise HTTPException(status_code=401, detail="Route authorization context is required")
                dependency = getattr(dependency_parameter, "dependency", None)
                if not callable(dependency):
                    raise HTTPException(status_code=403, detail="Unsupported route authorization dependency")
                resolved = dependency(request)
                if inspect.isawaitable(resolved):
                    await resolved
            return


async def call_router_function(
    tool: AgentTool, session: Any, request: Any, params: dict[str, Any]
) -> ToolResult:
    """通过 executor_ref 调用 router 函数。"""
    if tool.name in _UNSUPPORTED_DIRECT_TOOLS:
        page = _TOOL_PAGES.get(tool.category, "api-docs")
        return ToolResult(
            success=False,
            tool_name=tool.name,
            formatted_text=f"「{tool.display_name}」需要在对应页面补充文件或交互参数，请打开页面操作。",
            page=page,
            quick_actions=[{"label": "打开页面", "page": page}],
            error="该工具不支持 Agent 直接执行",
        )

    ref = tool.executor_ref
    if ":" not in ref:
        return ToolResult(success=False, tool_name=tool.name, error=f"Invalid executor_ref: {ref}")

    module_path, func_name = ref.rsplit(":", 1)
    try:
        module = importlib.import_module(module_path)
        func = getattr(module, func_name)
    except (ImportError, AttributeError) as e:
        return ToolResult(success=False, tool_name=tool.name, error=f"Cannot resolve {ref}: {e}")

    try:
        # Calling an endpoint object directly bypasses FastAPI's route
        # dependency graph.  Resolve route-level guards (notably
        # human-only policies) explicitly before binding any caller data.
        await enforce_route_dependencies(module, func, request)
        call_kwargs = build_call_kwargs(func, tool, request, params)
        if asyncio.iscoroutinefunction(func):
            response = await func(**call_kwargs)
        else:
            response = await asyncio.to_thread(func, **call_kwargs)

        # 解析 JSONResponse
        payload = _json_body(response) if hasattr(response, "body") else {"success": True, "data": response}
        formatted = _format_payload(tool, payload)
        return ToolResult(
            success=payload.get("success", True),
            tool_name=tool.name,
            data=payload.get("data", payload),
            formatted_text=formatted,
            page=_TOOL_PAGES.get(tool.category, ""),
            error=payload.get("error", ""),
        )
    except Exception as e:
        logger.error("[Agent] router call %s failed: %s", ref, e, exc_info=True)
        return ToolResult(
            success=False,
            tool_name=tool.name,
            error=str(e),
            formatted_text=f"调用「{tool.display_name}」失败：{e}",
            page=_TOOL_PAGES.get(tool.category, ""),
        )


__all__ = ["call_router_function", "enforce_route_dependencies"]
