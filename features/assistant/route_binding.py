"""executor_ref 调用的参数绑定层。

从 executor.py 拆出的 HTTP 模拟层：把 Agent 工具参数绑定到
FastAPI 路由函数的 query / body / route-dependency 形参上。
不负责工具解析与授权——executor.py 只保留
resolve tool → authorize → build invocation → execute → normalize。
"""

from __future__ import annotations

import inspect
from typing import Any

from fastapi import HTTPException

from features.assistant.tools import AgentTool


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
        from features.firmware import SNBurnRequest

        # features.knowledge 的公共面不含请求模型;经延迟导入引用其 API
        # 模块(跨 feature 深层内部 import 会被依赖门禁拦截)。
        from features.knowledge import external_api as _knowledge_external_api
        from features.reports import ReportDiagnosisRequest
        from features.system import VNCStartRequest, VPNConnectRequest
        from features.test_execution import (
            SuiteApkAnalyzeRequest,
            TestParseArgsRequest,
            TestStartRequest,
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
            "burn_serial": SNBurnRequest,
            # knowledge external search: 请求体经 ExternalSearchRequest 建模,
            # android_api_level 的范围校验(1-1000)在此生效。
            "android_internals_search": _knowledge_external_api.ExternalSearchRequest,
        }
    return _MODEL_BY_TOOL


def build_call_kwargs(
    func: Any, tool: AgentTool, request: Any, params: dict[str, Any]
) -> dict[str, Any]:
    from features.assistant.api import AgentRequestShim

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
    if tool.name == "devices_shell" and "serial_no" not in body:
        devices = body.get("devices") or []
        if devices:
            body["serial_no"] = devices[0]
    if tool.name == "burn_serial" and "sn_code" not in body:
        body["sn_code"] = body.get("serial") or body.get("sn") or ""
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


__all__ = [
    "body_params_for_tool",
    "build_call_kwargs",
    "cached_signature",
    "get_model_by_tool",
    "query_params_for_tool",
]
