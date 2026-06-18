"""
Agent Action Executor — 统一执行层。

调用现有 router 函数，返回标准化的 ToolResult。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from core.agent_tools import AgentTool, registry
from core.config import config_manager
from features.devices.locks import device_lock_manager
from features.devices.manager import device_manager
from features.devices.support import get_or_create_user_state
from features.redmine.repository import (
    _name_keys,
    _norm_name,
    display_names_from_mapping,
    find_user_mapping,
    load_redmine_user_map,
)


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
}

_CATEGORY_LABELS = {
    "device": "设备",
    "test": "测试/套件",
    "report": "报告/诊断",
    "apk": "APK/源码",
    "desktop": "桌面",
    "terminal": "终端",
    "users": "用户",
    "config": "配置",
    "system": "系统",
    "ssh": "SSH",
    "vpn": "VPN",
    "usbip": "USB/IP",
    "file": "文件/搜索",
    "burn": "烧写",
    "audit": "审计",
    "assets": "网址/工具",
    "agent": "Agent",
    "redmine": "Redmine",
    "gerrit": "Gerrit",
}

_UNSUPPORTED_DIRECT_TOOLS = {
    "apk_upload",
    "terminal_push",
    "test_logs_stream",
    "system_websocket_{client_id}",
    "burn_firmware",
    "burn_gsi",
}

# Request model mapping for tools that accept a body — built once at module level.
_MODEL_BY_TOOL = None  # lazily initialized to avoid circular imports

# Cache of inspect.signature(func) — a function's signature never changes, so the
# repeated signature introspection on every tool call (now ~40+ generic tools) is pure waste.
_SIGNATURE_CACHE: dict[Any, inspect.Signature] = {}


def _cached_signature(func: Any) -> inspect.Signature:
    sig = _SIGNATURE_CACHE.get(func)
    if sig is None:
        sig = inspect.signature(func)
        _SIGNATURE_CACHE[func] = sig
    return sig


def _get_model_by_tool() -> dict[str, type]:
    """Lazy-initialised mapping of tool names to Pydantic request models."""
    global _MODEL_BY_TOOL
    if _MODEL_BY_TOOL is None:
        from core.schemas import (
            ADBForwardStartRequest,
            ClientInfoRequest,
            DeviceActionRequest,
            DeviceLockRequest,
            DeviceShellRequest,
            ReportDiagnosisRequest,
            SNBurnRequest,
            SuiteApkAnalyzeRequest,
            TestParseArgsRequest,
            TestStartRequest,
            TradefedListResultsRequest,
            USBIPDisconnectRequest,
            USBIPStartRequest,
            VNCStartRequest,
            VPNConnectRequest,
            WifiConnectRequest,
        )
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
        }
    return _MODEL_BY_TOOL


# ==================== Result ====================

@dataclass
class ToolResult:
    """工具执行结果。"""
    success: bool
    tool_name: str
    data: Any = None
    formatted_text: str = ""
    quick_actions: list[dict[str, Any]] = field(default_factory=list)
    page: str = ""
    kind: str = "text"  # text / table / status / file / code
    entities: dict[str, list[str]] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "tool_name": self.tool_name,
            "data": self.data,
            "formatted_text": self.formatted_text,
            "quick_actions": self.quick_actions,
            "page": self.page,
            "kind": self.kind,
            "entities": self.entities,
            "error": self.error,
        }


# ==================== Executor ====================

class ActionExecutor:
    """统一执行层，按工具名调用对应的查询/操作函数。"""

    def __init__(self) -> None:
        self._handlers = {
            # Query handlers (readonly)
            "devices_list": self._query_devices,
            "devices_management": self._query_devices_management,
            "devices_user_locked": self._query_locked_devices,
            "devices_info": self._query_device_info,
            "test_suites": self._query_suites,
            "test_status": self._query_test_status,
            "reports_list": self._query_reports,
            "users_list": self._query_users,
            "system_health": self._query_health,
            "config_read": self._query_config,
            "vpn_status": self._query_vpn_status,
            "desktop_vnc_status": self._query_vnc_status,
            "terminal_open": self._query_terminal,
            "usbip_status": self._query_usbip_status,
            "apk_tasks": self._query_apk_tasks,
            "ssh_sshd": self._query_ssh_status,
            "agent_capabilities": self._query_capabilities,
            "redmine_workload_stats": self._query_redmine_workload_stats,
            "redmine_department_stats": self._query_redmine_department_stats,
            "redmine_stats_config": self._query_redmine_stats_config,
            "gerrit_dashboard_config": self._query_gerrit_dashboard_config,
            "gerrit_dashboard_changes": self._query_gerrit_dashboard_changes,
            "knowledgebase_search": self._query_knowledgebase_search,
            "knowledgebase_stats": self._query_knowledgebase_stats,
            "security_audit_logs": self._query_security_audit_logs,
            "devices_wifi": self._connect_wifi,
        }

    async def execute(
        self,
        session: dict[str, Any],
        request: Any,
        tool_name: str,
        params: dict[str, Any],
    ) -> ToolResult:
        """执行工具调用。"""
        tool = registry.get(tool_name)
        if not tool:
            return ToolResult(
                success=False, tool_name=tool_name,
                error=f"未知工具: {tool_name}",
            )

        handler = self._handlers.get(tool_name)
        if handler:
            try:
                return await handler(session, request, params)
            except Exception as e:
                logger.error("[Agent] executor error for %s: %s", tool_name, e, exc_info=True)
                return ToolResult(
                    success=False, tool_name=tool_name,
                    error=str(e), formatted_text=f"执行失败: {e}",
                )

        # 对于没有专用 handler 的工具，尝试调用 router 函数
        if tool.executor_ref:
            try:
                return await self._call_router_function(tool, session, request, params)
            except Exception as e:
                logger.error("[Agent] router call error for %s: %s", tool_name, e, exc_info=True)
                return ToolResult(
                    success=False, tool_name=tool_name,
                    error=str(e), formatted_text=f"调用失败: {e}",
                )

        return ToolResult(
            success=False, tool_name=tool_name,
            error=f"工具 {tool_name} 暂未实现执行逻辑",
            formatted_text=f"抱歉，工具「{tool.display_name}」暂未实现。请在对应页面操作。",
        )

    # ==================== Query Helpers ====================

    @staticmethod
    async def _fetch_router_json(module_path: str, func_name: str, tool_name_for_error: str = "", **kwargs) -> tuple:
        """Import and call an async router function, returning (error_result | None, payload_dict).

        Returns (None, payload) on success, or (error_ToolResult, {}) on failure.
        kwargs are forwarded to the router function.
        """
        try:
            module = importlib.import_module(module_path)
            func = getattr(module, func_name)
            response = await func(**kwargs) if kwargs else await func()
            return None, _json_body(response)
        except Exception as e:
            label = tool_name_for_error or func_name
            return ToolResult(
                success=False, tool_name=label,
                error=str(e), formatted_text=f"查询失败: {e}",
            ), {}

    # ==================== Query Handlers ====================

    async def _query_devices(self, session, request, params) -> ToolResult:
        """查询设备列表。"""
        query = str(params.get("query") or "").strip().lower()
        details = await self._load_device_summaries()
        if query == "available":
            details = [d for d in details if not d.get("locked")]
        elif query == "locked":
            details = [d for d in details if d.get("locked")]
        elif query:
            details = [
                d for d in details
                if query in " ".join(str(d.get(k, "")).lower() for k in (
                    "device_id", "serial_no", "model", "soc_model", "android_version", "source_host"
                ))
            ]

        locked = [d for d in details if d["locked"]]
        unlocked = [d for d in details if not d["locked"]]
        lines = []
        for d in details[:12]:
            state = f"🔒 {d.get('locked_by') or '占用'}" if d.get("locked") else "✅ 空闲"
            desc = " | ".join(
                part for part in [
                    d.get("model") or "",
                    d.get("soc_model") or "",
                    f"Android {d.get('android_version')}" if d.get("android_version") else "",
                    d.get("source_type") or "",
                ] if part
            )
            lines.append(f"{state} {d.get('device_id')}" + (f" | {desc}" if desc else ""))
        prefix = f"匹配「{query}」的设备" if query and query not in {"available", "locked"} else "当前设备"
        text = (
            f"{prefix} {len(details)} 台，空闲 {len(unlocked)} 台，占用 {len(locked)} 台。\n"
            + ("\n".join(f"- {line}" for line in lines) if lines else "- 未找到匹配设备")
        )

        return ToolResult(
            success=True, tool_name="devices_list", data={"devices": details},
            formatted_text=text, kind="table", page="devices",
            entities={"devices": [d["device_id"] for d in unlocked[:8]]},
            quick_actions=[
                {"label": "查看设备管理", "page": "devices"},
                {"label": "查看第一台详情", "action": "devices_info", "params": {"devices": [unlocked[0]["device_id"]]} if unlocked else {}},
            ],
        )

    async def _load_device_summaries(self) -> list[dict[str, Any]]:
        """Load device summaries, preferring management payload when available."""
        try:
            from core.ssh import ssh_manager
            from features.devices.api import (
                _build_devices_management_payload,
                _build_management_props_command,
                _parse_management_device_props,
            )

            config = config_manager.load_config()
            device_ids = await asyncio.to_thread(device_manager.get_connected_devices, True)
            device_data: dict[str, dict[str, str]] = {}
            ssh = ssh_manager.get_connection(config)
            if ssh and device_ids:
                try:
                    command = _build_management_props_command(device_ids)
                    output, _, _ = ssh_manager.execute_command(ssh, command, timeout=15)
                    device_data = _parse_management_device_props(output)
                finally:
                    ssh_manager.return_connection(ssh)
            payload = _build_devices_management_payload(device_ids, device_data, config)
            items = payload.get("devices") or []
            if items:
                for item in items:
                    item["locked"] = bool(item.get("locked_by"))
                return items
        except Exception as e:
            logger.info("[Agent] device management summary fallback: %s", e)

        device_ids = await asyncio.to_thread(device_manager.get_connected_devices, True)
        details = []
        for did in device_ids:
            lock = device_lock_manager.get_lock_status(did)
            details.append({
                "device_id": did,
                "serial_no": did,
                "status": "online",
                "locked": bool(lock),
                "locked_by": lock.get("locked_by", "") if lock else "",
            })
        return details

    async def _connect_wifi(self, session, request, params) -> ToolResult:
        """连接设备到 WiFi。未指定设备时默认选择一台空闲设备。"""
        from features.devices.api import connect_wifi
        from features.devices.models import WifiConnectRequest

        devices = list(params.get("devices") or [])
        if not devices:
            connected = await asyncio.to_thread(device_manager.get_connected_devices, True)
            devices = [
                device_id
                for device_id in connected
                if not device_lock_manager.get_lock_status(device_id)
            ][:1]

        if not devices:
            return ToolResult(
                success=False,
                tool_name="devices_wifi",
                error="没有可用未占用设备",
                formatted_text="当前没有可用未占用设备，无法连接 WiFi。",
                page="devices",
            )

        wifi_defaults = config_manager.get_wifi_defaults()
        req = WifiConnectRequest(
            devices=devices,
            ssid=params.get("ssid") or wifi_defaults["ssid"],
            password=params.get("password") or wifi_defaults["password"],
        )
        response = await connect_wifi(req)
        payload = _json_body(response)
        summary = payload.get("summary") or {}
        success_count = summary.get("success", 0)
        total = summary.get("total", len(devices))
        text = f"WiFi 连接完成：成功 {success_count}/{total}，设备：{', '.join(devices)}"
        return ToolResult(
            success=payload.get("success", True),
            tool_name="devices_wifi",
            data=payload,
            formatted_text=text,
            page="devices",
            entities={"devices": devices},
            error=payload.get("error", ""),
        )

    async def _query_devices_management(self, session, request, params) -> ToolResult:
        """查询设备管理信息（详细版）。"""
        result = await self._query_devices(session, request, params)
        result.tool_name = "devices_management"
        return result

    async def _query_locked_devices(self, session, request, params) -> ToolResult:
        """查询锁定设备。"""
        locks = device_lock_manager.get_all_locks()
        lines = [f"- {did}: {info.get('locked_by', 'unknown')}" for did, info in locks.items()]
        text = f"当前锁定设备 {len(locks)} 台。\n" + ("\n".join(lines) if lines else "- 无锁定设备")
        return ToolResult(
            success=True, tool_name="devices_user_locked",
            data={"locks": locks}, formatted_text=text, kind="table", page="devices",
        )

    async def _query_device_info(self, session, request, params) -> ToolResult:
        """查询设备详细信息。"""
        devices = params.get("devices", [])
        if not devices:
            return ToolResult(success=False, tool_name="devices_info", error="未指定设备")

        # 通过 router 调用
        tool = registry.get("devices_info")
        if tool and tool.executor_ref:
            return await self._call_router_function(tool, session, request, {
                "devices": devices,
            })

        return ToolResult(
            success=True, tool_name="devices_info",
            data={"devices": devices},
            formatted_text=f"设备 {', '.join(devices)} — 详细信息请查看设备管理页面",
            page="devices",
        )

    async def _query_suites(self, session, request, params) -> ToolResult:
        """查询测试套件。"""
        from core.test_suite_utils import get_default_suites_path
        from routers.tests import _get_available_test_suites

        config = config_manager.load_config()
        base_path = config.get("suites_path") or get_default_suites_path(config)
        suites = await asyncio.to_thread(_get_available_test_suites, config, base_path)

        counts = {}
        for s in suites:
            key = str(s.get("test_type", "unknown")).upper()
            counts[key] = counts.get(key, 0) + 1
        count_line = "，".join(f"{k}: {v}" for k, v in sorted(counts.items()))
        lines = [
            f"- {s.get('test_type', '').upper()} {s.get('version') or s.get('binary') or '-'} → {s.get('tools_path', '-')}"
            for s in suites[:10]
        ]
        text = f"发现 {len(suites)} 个测试套件。按类型：{count_line}\n" + "\n".join(lines)

        return ToolResult(
            success=True, tool_name="test_suites",
            data={"suites": suites[:20], "count": len(suites)},
            formatted_text=text, kind="table", page="test-suites",
            entities={"suites": [s.get("tools_path", "") for s in suites[:8]]},
            quick_actions=[
                {"label": "打开测试套件页", "page": "test-suites"},
            ],
        )

    async def _query_test_status(self, session, request, params) -> ToolResult:
        """查询测试运行状态。"""
        client_id = session.get("client_id", "")
        user_state = get_or_create_user_state(client_id)
        running = user_state.get("running", False)
        devices = user_state.get("devices", [])
        logs = user_state.get("logs", [])
        tail = [str(item)[-120:] for item in list(logs)[-5:]]

        text = (
            f"测试状态：{'🔴 运行中' if running else '⚪ 未运行'}\n"
            f"占用设备：{', '.join(devices) or '无'}\n"
            f"日志条数：{len(logs)}\n"
            + ("\n最近日志：\n" + "\n".join(f"- {line}" for line in tail) if tail else "")
        )
        return ToolResult(
            success=True, tool_name="test_status",
            data={"running": running, "devices": devices, "log_count": len(logs)},
            formatted_text=text, kind="status", page="test",
            quick_actions=[
                {"label": "查看测试界面", "page": "test"},
                {"label": "停止测试", "action": "test_stop"} if running else None,
            ],
        )

    async def _query_reports(self, session, request, params) -> ToolResult:
        """查询测试报告。"""
        from features.reports.repository import test_report_db

        reports = test_report_db.get_reports(limit=10)
        stats = test_report_db.get_statistics()
        lines = [
            f"- {r.get('timestamp', '-')} | {r.get('test_type', '?')} | pass {r.get('pass', 0)} fail {r.get('fail', 0)} total {r.get('total', 0)}"
            for r in reports
        ]
        text = (
            f"报告共 {stats.get('total_reports', len(reports))} 份，最近7天 {stats.get('recent_week', 0)} 份。\n"
            + "\n".join(lines) if lines else "暂无报告"
        )
        return ToolResult(
            success=True, tool_name="reports_list",
            data={"reports": reports, "stats": stats},
            formatted_text=text, kind="table", page="reports",
            entities={"reports": [r.get("timestamp", "") for r in reports[:8]]},
            quick_actions=[
                {"label": "打开报告管理", "page": "reports"},
                {"label": "打开报告分析", "page": "report-analysis"},
            ],
        )

    async def _query_users(self, session, request, params) -> ToolResult:
        """查询在线用户。"""
        from routers.users import list_users

        response = await list_users()
        payload = _json_body(response)
        users = payload.get("users", [])
        lines = [
            f"- {u.get('username')}@{u.get('ip')} | {'测试中' if u.get('running') else '空闲'} | 设备: {', '.join(u.get('devices') or []) or '-'}"
            for u in users[:10]
        ]
        text = f"在线用户 {payload.get('total', len(users))} 个。\n" + "\n".join(lines)
        return ToolResult(
            success=True, tool_name="users_list",
            data={"users": users, "total": payload.get("total", len(users))},
            formatted_text=text, kind="table", page="users",
            entities={"users": [u.get("username", "") for u in users[:8]]},
        )

    async def _query_health(self, session, request, params) -> ToolResult:
        """查询系统健康。"""
        from routers.system import health_check

        response = await health_check()
        payload = _json_body(response)
        modules = payload.get("modules", {})
        text = (
            f"系统状态：{payload.get('status', 'unknown')}\n"
            f"服务：{payload.get('service', '-')}\n"
            f"WebSocket 连接：{payload.get('websocket_connections', 0)}\n"
            f"模块：{', '.join(f'{k}={v}' for k, v in modules.items())}"
        )
        return ToolResult(
            success=True, tool_name="system_health",
            data=payload, formatted_text=text, kind="status", page="security-audit",
        )

    async def _query_config(self, session, request, params) -> ToolResult:
        """查询配置摘要。"""
        config = config_manager.load_config()
        safe = {
            "ubuntu_host": config.get("ubuntu_host"),
            "ubuntu_user": config.get("ubuntu_user"),
            "local_server": config.get("local_server"),
            "suites_path": config.get("suites_path"),
            "ai_enabled": bool((config.get("ai_models") or {}).get("enabled")),
            "opengrok": bool((config.get("opengrok") or {}).get("base_url")),
        }
        lines = [f"- {k}: {v}" for k, v in safe.items()]
        return ToolResult(
            success=True, tool_name="config_read",
            data=safe, formatted_text="当前关键配置：\n" + "\n".join(lines),
            kind="table", page="api-docs",
        )

    async def _query_vpn_status(self, session, request, params) -> ToolResult:
        """查询 VPN 状态。"""
        result, payload = await self._fetch_router_json("routers.integrations", "get_vpn_status")
        if result is not None:
            return result
        text = f"VPN 状态：{payload.get('status', 'unknown')}"
        return ToolResult(
            success=True, tool_name="vpn_status",
            data=payload, formatted_text=text, kind="status",
            quick_actions=[
                {"label": "连接 VPN", "action": "vpn_connect"},
                {"label": "断开 VPN", "action": "vpn_disconnect"},
            ],
        )

    async def _query_vnc_status(self, session, request, params) -> ToolResult:
        """查询 VNC 状态。"""
        result, payload = await self._fetch_router_json("routers.desktop", "get_desktop_vnc_status")
        if result is not None:
            return result
        text = f"VNC 状态：{payload.get('status', 'unknown')}"
        return ToolResult(
            success=True, tool_name="desktop_vnc_status",
            data=payload, formatted_text=text, kind="status", page="desktop",
            quick_actions=[{"label": "打开桌面", "page": "desktop"}],
        )

    async def _query_terminal(self, session, request, params) -> ToolResult:
        """查询终端连接信息。"""
        result, payload = await self._fetch_router_json("routers.terminal", "get_ssh_terminal_info")
        if result is not None:
            return result
        text = f"终端连接：{payload.get('connection_command', 'ssh ' + payload.get('host', ''))}"
        return ToolResult(
            success=True, tool_name="terminal_open",
            data=payload, formatted_text=text, kind="status", page="terminal",
            quick_actions=[{"label": "打开终端", "page": "terminal"}],
        )

    async def _query_usbip_status(self, session, request, params) -> ToolResult:
        """查询 USB/IP 状态。"""
        result, payload = await self._fetch_router_json("features.devices.integrations_api", "get_usbip_status")
        if result is not None:
            return result
        return ToolResult(
            success=True, tool_name="usbip_status",
            data=payload, formatted_text=f"USB/IP 状态：{payload.get('status', 'unknown')}",
            kind="status",
        )

    async def _query_apk_tasks(self, session, request, params) -> ToolResult:
        """查询 APK 反编译任务。"""
        result, payload = await self._fetch_router_json("routers.apk", "list_apk_tasks")
        if result is not None:
            return result
        tasks = (payload.get("data") or {}).get("tasks", [])
        lines = [f"- {t.get('filename') or t.get('task_id')} | {t.get('status')} | {t.get('progress', 0)}%" for t in tasks[:8]]
        text = f"APK 任务 {len(tasks)} 个。\n" + ("\n".join(lines) if lines else "- 无任务")
        return ToolResult(
            success=True, tool_name="apk_tasks",
            data={"tasks": tasks}, formatted_text=text, kind="table", page="apk-analysis",
            entities={"tasks": [t.get("task_id", "") for t in tasks[:8]]},
        )

    async def _query_ssh_status(self, session, request, params) -> ToolResult:
        """查询 SSH 服务状态。"""
        return ToolResult(
            success=True, tool_name="ssh_sshd",
            formatted_text="请使用「系统接口」页面查看 SSH 服务状态",
            page="api-docs",
        )

    async def _query_capabilities(self, session, request, params) -> ToolResult:
        """返回 Agent 能力描述。"""
        categories = registry.get_all_categories()
        lines = []
        for cat, tools in categories.items():
            tool_names = [t.display_name for t in tools[:4]]
            lines.append(f"- {_CATEGORY_LABELS.get(cat, cat)}: {', '.join(tool_names)}")
        text = (
            "我是 GMS 远程测试平台的对话 Agent，可以帮你查询状态、生成测试计划、执行受控操作、分析失败和打开页面。\n\n"
            "常用能力：\n"
            "- 设备：查设备/型号/空闲占用，查看详情，WiFi、重启、remount、投屏。\n"
            "- 测试：查套件和状态，按模块/用例启动测试，失败 retry，后台监控结果。\n"
            "- 报告：列最近报告、下载/删除报告、分析失败、生成诊断线索。\n"
            "- Redmine：查询个人/部门统计、超阈值未回复问题、统计设置和 RedmineAgent 页面。\n"
            "- Gerrit：打开 Gerrit 看板、读取 dashboard 配置、按配置查询变更。\n"
            "- APK/源码：查看反编译任务，上传 APK/JAR 需到页面，支持套件 APK 源码分析。\n"
            "- 运维：终端、桌面、VPN、USB/IP、SSH、配置、系统健康、安全审计。\n\n"
            "已注册工具概览：\n" + "\n".join(lines[:12]) + "\n\n"
            "有风险或会改变状态的操作会先给计划，确认后再执行。"
        )
        return ToolResult(
            success=True, tool_name="agent_capabilities",
            formatted_text=text, kind="text", page="agent",
            data={"categories": {cat: [t.name for t in tools] for cat, tools in categories.items()}},
            quick_actions=[
                {"label": "查看设备", "action": "devices_list", "params": {}},
                {"label": "测试套件", "action": "test_suites", "params": {}},
                {"label": "最近报告", "action": "reports_list", "params": {}},
                {"label": "打开测试界面", "page": "test"},
            ],
        )

    async def _query_redmine_stats_config(self, session, request, params) -> ToolResult:
        cfg = config_manager.get_redmine_stats_config()
        dashboard = config_manager.get_redmine_dashboard_config()
        profiles = ", ".join(profile.get("name", "") for profile in dashboard.get("profiles", []))
        text = (
            f"Redmine 统计设置：未回复阈值 {cfg['stale_days']} 天，统计窗口 {cfg['window_days']} 天，"
            f"缓存 {cfg['cache_ttl']} 秒。\n部门看板：{profiles or '未配置'}"
        )
        return ToolResult(
            success=True,
            tool_name="redmine_stats_config",
            data={"stats": cfg, "dashboard": dashboard},
            formatted_text=text,
            kind="status",
            page="redmine-agent",
            quick_actions=[{"label": "打开 Redmine 看板", "page": "redmine-agent", "params": {"tab": "department"}}],
        )

    async def _query_redmine_department_stats(self, session, request, params) -> ToolResult:
        result, payload = await self._fetch_router_json(
            "features.redmine.api", "get_department_overdue_statistics",
            tool_name_for_error="redmine_department_stats",
            stale_days=params.get("stale_days"),
            list_limit=None,
            issue_limit=None,
            profile_id=str(params.get("profile_id") or ""),
            refresh=True,
        )
        if result is not None:
            return result
        data = payload.get("data") or payload
        summary = data.get("summary") or {}
        profile = data.get("profile") or {}
        top_users = sorted(data.get("users") or [], key=lambda item: int(item.get("no_reply_3_days") or 0), reverse=True)[:5]
        lines = [
            f"{profile.get('name') or '部门'} Redmine 统计：配置用户 {summary.get('user_count', 0)}，"
            f"未 Close {summary.get('open_count', 0)}，待回复 {summary.get('waiting_my_reply', 0)}，"
            f"超阈值未回复 {summary.get('no_reply_3_days', 0)}。",
            "",
            "| 人员 | 超阈值未回复 | 最长未回复天数 | 待回复 |",
            "| --- | ---: | ---: | ---: |",
        ]
        for user in top_users:
            lines.append(
                f"| {user.get('name') or '-'} | {user.get('no_reply_3_days', 0)} | "
                f"{user.get('max_unreplied_days', 0)} | {user.get('waiting_my_reply', 0)} |"
            )
        return ToolResult(
            success=True,
            tool_name="redmine_department_stats",
            data=data,
            formatted_text="\n".join(lines),
            kind="table",
            page="redmine-agent",
            quick_actions=[{"label": "打开部门看板", "page": "redmine-agent", "params": {"tab": "department"}}],
        )

    async def _query_gerrit_dashboard_config(self, session, request, params) -> ToolResult:
        cfg = config_manager.get_gerrit_dashboard_config()
        profiles = ", ".join(profile.get("name", "") for profile in cfg.get("dashboard_profiles", []))
        text = (
            f"Gerrit 看板配置：base_url={cfg.get('base_url') or '-'}，ssh={cfg.get('ssh_user') or '-'}@"
            f"{cfg.get('ssh_host') or '-'}:{cfg.get('ssh_port')}，profiles={profiles or '-'}。"
        )
        return ToolResult(
            success=True,
            tool_name="gerrit_dashboard_config",
            data=cfg,
            formatted_text=text,
            kind="status",
            page="gerrit-dashboard",
            quick_actions=[{"label": "打开 Gerrit 看板", "page": "gerrit-dashboard"}],
        )

    async def _query_gerrit_dashboard_changes(self, session, request, params) -> ToolResult:
        result, payload = await self._fetch_router_json(
            "features.gerrit.api", "list_gerrit_changes",
            tool_name_for_error="gerrit_dashboard_changes",
            profile_id=str(params.get("profile_id") or ""),
            query=str(params.get("query") or ""),
        )
        if result is not None:
            return result
        data = payload.get("data") or payload
        if data.get("error"):
            text = f"Gerrit 查询失败：{data['error']}"
        elif not data.get("configured"):
            text = data.get("message") or "Gerrit 尚未配置。"
        else:
            items = data.get("items") or []
            lines = [f"Gerrit 查询结果 {len(items)} 条："]
            for item in items[:8]:
                lines.append(f"- #{item.get('number') or item.get('id')} {item.get('subject') or '-'} [{item.get('status') or '-'}]")
            text = "\n".join(lines)
        return ToolResult(
            success=True,
            tool_name="gerrit_dashboard_changes",
            data=data,
            formatted_text=text,
            kind="table",
            page="gerrit-dashboard",
            quick_actions=[{"label": "打开 Gerrit 看板", "page": "gerrit-dashboard"}],
        )

    async def _query_knowledgebase_search(self, session, request, params) -> ToolResult:
        """搜索本地 GMS 知识库（参数名映射：q/query → query）。"""
        query = str(params.get("query") or params.get("q") or "").strip()
        if not query:
            return ToolResult(
                success=False, tool_name="knowledgebase_search",
                error="缺少搜索关键词", formatted_text="请提供要搜索的关键词。",
                page="report-analysis",
            )
        try:
            limit = max(1, min(int(params.get("limit") or 8), 20))
        except (TypeError, ValueError):
            limit = 8
        result, payload = await self._fetch_router_json(
            "features.reports.api", "knowledgebase_search",
            tool_name_for_error="knowledgebase_search", query=query, limit=limit,
        )
        if result is not None:
            return result
        data = payload.get("data") or payload
        items = data.get("results") or []
        lines = [f"知识库搜索「{query}」命中 {data.get('count', len(items))} 条："]
        for item in items[:8]:
            title = str(item.get("title") or item.get("subject") or "-")
            score = item.get("score")
            lines.append(f"- {title}" + (f"（相关度 {score}）" if score else ""))
        return ToolResult(
            success=True, tool_name="knowledgebase_search",
            data=data, formatted_text="\n".join(lines) if items else f"知识库未命中「{query}」。",
            kind="table", page="report-analysis",
        )

    async def _query_knowledgebase_stats(self, session, request, params) -> ToolResult:
        """知识库统计。"""
        result, payload = await self._fetch_router_json(
            "features.reports.api",
            "knowledgebase_stats",
        )
        if result is not None:
            return result
        stats = (payload.get("data") or payload).get("stats") or {}
        text = "知识库统计：" + "，".join(f"{k}={v}" for k, v in stats.items()) if stats else "知识库为空或未生成。"
        return ToolResult(
            success=True, tool_name="knowledgebase_stats",
            data={"stats": stats}, formatted_text=text, kind="status", page="report-analysis",
        )

    async def _query_security_audit_logs(self, session, request, params) -> ToolResult:
        """查询安全审计日志。"""
        kwargs = {"limit": max(1, min(int(params.get("limit") or 50), 1000))}
        if params.get("source"):
            kwargs["source"] = str(params.get("source"))
        if params.get("action_type"):
            kwargs["action_type"] = str(params.get("action_type"))
        if params.get("q"):
            kwargs["q"] = str(params.get("q"))
        result, payload = await self._fetch_router_json(
            "routers.audit", "list_security_audit_logs",
            tool_name_for_error="security_audit_logs", **kwargs,
        )
        if result is not None:
            return result
        data = payload.get("data") or payload
        events = data.get("records") or []
        lines = [f"安全审计日志 {len(events)} 条："]
        for ev in events[:10]:
            ts = str(ev.get("timestamp") or "")[:19]
            who = ev.get("username") or ev.get("source") or "-"
            act = ev.get("operation") or ev.get("action_type") or ev.get("path") or "-"
            lines.append(f"- [{ts}] {who} | {act}")
        return ToolResult(
            success=True, tool_name="security_audit_logs",
            data=data, formatted_text="\n".join(lines) if events else "暂无审计日志。",
            kind="table", page="security-audit",
            quick_actions=[{"label": "打开安全审计", "page": "security-audit"}],
        )

    async def _query_redmine_workload_stats(self, session, request, params) -> ToolResult:
        """统计一个或多个人员的 Redmine 工作量。"""
        from features.redmine.api import _resolve_owner_names, redmine_service

        raw_names = params.get("names") or []
        if isinstance(raw_names, str):
            raw_names = [raw_names]
        names = [str(name).strip() for name in raw_names if str(name).strip()]
        stale_days = int(params.get("stale_days") or 3)

        if not names:
            try:
                names = await _resolve_owner_names()
            except Exception:
                names = []
        if not names:
            return ToolResult(
                success=False,
                tool_name="redmine_workload_stats",
                formatted_text="没有识别到要统计的人员，请指定姓名，例如：统计 卞金晨 Redmine 信息。",
                page="redmine-agent",
                error="missing names",
            )

        user_map = load_redmine_user_map()
        resolved = redmine_service.repository.resolve_assignee_names(names)
        try:
            window_days = int((config_manager.load_config().get("redmine_stats") or {}).get("window_days") or 0)
        except Exception:
            window_days = 0
        rows = []
        for requested_name in names:
            matched_names, stats = await self._resolve_user_stats(
                redmine_service.agent,
                redmine_service.repository,
                requested_name,
                resolved,
                user_map,
                stale_days,
                window_days,
            )
            rows.append({
                "requested_name": requested_name,
                "matched_names": matched_names,
                "stats": stats,
            })

        header = "| 人员 | 匹配到的 Redmine 指派人 | 历史数量 | 未 Close | 待回复 | 超阈值未回复 | 缺测试报告 | 已解决/关闭 |"
        sep = "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"
        lines = [f"Redmine 统计结果（未回复阈值 {stale_days} 天）：", "", header, sep]
        for row in rows:
            stats = row["stats"]
            matched_text = " / ".join(row["matched_names"])
            lines.append(
                f"| {row['requested_name']} | {matched_text} | {stats.get('total_owned', 0)} | "
                f"{stats.get('open_count', 0)} | {stats.get('waiting_my_reply', 0)} | "
                f"{stats.get('no_reply_3_days', 0)} | {stats.get('missing_test_report', 0)} | "
                f"{stats.get('closed_count', 0)} |"
            )

        zero_rows = [row["requested_name"] for row in rows if row["stats"].get("total_owned", 0) == 0]
        if zero_rows:
            lines.append("")
            lines.append(
                "提示：以下人员未在本地 RedmineAgent 缓存或当前 Redmine 可访问范围内匹配到问题："
                + "、".join(zero_rows)
                + "。如果 Redmine 禁止用户搜索，需要先同步到这些人的问题，或补充姓名到 Redmine 用户 ID 的映射。"
            )
        if any(find_user_mapping(row["requested_name"]) for row in rows):
            lines.append("")
            lines.append("口径：历史数量、未 Close、已解决/关闭来自 Redmine 实时 count；待回复、超阈值未回复、缺测试报告依赖本地已同步的 journal/附件详情。")

        for row in rows:
            stats = row["stats"]
            waiting = (stats.get("lists") or {}).get("waiting_my_reply") or []
            stale = (stats.get("lists") or {}).get("no_reply_3_days") or []
            missing = (stats.get("lists") or {}).get("missing_test_report") or []
            lines.append("")
            lines.append(f"{row['requested_name']} 重点问题：")
            lines.append(self._format_redmine_issue_lines("待回复", waiting))
            lines.append(self._format_redmine_issue_lines("超阈值未回复", stale))
            lines.append(self._format_redmine_issue_lines("缺测试报告", missing))

        return ToolResult(
            success=True,
            tool_name="redmine_workload_stats",
            data={"rows": rows, "stale_days": stale_days},
            formatted_text="\n".join(lines),
            kind="table",
            page="redmine-agent",
            entities={"redmine_users": names},
            quick_actions=[
                {
                    "label": "打开 Redmine 统计页",
                    "page": "redmine-agent",
                    "params": {"name": names[0]} if len(names) == 1 else {},
                }
            ],
        )

    async def _resolve_user_stats(
        self, agent: Any, db: Any, requested_name: str,
        resolved: dict[str, list[str]], user_map: list[dict], stale_days: int,
        window_days: int = 0,
    ) -> tuple:
        """Resolve a single user's Redmine stats: name matching → live counts → workload.

        Encapsulates the "try user map → try sync fallback" flow for one person.
        Returns (matched_names, stats_dict).
        """
        matched_names = resolved.get(requested_name) or [requested_name]
        mapped_user = find_user_mapping(requested_name) if user_map else {}
        live_counts = {}

        if mapped_user:
            matched_names = display_names_from_mapping(mapped_user)
            live_counts = await self._count_redmine_user_for_stats(agent, mapped_user)

        stats = db.get_workload_statistics(
            owner_names=matched_names, stale_days=stale_days,
            list_limit=5, display_names=matched_names,
            window_days=window_days,
        )

        # Fallback: if no local data, try syncing from Redmine
        if not mapped_user and (stats.get("total_owned", 0) == 0 or matched_names == [requested_name]):
            live_names = await self._sync_redmine_user_for_stats(agent, requested_name)
            if live_names:
                matched_names = live_names
                resolved[requested_name] = live_names
                stats = db.get_workload_statistics(
                    owner_names=matched_names, stale_days=stale_days,
                    list_limit=5, display_names=matched_names,
                    window_days=window_days,
                )

        if live_counts:
            stats.update(live_counts)
        return matched_names, stats

    async def _count_redmine_user_for_stats(self, agent: Any, user: dict[str, Any]) -> dict[str, int]:
        client = agent._make_client()
        try:
            data = await client.count_issues_by_assignee(int(user["id"]))
            data.update(await client.resolved_trends_by_assignee(int(user["id"])))
            return data
        except Exception as exc:
            logger.info("[Agent] Redmine user count failed for %s: %s", user.get("id"), exc)
            return {}

    async def _sync_redmine_user_for_stats(self, agent: Any, requested_name: str) -> list[str]:
        """Try to find and sync a Redmine user by name. Returns display names on success."""
        client = agent._make_client()
        mapped_user = find_user_mapping(requested_name)
        if mapped_user:
            await self._sync_redmine_user_issues(agent, client, mapped_user)
            return display_names_from_mapping(mapped_user)

        # Single search with the compact name — good enough for most cases
        compact = _norm_name(requested_name).replace(" ", "")
        try:
            candidates = await client.search_users(compact or requested_name, limit=10)
        except Exception as exc:
            logger.info("[Agent] Redmine user search failed for %s: %s", requested_name, exc)
            return []
        if not candidates:
            return []

        query_keys = _name_keys(requested_name)
        for candidate in candidates:
            if not candidate.get("id"):
                continue
            values = [
                candidate.get("name") or "",
                f"{candidate.get('lastname', '')} {candidate.get('firstname', '')}".strip(),
                candidate.get("mail") or "",
            ]
            if any(query_keys.intersection(_name_keys(v)) for v in values):
                best_names = list(dict.fromkeys([
                    candidate.get("name") or "",
                    f"{candidate.get('lastname', '')} {candidate.get('firstname', '')}".strip(),
                ]))
                await self._sync_redmine_user_issues(
                    agent, client, {"id": candidate["id"], "name": best_names[0] or requested_name},
                )
                return [n for n in best_names if n]

        return []

    async def _sync_redmine_user_issues(self, agent: Any, client: Any, user: dict[str, Any]) -> None:
        from features.redmine.agent import RESOLVED_STATUSES

        issues = await client.fetch_issues_by_assignee(int(user["id"]), status_id="*", limit=2000)
        display_names = display_names_from_mapping(user)
        detail_refreshed = 0
        for issue_stub in issues:
            issue_id = int(issue_stub.id)
            existing = agent.db.get_issue(issue_id)
            payload = agent._stub_to_dict(issue_stub, f"agent-user-{user['id']}")
            status_name = payload.get("status_name") or ""
            payload["is_resolved"] = 1 if status_name in RESOLVED_STATUSES else 0
            if not payload.get("assigned_to_name"):
                payload["assigned_to_name"] = display_names[0]
            if payload["is_resolved"] == 0 and detail_refreshed < 30:
                try:
                    detail = await agent.fetch_issue_snapshot(client, issue_id, payload.get("run_id") or "")
                    if existing:
                        detail["attachments_json"] = agent._merge_attachment_analysis(
                            existing.get("attachments_json") or [],
                            detail.get("attachments_json") or [],
                        )
                    payload.update({k: v for k, v in detail.items() if v not in (None, "", [], {})})
                    detail_refreshed += 1
                except Exception as exc:
                    logger.info("[Agent] Redmine detail refresh failed for #%s: %s", issue_id, exc)
            if existing:
                agent._preserve_existing_analysis_fields(payload, existing)
            agent.db.upsert_issue(payload)

    @staticmethod
    def _format_redmine_issue_lines(label: str, issues: list[dict[str, Any]]) -> str:
        if not issues:
            return f"- {label}: 无"
        parts = []
        for item in issues[:3]:
            issue_id = item.get("issue_id")
            subject = str(item.get("subject") or "-")
            if len(subject) > 42:
                subject = subject[:42] + "..."
            time_text = (item.get("last_external_reply_at") or item.get("updated_on") or "")[:10]
            parts.append(f"#{issue_id} {subject}" + (f" ({time_text})" if time_text else ""))
        return f"- {label}: " + "；".join(parts)

    # ==================== Router Function Caller ====================

    async def _call_router_function(
        self, tool: AgentTool, session: Any, request: Any, params: dict[str, Any]
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
            call_kwargs = self._build_call_kwargs(func, tool, request, params)
            if asyncio.iscoroutinefunction(func):
                response = await func(**call_kwargs)
            else:
                response = func(**call_kwargs)

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

    def _build_call_kwargs(self, func: Any, tool: AgentTool, request: Any, params: dict[str, Any]) -> dict[str, Any]:
        from routers.agent import AgentRequestShim

        model_by_tool = _get_model_by_tool()

        query_params = self._query_params_for_tool(tool, params)
        shim = AgentRequestShim(request, query_params=query_params) if request else None
        sig = _cached_signature(func)
        kwargs: dict[str, Any] = {}

        for name in sig.parameters:
            if name == "request":
                kwargs[name] = shim
            elif name == "help":
                kwargs[name] = False
            elif name == "h":
                kwargs[name] = None
            elif name in ("req", "body"):
                model = model_by_tool.get(tool.name)
                if model:
                    kwargs[name] = model(**self._body_params_for_tool(tool, params))
                else:
                    kwargs[name] = self._body_params_for_tool(tool, params)
            elif name in params:
                kwargs[name] = params[name]
            elif name in query_params:
                kwargs[name] = query_params[name]

        return kwargs

    @staticmethod
    def _body_params_for_tool(tool: AgentTool, params: dict[str, Any]) -> dict[str, Any]:
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

    @staticmethod
    def _query_params_for_tool(tool: AgentTool, params: dict[str, Any]) -> dict[str, Any]:
        query = dict(params or {})
        if tool.name == "reports_delete" and "timestamp" not in query:
            query["timestamp"] = query.get("report_timestamp", "")
        if tool.name == "reports_download" and "report_timestamp" not in query:
            query["report_timestamp"] = query.get("timestamp", "")
        return {k: v for k, v in query.items() if v is not None}


# ==================== Helpers ====================

def _json_body(response) -> dict[str, Any]:
    """Extract JSON body from a FastAPI JSONResponse or similar object."""
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump()
    if response is None:
        return {"success": False, "error": "Empty response"}
    try:
        body = getattr(response, "body", None)
        if body is None and hasattr(response, "render"):
            body = response.render(getattr(response, "content", None))
        if isinstance(body, memoryview):
            body = body.tobytes()
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        if isinstance(body, str) and body.strip():
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {
                    "success": False,
                    "error": body.strip(),
                    "message": body.strip(),
                }
    except Exception as e:
        logger.warning("[Agent] Failed to parse JSON response from %s: %s", type(response).__name__, e)
    return {"success": False, "error": f"Invalid JSON response: {type(response).__name__}"}


def _format_payload(tool: AgentTool, payload: dict[str, Any]) -> str:
    if payload.get("error"):
        return str(payload["error"])
    if tool.name == "devices_info":
        return _format_device_info_payload(payload)
    if payload.get("message"):
        return str(payload["message"])
    if "connected" in payload:
        return f"{tool.display_name}：{'已连接/正常' if payload['connected'] else '未连接'}"
    data = payload.get("data", payload)
    if isinstance(data, dict):
        # Helper to get a list from data or payload
        def _first_list(*keys):
            for container in (data, payload):
                for k in keys:
                    v = container.get(k)
                    if isinstance(v, list):
                        return v
            return []

        results = _first_list("results")
        if results:
            ok = sum(1 for item in results if isinstance(item, dict) and item.get("success"))
            return f"{tool.display_name}完成：成功 {ok}/{len(results)}"
        reports = _first_list("reports")
        if reports:
            return f"查询到 {len(reports)} 份报告。"
        devices = _first_list("devices")
        if devices:
            return f"查询到 {len(devices)} 台设备。"
    return f"{tool.display_name}已完成。"


def _format_device_info_payload(payload: dict[str, Any]) -> str:
    data = payload.get("data") or payload
    results = data.get("results") if isinstance(data, dict) else None
    if not results and isinstance(data, dict):
        props = data.get("properties") or data.get("info")
        if isinstance(props, dict):
            results = [{
                "device": data.get("device") or data.get("device_id") or payload.get("device_id"),
                "properties": props,
            }]
    if not isinstance(results, list) or not results:
        return payload.get("message") or "未返回设备详情。"

    preferred_fields = (
        "Model",
        "Android Version",
        "API Level",
        "SDK Version",
        "Serial Number",
        "Boot State",
        "Security Patch",
        "Build Type",
        "Build Tags",
        "Build Date",
        "Mali Version",
        "Total Memory",
        "Free Memory",
        "DATA Partition",
        "Timezone",
        "Language",
        "Fingerprint",
    )

    sections = []
    for item in results:
        if not isinstance(item, dict):
            continue
        device_id = item.get("device") or item.get("device_id") or item.get("serial_no") or "Unknown"
        props = item.get("properties") or {}
        lines = [f"设备 {device_id} 详情："]
        for field_name in preferred_fields:
            value = props.get(field_name)
            if value not in (None, ""):
                lines.append(f"- {field_name}: {value}")
        extra_fields = [
            (key, value)
            for key, value in props.items()
            if key not in preferred_fields and value not in (None, "")
        ]
        for key, value in extra_fields[:8]:
            lines.append(f"- {key}: {value}")
        sections.append("\n".join(lines))

    return "\n\n".join(sections) if sections else "未返回设备详情。"


# ==================== Global Instance ====================

executor = ActionExecutor()
