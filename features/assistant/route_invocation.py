"""executor_ref 直调 FastAPI 路由：参数绑定与执行。

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

from features.assistant.response import ToolResult, http_error_result, internal_error_result
from features.assistant.response import format_payload as _format_payload
from features.assistant.response import json_body as _json_body
from features.assistant.tools import AgentTool
from foundation.error_model import ApiError
from foundation.runtime_contracts import TestStartRequest


logger = logging.getLogger(__name__)


# 请求体模型映射，首次使用时初始化。
_MODEL_BY_TOOL: dict[str, type] | None = None  # lazily initialized to avoid circular imports

# 缓存函数签名，避免重复反射。
_SIGNATURE_CACHE: dict[Any, inspect.Signature] = {}


def cached_signature(func: Any) -> inspect.Signature:
    sig = _SIGNATURE_CACHE.get(func)
    if sig is None:
        sig = inspect.signature(func)
        _SIGNATURE_CACHE[func] = sig
    return sig


def get_model_by_tool() -> dict[str, type]:
    """Lazy-initialised mapping of tool names to Pydantic request models."""
    global _MODEL_BY_TOOL
    if _MODEL_BY_TOOL is None:
        from features.devices import (
            ADBForwardStartRequest,
            DeviceActionRequest,
            DeviceLockRequest,
            DeviceShellRequest,
            UiControlRequest,
            UiTapRequest,
            USBIPDisconnectRequest,
            USBIPStartRequest,
            WifiConnectRequest,
        )
        from features.firmware import GSIBurnRequest

        # features.knowledge 公共面经 __getattr__ 惰性导出请求模型，
        # 不再深引其路由模块内部。
        from features.knowledge import ExternalSearchRequest as _ExternalSearchRequest
        from features.reports import ReportDiagnosisRequest
        from features.system import VNCStartRequest, VPNConnectRequest
        from features.test_execution import (
            SuiteApkAnalyzeRequest,
            TestParseArgsRequest,
            TradefedListResultsRequest,
        )
        from features.users import ClientInfoRequest

        _MODEL_BY_TOOL = {
            "users_detect": ClientInfoRequest,
            "users_set_username": ClientInfoRequest,
            "devices_bootloader_lock": DeviceLockRequest,
            "devices_bootloader_unlock": DeviceLockRequest,
            "devices_bootloader_status": DeviceActionRequest,
            "devices_info": DeviceActionRequest,
            "devices_reboot": DeviceActionRequest,
            "devices_remount": DeviceActionRequest,
            "devices_wifi": WifiConnectRequest,
            "devices_shell": DeviceShellRequest,
            "devices_scrcpy": DeviceActionRequest,
            "devices_ui_layout": UiControlRequest, "devices_ui_tap": UiTapRequest,
            "test_start": TestStartRequest,
            "test_parse_args": TestParseArgsRequest,
            "test_suites_result": TradefedListResultsRequest,
            "reports_diagnose": ReportDiagnosisRequest,
            "suites_apk_analyze": SuiteApkAnalyzeRequest,
            "desktop_vnc_start": VNCStartRequest,
            "desktop_validate": VNCStartRequest,
            "vpn_connect": VPNConnectRequest,
            "adb_forward_start": ADBForwardStartRequest,
            "usbip_connect": USBIPStartRequest,
            "usbip_disconnect": USBIPDisconnectRequest,
            "burn_gsi": GSIBurnRequest,
            # knowledge external search: 请求体经 ExternalSearchRequest 建模,
            # android_api_level 的范围校验(1-1000)在此生效。
            "android_internals_search": _ExternalSearchRequest,
        }
    return _MODEL_BY_TOOL


def build_call_kwargs(
    func: Any, tool: AgentTool, request: Any, params: dict[str, Any]
) -> dict[str, Any]:
    from features.assistant.session_registry import AgentRequestShim

    model_by_tool = get_model_by_tool()

    query_params = query_params_for_tool(tool, params)
    body_params = body_params_for_tool(tool, params)
    sig = cached_signature(func)
    dependency_names = {
        name for name, parameter in sig.parameters.items()
        if (
            parameter.default is not inspect.Parameter.empty
            and parameter.default.__class__.__module__.startswith("fastapi.params")
            and parameter.default.__class__.__name__ == "Depends"
        )
    }
    supplied_dependencies = dependency_names.intersection(params or {})
    if supplied_dependencies:
        raise HTTPException(
            status_code=400,
            detail=f"Reserved authorization parameters are not accepted: {sorted(supplied_dependencies)}",
        )

    shim = AgentRequestShim(
        request,
        query_params=query_params,
        json_body=body_params,
    ) if request else None
    kwargs: dict[str, Any] = {}

    for name, parameter in sig.parameters.items():
        if name == "request":
            kwargs[name] = shim
        elif name == "help":
            kwargs[name] = False
        elif name == "h":
            kwargs[name] = None
        elif name in ("req", "body", "payload"):
            # "payload" 是 knowledge external_api 等路由的请求体形参,
            # 与 req/body 同语义(POST body),此前未绑定导致 assistant
            # 经 executor_ref 直调 search_external 时必现缺参。
            model = model_by_tool.get(tool.name)
            if model:
                kwargs[name] = model(**body_params)
            else:
                kwargs[name] = body_params
        elif (
            parameter.default is not inspect.Parameter.empty
            and parameter.default.__class__.__module__.startswith("fastapi.params")
            and parameter.default.__class__.__name__ == "Depends"
        ):
            dependency = parameter.default.dependency
            if not callable(dependency) or request is None:
                raise HTTPException(
                    status_code=401,
                    detail="Route authorization context is required",
                )
            resolved = dependency(request)
            if inspect.isawaitable(resolved):
                raise RuntimeError("Async route dependencies are not supported")
            kwargs[name] = resolved
        elif name in params:
            kwargs[name] = params[name]
        elif name in query_params:
            kwargs[name] = query_params[name]
        elif parameter.default is not inspect.Parameter.empty:
            default = parameter.default
            if default.__class__.__module__.startswith("fastapi.params"):
                value = getattr(default, "default", inspect.Parameter.empty)
                if value is not inspect.Parameter.empty and value.__class__.__name__ != "PydanticUndefinedType":
                    kwargs[name] = value

    return kwargs


def body_params_for_tool(tool: AgentTool, params: dict[str, Any]) -> dict[str, Any]:
    body = dict(params or {})
    if tool.name == "knowledge_ask":
        # Protected import/web expansion is a human knowledge-page action, not a planner parameter.
        body.pop("include_authorized_imports", None)
        body.pop("include_external", None)
    if tool.name == "devices_shell" and "serial_no" not in body:
        devices = body.get("devices") or []
        if devices:
            body["serial_no"] = devices[0]
    if tool.name == "desktop_validate" and "host" not in body:
        body["host"] = body.get("ubuntu_host") or body.get("device_host")
    return body


def query_params_for_tool(tool: AgentTool, params: dict[str, Any]) -> dict[str, Any]:
    query = dict(params or {})
    if tool.name == "reports_delete" and "timestamp" not in query:
        query["timestamp"] = query.get("report_timestamp", "")
    if tool.name == "reports_download" and "report_timestamp" not in query:
        query["report_timestamp"] = query.get("timestamp", "")
    return {k: v for k, v in query.items() if v is not None}

# 已装配 FastAPI app 的展开路由表缓存（模块回退用；惰性填充避免导入期循环）。
_global_app_routes: list | None = None


def _get_global_app_routes() -> list | None:
    """Return flattened routes of the assembled FastAPI app (cached, may be None).

    提供无 ``request.app`` 上下文（executor 假 request / 内部调用）时的
    守卫匹配源：薄包装 executor_ref 的 api_path 与真实路由一一对应。
    """
    global _global_app_routes
    if _global_app_routes is None:
        try:
            from app import app as _assembled_app
            from foundation.routing_introspection import flatten_app_routes

            _global_app_routes = flatten_app_routes(_assembled_app)
        except Exception:
            # app 未装配（纯单测环境）时保持 None：调用方继续走模块级
            # router 回退，全部未命中仍按 fail-closed 拒绝。
            return None
    return _global_app_routes


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
    "sdk": "users",
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


async def _run_route_dependencies(route: Any, request: Any) -> None:
    for dependency_parameter in getattr(route, "dependencies", ()):
        if request is None:
            raise HTTPException(status_code=401, detail="Route authorization context is required")
        dependency = getattr(dependency_parameter, "dependency", None)
        if not callable(dependency):
            raise HTTPException(status_code=403, detail="Unsupported route authorization dependency")
        resolved = dependency(request)
        if inspect.isawaitable(resolved):
            await resolved


async def enforce_route_dependencies(
    module: Any, func: Any, request: Any, *, api_path: str = "", method: str = ""
) -> None:
    """Run route-level guards for a dynamically invoked endpoint.

    Fail-closed: if no registered route can be matched, the invocation is
    rejected instead of silently skipping authorization (an unmatched
    function has not gone through route registration review).

    Matching order:
    1. ``api_path`` + ``method`` against the live FastAPI application route
       table — the authoritative registry (``AgentTool.api_path`` mirrors
       the HTTP route the tool stands in for, which also covers modules
       that re-export or thin-wrap an endpoint implemented elsewhere).
    2. Endpoint identity against the live route table (fastapi 0.141+
       requires ``foundation.routing_introspection.flatten_app_routes``
       to unwrap the lazy ``_IncludedRouter`` entries).
    3. The assembled application's routes when ``request.app`` is absent.
    4. Module-level ``APIRouter`` attributes of `module` — fallback when
       no application route table is available.
    """
    from foundation.routing_introspection import flatten_app_routes

    if request is not None:
        app = getattr(request, "app", None)
        if app is not None:
            routes = flatten_app_routes(app)
            if api_path:
                wanted_method = (method or "GET").upper()
                for route in routes:
                    if getattr(route, "path", "") != api_path:
                        continue
                    methods = getattr(route, "methods", None) or set()
                    if wanted_method not in methods:
                        continue
                    await _run_route_dependencies(route, request)
                    return
            for route in routes:
                if getattr(route, "endpoint", None) is not func:
                    continue
                if method and method.upper() != "WEBSOCKET" and method.upper() not in (
                    getattr(route, "methods", None) or set()
                ):
                    continue
                await _run_route_dependencies(route, request)
                return
            # An active app is authoritative: an unmounted module router must
            # never supply a weaker fallback authorization context.
            raise HTTPException(status_code=404, detail="No registered route guards matched; refusing dynamic invocation")
    # 薄包装 executor_ref（如 features.sdk:list_sdk_sources 转发
    # source_api.list_sdk_sources）本身不在任何路由上；api_path 给出时，
    # 无 app 上下文也可用全局装配 app 做"该路径注册的端点守卫"匹配——
    # 路径+方法唯一确定路由，包装函数与实现函数共享同一 HTTP 守卫。
    if api_path and (request is None or getattr(request, "app", None) is None):
        global_routes = _get_global_app_routes()
        if global_routes is not None:
            wanted_method = (method or "GET").upper()
            for route in global_routes:
                if getattr(route, "path", "") != api_path:
                    continue
                if wanted_method not in (getattr(route, "methods", None) or set()):
                    continue
                await _run_route_dependencies(route, request)
                return
            for route in global_routes:
                if getattr(route, "endpoint", None) is not func:
                    continue
                if method and method.upper() != "WEBSOCKET" and method.upper() not in (
                    getattr(route, "methods", None) or set()
                ):
                    continue
                await _run_route_dependencies(route, request)
                return
            raise HTTPException(status_code=404, detail="No registered route guards matched; refusing dynamic invocation")
    routers = [value for value in vars(module).values() if value.__class__.__name__ == "APIRouter"]
    for router in routers:
        # fastapi 0.141 把 include_router 表示为惰性包装，模块级 router
        # 的 routes 同样需要展开后才能做端点身份匹配。
        for route in flatten_app_routes(router):
            if getattr(route, "endpoint", None) is not func:
                continue
            if method and method.upper() != "WEBSOCKET" and method.upper() not in (
                getattr(route, "methods", None) or set()
            ):
                continue
            await _run_route_dependencies(route, request)
            return
    raise HTTPException(status_code=404, detail="No registered route guards matched; refusing dynamic invocation")


async def call_router_function(
    tool: AgentTool, session: Any, request: Any, params: dict[str, Any]
) -> ToolResult:
    """通过 executor_ref 调用 router 函数。"""
    guard_path = getattr(tool, "api_path", "") or ""
    guard_method = getattr(tool, "method", "") or "GET"
    if guard_path and guard_method.upper() == "WEBSOCKET":
        guard_path = ""
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
    except (ImportError, AttributeError):
        # 配置错误属于服务端问题：模块名/符号名不外显给 Agent，
        # 保留 request_id 契约（traceback 已随 logger 落服务端日志）。
        result = internal_error_result(tool.name, "调用")
        logger.error("[Agent] unresolved executor_ref %s: %s", ref, result.error, exc_info=True)
        return result

    try:
        # Calling an endpoint object directly bypasses FastAPI's route
        # dependency graph.  Resolve route-level guards (notably
        # human-only policies) explicitly before binding any caller data.
        await enforce_route_dependencies(
            module, func, request, api_path=guard_path, method=guard_method
        )
        call_kwargs = build_call_kwargs(func, tool, request, params)
        if asyncio.iscoroutinefunction(func):
            response = await func(**call_kwargs)
        else:
            response = await asyncio.to_thread(func, **call_kwargs)

        # 解析 JSONResponse
        payload = _json_body(response)
        if payload.get("success") is False:
            rejected = http_error_result(tool.name, HTTPException(
                status_code=getattr(response, "status_code", 400),
                detail=payload.get("error") or payload.get("message") or payload.get("detail"),
            ))
            rejected.data = {**payload, **(rejected.data or {})}
            rejected.page = _TOOL_PAGES.get(tool.category, "")
            return rejected
        formatted = _format_payload(tool, payload)
        return ToolResult(
            success=payload.get("success", True),
            tool_name=tool.name,
            data=payload.get("data", payload),
            formatted_text=formatted,
            page=_TOOL_PAGES.get(tool.category, ""),
            error=payload.get("error", ""),
        )
    except ApiError as exc:
        return ToolResult(
            success=False, tool_name=tool.name,
            data={"status_code": exc.status_code, "code": exc.code,
                  "next_actions": list(exc.next_actions)},
            formatted_text=exc.message, error=exc.message,
        )
    except HTTPException as exc:
        # 受控异常：detail 是路由守卫/端点自己的业务语义（401/403 等），
        # 可以原样交给 Agent；单独分支避免与未知异常混在一起。
        logger.warning("[Agent] router call %s rejected: %s", ref, exc.detail)
        result = http_error_result(tool.name, exc, f"调用「{tool.display_name}」")
        result.page = _TOOL_PAGES.get(tool.category, "")
        return result
    except Exception:
        # 未知异常的 str(e) 可能内嵌路径/凭据片段，不外显给
        # Agent——完整 traceback 只进日志，Agent 拿到统一错误 + request id
        #（internal_error_result），便于用户凭 request id 回查服务端日志。
        result = internal_error_result(tool.name, f"调用「{tool.display_name}」")
        # request_id 必须和 traceback 出现在同一条日志里，否则
        # 返回给 Agent 的编号无法用于回查。
        logger.error(
            "[Agent] router call %s failed: %s", ref, result.error,
            exc_info=True,
        )
        return result


async def guarded_tool_call(
    tool_name: str, action: str, awaitable: Any
) -> ToolResult:
    """executor 编排层的异常守护（HTTPException 与未知异常统一收敛）。

    - HTTPException：受控异常，detail 是业务语义（401/403 等），可交给
      Agent；
    - 未知异常：str(e) 可能内嵌路径/凭据片段，不外显——traceback 只进
      日志，Agent 拿统一错误 + request id（``internal_error_result``）。
    executor 的 handler 分支与 executor_ref 分支共用，避免两处重复。
    """
    try:
        return await awaitable
    except HTTPException as exc:
        return http_error_result(tool_name, exc)
    except Exception:
        result = internal_error_result(tool_name, action)
        logger.error(
            "[Agent] tool %s %s error: %s", tool_name, action, result.error,
            exc_info=True,
        )
        return result


__all__ = [
    "build_call_kwargs",
    "cached_signature","call_router_function", "enforce_route_dependencies", "guarded_tool_call"]
