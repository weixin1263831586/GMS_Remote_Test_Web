"""Agent tool results, response formatting and navigation actions."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from foundation.error_model import ApiError


if TYPE_CHECKING:
    from .tools import AgentTool


logger = logging.getLogger(__name__)


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


def http_error_result(tool_name: str, exc: HTTPException, action_label: str = "") -> ToolResult:
    """Preserve actionable guards without stringifying Python dicts into display text."""
    detail = exc.detail
    message = detail.get("message") or detail.get("error") if isinstance(detail, dict) else detail
    message = message if isinstance(message, str) and message else f"请求被拒绝（HTTP {exc.status_code}）"
    data = {"status_code": exc.status_code}
    if exc.status_code == 403:
        data["code"] = ApiError.forbidden().code
        if isinstance(detail, dict) and detail.get("elevation_required") is True:
            data["elevation_required"] = True
            message = "需要管理员临时授权"
    elif exc.status_code == 401:
        data["code"] = ApiError.unauthenticated().code
    return ToolResult(success=False, tool_name=tool_name, data=data, error=message,
                      formatted_text=f"{action_label}被拒绝：{message}" if action_label else message)


def internal_error_result(tool_name: str, action_label: str) -> ToolResult:
    """未知异常的统一 Agent 出口。

    ``str(e)`` 可能内嵌路径/凭据片段，不外显给 Agent——完整 traceback
    只进服务端日志；Agent 拿到统一错误 + request id，用户可凭
    request_id 回查日志定位。``action_label`` 是失败动作的动词短语
    （执行/调用），用于 formatted_text。
    """
    request_id = uuid.uuid4().hex[:12]
    return ToolResult(
        success=False,
        tool_name=tool_name,
        error=f"服务内部错误（request_id={request_id}）",
        formatted_text=(
            f"{action_label}失败：服务内部错误（request_id={request_id}，已记录日志）"
        ),
    )


def json_body(response) -> dict[str, Any]:
    """Extract JSON from a FastAPI response or model-like value."""
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
    except Exception as exc:
        logger.warning(
            "[Agent] Failed to parse JSON response from %s: %s",
            type(response).__name__,
            exc,
        )
    return {
        "success": False,
        "error": f"Invalid JSON response: {type(response).__name__}",
    }


def format_payload(tool: AgentTool, payload: dict[str, Any]) -> str:
    if payload.get("error"):
        return str(payload["error"])
    if tool.name == "devices_info":
        return format_device_info_payload(payload)
    if payload.get("message"):
        return str(payload["message"])
    if "connected" in payload:
        state = "已连接/正常" if payload["connected"] else "未连接"
        return f"{tool.display_name}：{state}"
    data = payload.get("data", payload)
    if isinstance(data, dict):
        def first_list(*keys: str) -> list:
            for container in (data, payload):
                for key in keys:
                    value = container.get(key)
                    if isinstance(value, list):
                        return value
            return []

        results = first_list("results")
        if results:
            succeeded = sum(
                1
                for item in results
                if isinstance(item, dict) and item.get("success")
            )
            return f"{tool.display_name}完成：成功 {succeeded}/{len(results)}"
        reports = first_list("reports")
        if reports:
            return f"查询到 {len(reports)} 份报告。"
        devices = first_list("devices")
        if devices:
            return f"查询到 {len(devices)} 台设备。"
        if tool.name in {"sdk_sources", "sdk_sources_list"}:
            return _format_sdk_sources(data)
        items = first_list("items", "jobs", "workers", "docs")
        if items:
            return f"{tool.display_name}：查询到 {len(items)} 条记录。"
    return f"{tool.display_name}已完成。"


def _format_sdk_sources(data: dict[str, Any]) -> str:
    """只展示 source_id、类型、默认版本和 SSH 目标，不带路径或凭据。"""
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        return "尚未登记 SDK 源。"
    labels = {
        "local_git": "本机 Git 仓库",
        "ssh_git": "编译服务器",
        "codesearch": "OpenGrok",
    }
    lines = [f"已登记 {len(sources)} 个 SDK 源："]
    for item in sources:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_id") or "未命名")
        provider = labels.get(str(item.get("provider") or ""), str(item.get("provider") or "未知类型"))
        revision = str(item.get("default_revision") or "").strip() or "未设置默认版本"
        host = str(item.get("ssh_host") or "").strip()
        location = ""
        if host:
            user = str(item.get("ssh_username") or "").strip()
            port = item.get("ssh_port") or 22
            target = f"{user}@{host}:{port}" if user else f"{host}:{port}"
            location = f"，{target}"
        lines.append(f"- {source_id}（{provider}，默认版本 {revision}{location}）")
    return "\n".join(lines)


def format_device_info_payload(payload: dict[str, Any]) -> str:
    data = payload.get("data") or payload
    results = data.get("results") if isinstance(data, dict) else None
    if not results and isinstance(data, dict):
        properties = data.get("properties") or data.get("info")
        if isinstance(properties, dict):
            results = [
                {
                    "device": data.get("device")
                    or data.get("device_id")
                    or payload.get("device_id"),
                    "properties": properties,
                }
            ]
    if not isinstance(results, list) or not results:
        return payload.get("message") or "未返回设备详情。"
    preferred_fields = (
        "Model", "Android Version", "API Level", "SDK Version",
        "Serial Number", "Boot State", "Security Patch", "Build Type",
        "Build Tags", "Build Date", "Mali Version", "Total Memory",
        "Free Memory", "DATA Partition", "Timezone", "Language", "Fingerprint",
    )
    sections = []
    for item in results:
        if not isinstance(item, dict):
            continue
        device_id = (
            item.get("device")
            or item.get("device_id")
            or item.get("serial_no")
            or "Unknown"
        )
        properties = item.get("properties") or {}
        lines = [f"设备 {device_id} 详情："]
        for field_name in preferred_fields:
            value = properties.get(field_name)
            if value not in (None, ""):
                lines.append(f"- {field_name}: {value}")
        extra_fields = [
            (key, value)
            for key, value in properties.items()
            if key not in preferred_fields and value not in (None, "")
        ]
        lines.extend(f"- {key}: {value}" for key, value in extra_fields[:8])
        sections.append("\n".join(lines))
    return "\n\n".join(sections) if sections else "未返回设备详情。"


# 单一页面注册表：page → (显示名, 功能描述)。
# Agent 提示（api.py 能力说明）、快捷导航（page_quick_actions）和页面
# 功能说明都从这里派生，新增/重命名页面只改这一处。
WEBAPP_PAGES: dict[str, tuple[str, str]] = {
    "test": ("测试界面", "运行测试、查看测试日志"),
    "desktop": ("主机桌面", "查看和控制主机桌面"),
    "terminal": ("主机终端", "打开主机终端和上传文件"),
    "users": ("用户管理", "查看在线用户和测试占用"),
    "devices": ("设备管理", "查看 ADB 设备、锁定状态和来源"),
    "devices-console": ("设备串口", "查看 Controller 本机串口控制台和早期启动日志"),
    "reports": ("报告管理", "查看、下载、删除测试报告"),
    "report-analysis": ("报告分析", "上传或打开报告并诊断失败"),
    "apk-analysis": ("APK分析", "上传、反编译和查看 APK/JAR 源码"),
    "test-suites": ("测试套件", "浏览、下载、解压和反编译套件文件"),
    "api-docs": ("系统接口", "查看 API 和 curl 示例"),
    "architecture": ("系统架构", "查看系统架构图"),
    "websites": ("常用网址", "管理常用站点"),
    "tools": ("常用工具", "下载和维护常用工具"),
    "security-audit": ("安全审计", "查看访问和接口审计"),
    "gms-assistant": ("GMS助手", "外部 GMS 助手"),
    "automation": ("GMS ATS", "查看和管理自动化测试流水线"),
    "cluster": ("主机集群", "查看 Worker、设备和持久化集群任务"),
    "redmine-agent": ("Redmine看板", "个人/部门 Redmine 统计、未回复问题和 RedmineAgent 扫描"),
    "gerrit-dashboard": ("Gerrit看板", "查询 Gerrit 变更和配置 Gerrit dashboard profiles"),
    "notes": ("个人知识库", "管理 Wiki 文档、附件和知识问答"),
    "agent": ("对话Agent", "自然语言操作 Web_app"),
}

PAGE_DISPLAY_NAMES: dict[str, str] = {
    page: name for page, (name, _description) in WEBAPP_PAGES.items()
}


def page_quick_actions() -> list[dict[str, str]]:
    """Return one navigation quick action per first-class sidebar page."""
    return [{"label": label, "page": page} for page, label in PAGE_DISPLAY_NAMES.items()]


@dataclass
class AgentResponse:
    """Agent 回复消息。"""
    content: str                       # 纯文本回退
    kind: str                          # text / table / status / plan / action_menu / code / error
    data: dict[str, Any]               # 结构化数据
    quick_actions: list[dict[str, Any]]  # 快捷操作按钮
    page: str                          # 关联页面

    def to_message_data(self) -> dict[str, Any]:
        """转为 _append_message 的 data 参数。"""
        result = dict(self.data or {})
        if self.quick_actions:
            result["quick_actions"] = self.quick_actions
        if self.page:
            result["page"] = self.page
        return result


def generate(result: ToolResult) -> AgentResponse:
    """从 ToolResult 生成 AgentResponse。"""
    kind = result.kind or "text"
    quick_actions = [a for a in result.quick_actions if a] if result.quick_actions else []

    # 根据 kind 增强 quick_actions
    if not quick_actions and result.page:
        quick_actions = [{"label": f"打开{_page_display_name(result.page)}", "page": result.page}]

    return AgentResponse(
        content=result.formatted_text or (result.error if not result.success else "操作完成"),
        kind=kind,
        data=result.data or {},
        quick_actions=quick_actions,
        page=result.page,
    )


def generate_clarification(suggestions: list[dict[str, str]]) -> AgentResponse:
    """生成澄清响应（当意图置信度低时）。"""
    lines = ["我不太确定您想要做什么。您是想要："]
    for i, s in enumerate(suggestions[:4], 1):
        lines.append(f"{i}. {s.get('label', s.get('description', ''))}")
    return AgentResponse(
        content="\n".join(lines),
        kind="action_menu",
        data={"suggestions": suggestions},
        quick_actions=[
            {"label": s.get("label", ""), "action": s.get("action", ""), "params": s.get("params", {})}
            for s in suggestions[:4]
        ],
        page="",
    )


def generate_capability_overview() -> str:
    """生成 Agent 能力概览文本。"""
    return (
        "我是 GMS 远程测试平台的对话 Agent，负责把自然语言转换成 Web_app 查询、导航和执行计划。\n\n"
        "我能做这些事：\n"
        "- 查询状态：设备/型号、空闲占用、测试状态、测试套件、报告、ATS 流水线、Cluster Worker/任务、构建任务、知识库、在线用户、系统健康、VPN、USB/IP、配置摘要。\n"
        "- 执行操作：启动/停止测试、失败 retry、取消/重试 ATS 或集群任务、连接 WiFi、重启/remount/投屏设备、VPN/USB-IP 操作、知识文档创建、报告下载/删除等；有风险或会改状态的操作会先让你确认。\n"
        "- 分析问题：报告分析、失败用例诊断、APK/JAR 反编译、套件 APK 源码分析、OpenGrok 代码搜索。\n"
        "- 页面导航：打开测试界面、设备管理、设备串口、报告管理、报告分析、APK分析、测试套件、GMS ATS、主机集群、Redmine看板、Gerrit看板、个人知识库、终端、桌面、常用网址、常用工具、安全审计。\n\n"
        "你可以直接这样说：\n"
        "- 「rk3572设备」或「空闲设备」\n"
        "- 「最近报告」或「分析最新失败报告」\n"
        "- 「跑 CtsWifiTestCases，失败 retry 2 次」\n"
        "- 「打开 APK 分析」或「VPN 状态」"
    )


def generate_page_overview() -> str:
    """生成 Web_app 页面功能说明。"""
    return (
        "Web_app 主要页面功能如下：\n\n"
        "- 测试界面：选择 CTS/GTS/VTS/STS 等套件，指定模块/用例和设备，启动/停止测试，查看实时日志和执行状态。\n"
        "- 主机桌面：启动或查看 noVNC/x11vnc 桌面，用于远程 GUI 操作、调试工具和桌面环境确认。\n"
        "- 主机终端：打开服务器 SSH 终端，执行命令、上传文件、辅助定位环境问题。\n"
        "- 用户管理：查看在线用户、客户端 IP、用户名、测试运行状态和设备占用情况。\n"
        "- 设备管理：查看 ADB 设备、型号、Android 版本、电量、来源、锁定状态；支持重启、remount、WiFi、投屏、bootloader 操作。\n"
        "- 设备串口：查看 Controller 本机 USB 转串口，使用实时控制台并管理开机早期日志。\n"
        "- 报告管理：列出历史测试报告，按用户/时间查看，下载、删除、进入分析。\n"
        "- 报告分析：上传报告或打开已有报告，解析失败项，做失败诊断、AI 分析和根因线索整理。\n"
        "- APK分析：上传 APK/JAR，反编译源码，查看 Manifest、权限、源码树和文件内容。\n"
        "- 测试套件：浏览本地/远端套件目录，查看 tradefed 结果，下载/解压套件，触发套件内 APK/JAR 分析。\n"
        "- 系统接口：查看 API 清单、参数、curl 示例和响应格式，适合调试接口或脚本调用。\n"
        "- 系统架构：查看平台模块、数据流和核心组件关系。\n"
        "- 常用网址：按分类维护常用站点、图标和链接。\n"
        "- 常用工具：维护可下载工具条目，从服务器白名单工具目录下载脚本或二进制工具。\n"
        "- 安全审计：查看页面访问、API 调用、请求摘要、响应摘要和耗时，辅助追踪操作记录。\n"
        "- GMS助手：打开外部/内置 GMS 知识助手入口。\n"
        "- GMS ATS：把 Gerrit 触发、构建产物、设备选择、烧写、测试执行和结果回写串成自动化测试站流程。\n"
        "- 主机集群：查看 Controller/Worker、设备、套件、运行任务和部署状态，管理持久化 Cluster Job。\n"
        "- Redmine看板：查看个人/部门/项目 Redmine 统计、待回复、超阈值未回复、解决趋势和问题明细。\n"
        "- Gerrit看板：查看个人/部门 Gerrit 提交统计、查询变更、趋势明细和成员配置。\n"
        "- 个人知识库：按知识空间和目录维护 Wiki，上传附件、全文检索、关联报告/Redmine/Gerrit 并进行知识问答。\n"
        "- 对话Agent：用自然语言查询设备/报告/套件，生成测试计划，打开项目页面，执行确认类操作并跟踪分析流程。"
    )


# ==================== Helpers ====================

def _page_display_name(page: str) -> str:
    """页面标识 → 中文显示名。"""
    return PAGE_DISPLAY_NAMES.get(page, page)
