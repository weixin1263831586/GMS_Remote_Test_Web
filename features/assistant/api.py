"""Conversation Agent router for guided GMS Remote Test workflows."""

import asyncio
import logging
import re
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, Field

from features.assistant.cluster_runtime import ACTIVE_CLUSTER_JOB_STATUSES as _ACTIVE_CLUSTER_JOB_STATUSES
from features.assistant.context import _parse_chinese_number, record_user_message, update_context
from features.assistant.executor import _json_body, executor
from features.assistant.intent import (
    _extract_device_ids,
    _extract_module_and_case,
    _extract_retry_count,
    _extract_test_type,
    _is_run_test_request,
)
from features.assistant.response import (
    generate as gen_response,
)
from features.assistant.response import (
    generate_capability_overview,
    generate_clarification,
    generate_page_overview,
    page_quick_actions,
)
from features.assistant.tools import registry
from features.devices import device_lock_manager, device_manager, get_or_create_user_state
from features.reports import ReportDiagnosisRequest, test_report_db
from features.test_execution import (
    SuiteApkAnalyzeRequest,
    detect_test_type_from_suite_path,
    get_default_suites_path,
)
from features.users import get_client_id_from_request
from foundation.config import config_manager
from foundation.responses import error_response, success_response


logger = logging.getLogger(__name__)

router = APIRouter()

AGENT_SESSION_TTL_SECONDS = 6 * 60 * 60
MAX_AGENT_RETRIES = 3
WEBAPP_PAGES = {
    "test": ("测试界面", "运行测试、查看测试日志"),
    "desktop": ("主机桌面", "查看和控制主机桌面"),
    "terminal": ("主机终端", "打开主机终端和上传文件"),
    "users": ("用户管理", "查看在线用户和测试占用"),
    "devices": ("设备管理", "查看 ADB 设备、锁定状态和来源"),
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

_agent_sessions: dict[str, dict[str, Any]] = {}
_agent_sessions_lock = asyncio.Lock()
_agent_monitor_tasks: dict[str, asyncio.Task] = {}


class AgentChatRequest(BaseModel):
    """Request body for Agent chat messages."""

    message: str = Field(default="", max_length=4000)
    session_id: str | None = None
    execute: bool = False
    action: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    workspace_context: dict[str, Any] = Field(default_factory=dict)


_AGENT_WORKSPACE_FIELDS = {
    "scope_mode", "worker_id", "device_ids", "suite_key", "suite_path",
    "cluster_job_id", "attempt_id", "automation_run_id", "report_id",
    "report_timestamp", "artifact_id", "gerrit_change_id", "gerrit_patchset",
    "redmine_issue_id", "origin_page",
}

def _local_worker_id() -> str:
    try:
        from features.cluster import get_cluster_service

        return str(get_cluster_service().config.local_worker_id or "worker-local")
    except (AttributeError, RuntimeError):
        return "worker-local"


def _is_local_worker_id(worker_id: str | None) -> bool:
    return not worker_id or worker_id in {"worker-local", _local_worker_id()}


def _normalize_workspace_context(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only bounded navigation state supplied by the authenticated browser."""
    if not raw:
        return {"scope_mode": "single", "worker_id": _local_worker_id(), "device_ids": []}
    context: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        if key not in _AGENT_WORKSPACE_FIELDS:
            continue
        if key == "device_ids":
            context[key] = [str(item)[:384] for item in (value or [])[:32] if str(item).strip()]
        elif value is not None:
            context[key] = str(value)[:4096]
    if "scope_mode" in raw:
        context["scope_mode"] = "cluster" if context.get("scope_mode") == "cluster" else "single"
    elif context.get("worker_id") and not _is_local_worker_id(context["worker_id"]):
        context["scope_mode"] = "cluster"
    if context.get("scope_mode") == "single":
        context["worker_id"] = _local_worker_id()
    elif context.get("scope_mode") == "cluster":
        context["worker_id"] = context.get("worker_id") or _local_worker_id()
        if context["worker_id"] == "worker-local":
            context["worker_id"] = _local_worker_id()
    return context


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


def _new_message(role: str, content: str, kind: str = "text", data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "role": role,
        "kind": kind,
        "content": content,
        "data": data or {},
        "created_at": _now_iso(),
    }


def _jsonable(value: Any) -> Any:
    """Return a JSON-serializable copy of Agent session data."""
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump())
    return str(value)


def _session_payload(session: dict[str, Any]) -> dict[str, Any]:
    return _jsonable(session)


def _expired_session_payload(session_id: str) -> dict[str, Any]:
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


async def _get_or_create_session(session_id: str | None, client_id: str) -> dict[str, Any]:
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


async def _cleanup_sessions() -> None:
    cutoff = time.time() - AGENT_SESSION_TTL_SECONDS
    async with _agent_sessions_lock:
        stale_ids = []
        for sid, session in _agent_sessions.items():
            try:
                updated = datetime.fromisoformat(session.get("updated_at", "")).timestamp()
            except Exception:
                updated = 0
            if updated < cutoff and session.get("status") not in {"running", "monitoring"}:
                stale_ids.append(sid)
        for sid in stale_ids:
            _agent_sessions.pop(sid, None)


def _append_step(session: dict[str, Any], title: str, status: str = "done", detail: str = "", data: dict[str, Any] | None = None) -> None:
    session.setdefault("steps", []).append({
        "id": str(uuid.uuid4()),
        "title": title,
        "status": status,
        "detail": detail,
        "data": data or {},
        "created_at": _now_iso(),
    })
    session["updated_at"] = _now_iso()


def _append_message(session: dict[str, Any], role: str, content: str, kind: str = "text", data: dict[str, Any] | None = None) -> dict[str, Any]:
    msg = _new_message(role, content, kind=kind, data=data)
    session.setdefault("messages", []).append(msg)
    session["updated_at"] = _now_iso()
    return msg


def _extract_device_count(text: str) -> int:
    if re.search(r"全部|所有|all", text, re.IGNORECASE):
        return 0
    match = re.search(r"(?<![A-Za-z0-9])(\d+)\s*(?:台|个)?\s*(?:设备|device)", text, re.IGNORECASE)
    if match:
        return max(1, min(8, int(match.group(1))))
    if re.search(r"(一|两|二|三|四|五)\s*台", text):
        return _parse_chinese_number(text)
    return 1


def _parse_user_intent(message: str) -> dict[str, Any]:
    text = message.strip()
    lowered = text.lower()
    module, case = _extract_module_and_case(text)
    test_type = _extract_test_type(text)
    retry_count = _extract_retry_count(text)
    module_case_tokens = set(
        re.findall(r"[A-Za-z0-9_.:-]{2,}", f"{module} {case}".replace("/", " "))
    )
    explicit_devices = [
        device_id for device_id in _extract_device_ids(text)
        if device_id not in module_case_tokens
    ]

    return {
        "raw": text,
        "intent": "run_test" if _is_run_test_request(text) else "chat",
        "test_type": test_type,
        "test_module": module,
        "test_case": case,
        "devices": explicit_devices,
        "device_count": len(explicit_devices) or _extract_device_count(text),
        "retry_count": retry_count,
        "analyze_on_failure": bool(re.search(r"报告分析|分析报告|报错分析|失败|fail|analy", lowered)),
        "apk_source_analysis": bool(re.search(r"apk|反编译|源码|source", lowered)),
        "connect_wifi": bool(re.search(r"wifi|wi-fi|无线网络|连接网络", lowered)),
    }


def _select_devices(
    intent: dict[str, Any], workspace_context: dict[str, Any] | None = None
) -> tuple[list[str], list[dict[str, Any]]]:
    workspace = workspace_context or {}
    worker_id = workspace.get("worker_id") or _local_worker_id()
    is_remote = workspace.get("scope_mode") == "cluster" and not _is_local_worker_id(worker_id)
    workspace_devices = [str(item) for item in workspace.get("device_ids") or []]
    if is_remote:
        from features.cluster import get_cluster_service

        rows = get_cluster_service().repository.list_devices(worker_id)
        details = [{
            "device_id": item.get("id") or f"{worker_id}:{item.get('serial', '')}",
            "serial": item.get("serial", ""),
            "worker_id": worker_id,
            "state": item.get("state", "unknown"),
            "locked": item.get("state") != "available",
            "locked_by": item.get("lease_owner", ""),
        } for item in rows]
        available = {item["device_id"]: item for item in details if not item["locked"]}

        def remote_id(value: str) -> str:
            return value if value.startswith(f"{worker_id}:") else f"{worker_id}:{value}"

        explicit = [remote_id(item) for item in intent.get("devices") or []]
        preferred = [remote_id(item) for item in workspace_devices]
        requested = explicit or preferred
        if requested:
            return [item for item in requested if item in available], details
        unlocked = list(available)
        count = int(intent.get("device_count") or 1)
        return unlocked if count <= 0 else unlocked[:count], details

    device_ids = device_manager.get_connected_devices(force_refresh=True)
    details = []
    for device_id in device_ids:
        lock_status = device_lock_manager.get_lock_status(device_id)
        details.append({
            "device_id": device_id,
            "locked": bool(lock_status),
            "locked_by": lock_status.get("locked_by", "") if lock_status else "",
        })

    requested = intent.get("devices") or [
        item.split(":", 1)[1]
        if item.startswith(("worker-local:", f"{_local_worker_id()}:")) else item
        for item in workspace_devices
    ]
    if requested:
        available = {item["device_id"]: item for item in details}
        return [dev for dev in requested if dev in available and not available[dev]["locked"]], details

    unlocked = [item["device_id"] for item in details if not item["locked"]]
    count = int(intent.get("device_count") or 1)
    return unlocked if count <= 0 else unlocked[:count], details


def _score_suite(suite: dict[str, Any], test_type: str, module: str) -> int:
    score = 0
    haystack = " ".join(str(suite.get(key, "")) for key in (
        "test_type", "suite_type", "version", "suite_version", "tools_path", "binary"
    )).lower()
    if test_type and test_type.lower() in haystack:
        score += 20
    if module:
        module_prefix = re.sub(r"TestCases?$", "", module, flags=re.IGNORECASE).lower()
        if module_prefix and module_prefix in haystack:
            score += 5
    if suite.get("tools_path"):
        score += 1
    return score


def _list_suites(workspace_context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    workspace = workspace_context or {}
    worker_id = workspace.get("worker_id") or _local_worker_id()
    if workspace.get("scope_mode") == "cluster" and not _is_local_worker_id(worker_id):
        from features.cluster import get_cluster_service

        return [
            item for item in get_cluster_service().repository.list_suites(worker_id)
            if item.get("available")
        ]
    from features.test_execution import _get_available_test_suites

    config = config_manager.load_config()
    base_path = config.get("suites_path") or get_default_suites_path(config)
    return _get_available_test_suites(config, base_path)


def _select_suite(
    intent: dict[str, Any], workspace_context: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    workspace = workspace_context or {}
    suites = _list_suites(workspace)
    test_type = intent.get("test_type") or ""
    module = intent.get("test_module") or ""
    if not suites:
        return None
    selected_path = workspace.get("suite_path") or ""
    selected_key = workspace.get("suite_key") or ""
    exact = next((suite for suite in suites if selected_path and
                  (suite.get("tools_path") or suite.get("full_path")) == selected_path), None)
    if not exact:
        exact = next((suite for suite in suites if selected_key and
                      suite.get("suite_key") == selected_key), None)
    if exact:
        return exact
    return max(suites, key=lambda suite: _score_suite(suite, test_type, module))


def _build_plan(
    intent: dict[str, Any], selected_devices: list[str],
    device_details: list[dict[str, Any]], suite: dict[str, Any] | None,
    workspace_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workspace = workspace_context or {}
    test_suite = (suite.get("tools_path") or suite.get("full_path") or "") if suite else ""
    test_type = intent.get("test_type") or (
        suite.get("test_type") or suite.get("suite_type") or "" if suite else ""
    )
    if not test_type and test_suite:
        test_type = detect_test_type_from_suite_path(test_suite).upper()

    steps = [
        {"title": "刷新设备", "detail": f"发现 {len(device_details)} 台设备，选择 {len(selected_devices)} 台可用设备"},
        {"title": "匹配测试套件", "detail": test_suite or "未找到可用测试套件"},
    ]
    pre_actions = []
    if intent.get("connect_wifi"):
        _wifi = config_manager.get_wifi_defaults()
        pre_actions.append({
            "type": "connect_wifi",
            "ssid": intent.get("wifi_ssid") or _wifi["ssid"],
            "password": intent.get("wifi_password") or _wifi["password"],
        })
        steps.append({"title": "连接 WiFi", "detail": f"测试前连接到 {_wifi['ssid']}"})
    steps.append({"title": "启动测试", "detail": intent.get("test_module") or intent.get("test_case") or "按测试套件执行"})
    retry_count = int(intent.get("retry_count") or 0)
    if retry_count:
        steps.append({"title": "失败后自动 retry", "detail": f"最多 retry {retry_count} 次"})
    if intent.get("analyze_on_failure"):
        steps.append({"title": "失败后报告分析", "detail": "测试仍失败时定位最近报告并给出分析入口"})
    if intent.get("apk_source_analysis"):
        steps.append({"title": "APK/源码分析", "detail": "根据失败信息定位套件 APK/JAR 并建议反编译入口"})

    return {
        "intent": intent,
        "steps": steps,
        "request": {
            "worker_id": workspace.get("worker_id") or _local_worker_id(),
            "test_type": test_type,
            "test_module": intent.get("test_module", ""),
            "test_case": intent.get("test_case", ""),
            "retry_dir": "",
            "test_suite": test_suite,
            "local_server": config_manager.load_config().get("local_server", ""),
            "devices": selected_devices,
        },
        "pre_actions": pre_actions,
        "policy": {
            "retry_count": retry_count,
            "analyze_on_failure": bool(intent.get("analyze_on_failure")),
            "apk_source_analysis": bool(intent.get("apk_source_analysis")),
        },
        "device_details": device_details,
        "suite": suite or {},
        "workspace_context": workspace,
    }


def _summarize_plan(plan: dict[str, Any]) -> str:
    req = plan.get("request", {})
    policy = plan.get("policy", {})
    lines = [
        "我已经生成执行计划，确认后开始：",
        f"- Worker: {req.get('worker_id') or _local_worker_id()}",
        f"- 设备: {', '.join(req.get('devices') or []) or '未选择'}",
        f"- 测试前连接 WiFi: {'是' if plan.get('pre_actions') else '否'}",
        f"- 测试类型: {req.get('test_type') or '自动识别'}",
        f"- 测试模块: {req.get('test_module') or '未指定'}",
        f"- 测试用例: {req.get('test_case') or '未指定'}",
        f"- 测试套件: {req.get('test_suite') or '未找到'}",
        f"- 失败 retry: {policy.get('retry_count', 0)} 次",
        f"- 失败后报告分析: {'是' if policy.get('analyze_on_failure') else '否'}",
    ]
    if not req.get("devices"):
        lines.append("当前没有可用未占用设备，不能执行。")
    if not req.get("test_suite") and not req.get("retry_dir"):
        lines.append("当前没有匹配到测试套件，不能执行。")
    lines.append("输入“确认执行”或点击执行按钮后启动。")
    return "\n".join(lines)


def _apply_workspace_tool_params(
    tool_name: str, params: dict[str, Any], workspace: dict[str, Any]
) -> dict[str, Any]:
    """Fill omitted tool parameters from the visible browser workspace."""
    result = dict(params or {})
    worker_id = workspace.get("worker_id") or _local_worker_id()
    if tool_name == "reports_list" and worker_id:
        result.setdefault("worker_id", worker_id)
    if tool_name == "test_start":
        result.setdefault("worker_id", worker_id)
        result.setdefault("devices", workspace.get("device_ids") or [])
        result.setdefault("test_suite", workspace.get("suite_path") or "")
    if tool_name in {"reports_analyze", "reports_download", "reports_delete"}:
        timestamp = workspace.get("report_timestamp") or workspace.get("report_id") or ""
        if timestamp:
            result.setdefault("report_timestamp", timestamp)
            result.setdefault("timestamp", timestamp)
    return result


def _missing_required_params(tool: Any, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Return required tool parameters that still have no usable value."""
    if not tool:
        return []
    missing: list[dict[str, Any]] = []
    for definition in tool.params:
        if not definition.get("required"):
            continue
        name = str(definition.get("name") or "").strip()
        if not name:
            continue
        value = params.get(name)
        if value is None or (isinstance(value, str) and not value.strip()) or value == []:
            missing.append(definition)
    return missing


def _request_missing_params(session: dict[str, Any], missing: list[dict[str, Any]]) -> None:
    labels = [str(item.get("desc") or item.get("name") or "参数") for item in missing]
    _append_message(
        session,
        "assistant",
        "还缺少必要参数：" + "、".join(labels) + "。请补充后再执行。",
    )
    session["status"] = "idle"
    session["pending_plan"] = None


def _latest_report_for_client(client_id: str, exclude_timestamp: str | None = None) -> dict[str, Any] | None:
    reports = test_report_db.get_reports(limit=20, user_only=client_id)
    if exclude_timestamp:
        for report in reports:
            if report.get("timestamp") == exclude_timestamp:
                break
            return report
        return None
    return reports[0] if reports else None


def _report_failed(report: dict[str, Any] | None) -> bool:
    if not report:
        return False
    fail_count = report.get("fail", report.get("fail_count", 0)) or 0
    try:
        return int(fail_count) > 0
    except Exception:
        return False


def _report_for_cluster_job(job_id: str, attempt_id: str = "") -> dict[str, Any] | None:
    """Resolve only the report produced by one durable Worker attempt."""
    if not job_id:
        return None
    report = test_report_db.get_report_by_timestamp(f"cluster-{job_id}")
    if not report or report.get("cluster_job_id") != job_id:
        return None
    if attempt_id and report.get("attempt_id") != attempt_id:
        return None
    return report


def _normalize_failure(raw_failure: dict[str, Any], index: int = 0) -> dict[str, Any]:
    """Return one failure in the shape expected by diagnosis APIs."""
    raw_failure = raw_failure or {}
    test_name = raw_failure.get("name") or raw_failure.get("test_name") or raw_failure.get("test") or ""
    reason = raw_failure.get("reason") or raw_failure.get("error_message") or raw_failure.get("message") or ""
    stack_trace = raw_failure.get("stack_trace") or raw_failure.get("trace") or reason
    return {
        "index": index,
        "test_name": str(test_name or ""),
        "module": str(raw_failure.get("module") or raw_failure.get("test_module") or ""),
        "error_message": str(reason or ""),
        "stack_trace": str(stack_trace or ""),
    }


async def _analyze_saved_report(session: dict[str, Any], report_timestamp: str) -> dict[str, Any] | None:
    """Analyze a saved report and append an Agent step."""
    from features.reports import test_report_manager

    _append_step(session, "报告分析", "running", f"正在分析报告 {report_timestamp}")
    try:
        analysis = await asyncio.to_thread(test_report_manager.analyze_report, report_timestamp)
        if not analysis:
            _append_step(session, "报告分析", "warning", "报告存在，但未能解析 test_result.xml")
            return None

        summary = analysis.get("summary") or {}
        analysis.get("failures") or []
        detail = f"总计 {summary.get('total', 0)}，通过 {summary.get('pass', 0)}，失败 {summary.get('fail', summary.get('failed', 0))}"
        _append_step(session, "报告分析", "done", detail, {"report_analysis": analysis})
        return analysis
    except Exception as e:
        logger.error("[Agent] report analysis failed: %s", e, exc_info=True)
        _append_step(session, "报告分析失败", "error", str(e))
        return None


async def _diagnose_report_failure(session: dict[str, Any], report: dict[str, Any], analysis: dict[str, Any], failure: dict[str, Any]) -> dict[str, Any] | None:
    """Run the existing report diagnosis pipeline for one failure."""
    from features.reports import diagnose_report_failure

    report_timestamp = report.get("timestamp", "")
    details = analysis.get("details") or {}
    req = ReportDiagnosisRequest(
        test_name=failure.get("test_name") or "Unknown",
        error_message=failure.get("error_message") or "",
        stack_trace=failure.get("stack_trace") or "",
        report_name=report_timestamp,
        failure_index=int(failure.get("index") or 0),
        module=failure.get("module") or report.get("test_module") or "",
        test_type=details.get("test_type") or report.get("test_type") or "",
        suite_version=details.get("suite_version") or report.get("suite_version") or "",
    )

    _append_step(session, "失败诊断", "running", req.test_name)
    try:
        response = await diagnose_report_failure(req)
        payload = _json_body(response)
        if not payload.get("success"):
            _append_step(session, "失败诊断失败", "warning", payload.get("error") or "诊断失败", payload)
            return None
        diagnosis = payload.get("data") or {}
        root_cause = (diagnosis.get("ai_result") or {}).get("root_cause") or diagnosis.get("summary") or "诊断完成"
        suite_target = diagnosis.get("suite_target") or {}
        artifact = suite_target.get("artifact") or {}
        _append_step(
            session,
            "失败诊断",
            "done",
            root_cause,
            {
                "diagnosis": diagnosis,
                "artifact": artifact,
                "source_path": diagnosis.get("source_path") or (suite_target.get("source_guess") or {}).get("source_path", ""),
            },
        )
        return diagnosis
    except Exception as e:
        logger.error("[Agent] failure diagnosis failed: %s", e, exc_info=True)
        _append_step(session, "失败诊断异常", "error", str(e))
        return None


def _extract_symbols_for_apk_lookup(diagnosis: dict[str, Any], failure: dict[str, Any]) -> list[str]:
    symbols: list[str] = []
    for class_name in diagnosis.get("class_names") or []:
        simple = str(class_name).split(".")[-1].split("$")[0]
        if simple and simple not in symbols:
            symbols.append(simple)
    test_name = failure.get("test_name") or ""
    if "#" in test_name:
        cls, method = test_name.split("#", 1)
        for value in (cls.split(".")[-1].split("$")[0], method):
            if value and value not in symbols:
                symbols.append(value)
    return symbols[:5]


async def _wait_for_apk_analysis(
    task_id: str, request: AgentRequestShim, timeout_seconds: int = 180
) -> dict[str, Any] | None:
    from features.firmware import get_apk_status

    deadline = time.time() + timeout_seconds
    last_status = None
    while time.time() < deadline:
        payload = _json_body(await get_apk_status(task_id, request))
        data = payload.get("data") or {}
        last_status = data
        if data.get("status") == "completed":
            return data
        if data.get("status") == "error":
            return data
        await asyncio.sleep(3)
    return last_status


async def _read_apk_source_snippet(
    task_id: str, diagnosis: dict[str, Any], failure: dict[str, Any],
    request: AgentRequestShim,
) -> dict[str, Any] | None:
    """Find a likely decompiled source file and read a short snippet."""
    from features.firmware import find_apk_symbol_definition, get_apk_source

    symbols = _extract_symbols_for_apk_lookup(diagnosis, failure)
    definition = None
    for symbol in symbols:
        payload = _json_body(await find_apk_symbol_definition(
            task_id, request, symbol=symbol
        ))
        if payload.get("success"):
            definition = (payload.get("data") or {}).get("definition")
            if definition:
                break

    if not definition:
        return None

    path = definition.get("path") or ""
    if not path:
        return None

    payload = _json_body(await get_apk_source(
        task_id, request, path=path, view=True
    ))
    if not payload.get("success"):
        return {"definition": definition, "path": path, "error": payload.get("error") or "源码读取失败"}

    source = payload.get("data") or {}
    content = source.get("content") or ""
    line = int(definition.get("line") or 1)
    lines = content.splitlines()
    start = max(0, line - 8)
    end = min(len(lines), line + 12)
    snippet = "\n".join(f"{idx + 1}: {lines[idx]}" for idx in range(start, end))
    return {"definition": definition, "path": path, "line": line, "snippet": snippet}


async def _run_apk_source_analysis(
    session: dict[str, Any], plan: dict[str, Any], diagnosis: dict[str, Any],
    failure: dict[str, Any], request: AgentRequestShim,
) -> dict[str, Any] | None:
    """Import a suite APK/JAR, decompile it, and read a likely source snippet."""
    from features.firmware import analyze_apk
    from features.test_execution import create_suite_apk_analysis_task

    suite_target = diagnosis.get("suite_target") or {}
    artifact = suite_target.get("artifact") or {}
    artifact_path = artifact.get("path") or ""
    suite_path = suite_target.get("suite_path") or (plan.get("request") or {}).get("test_suite") or ""

    if not suite_path or not artifact_path:
        _append_step(session, "APK/源码分析", "warning", "未定位到可反编译的套件 APK/JAR", {"suite_target": suite_target})
        return None

    _append_step(session, "APK/源码分析", "running", f"导入构件 {artifact_path}")
    try:
        req = SuiteApkAnalyzeRequest(suite_path=suite_path, path=artifact_path)
        create_payload = _json_body(await create_suite_apk_analysis_task(req, request))
        if not create_payload.get("success"):
            _append_step(session, "APK/源码分析失败", "warning", create_payload.get("error") or "构件导入失败", create_payload)
            return None

        task = create_payload.get("data") or {}
        task_id = task.get("task_id")
        if not task_id:
            _append_step(session, "APK/源码分析失败", "warning", "反编译任务 ID 为空", create_payload)
            return None

        start_payload = _json_body(await analyze_apk(task_id, request))
        if not start_payload.get("success"):
            _append_step(session, "APK/源码分析失败", "warning", start_payload.get("error") or "反编译启动失败", start_payload)
            return None

        status = await _wait_for_apk_analysis(task_id, request)
        if not status or status.get("status") != "completed":
            _append_step(session, "APK/源码分析", "warning", f"反编译未完成: {(status or {}).get('status', 'timeout')}", {"task_id": task_id, "status": status})
            return {"task_id": task_id, "status": status}

        snippet = await _read_apk_source_snippet(task_id, diagnosis, failure, request)
        detail = f"反编译完成: {task.get('filename') or artifact_path}"
        if snippet and snippet.get("path"):
            detail += f"，命中源码 {snippet['path']}:{snippet.get('line', '')}"
        result = {"task_id": task_id, "task": task, "status": status, "snippet": snippet, "artifact": artifact}
        _append_step(session, "APK/源码分析", "done", detail, result)
        return result
    except Exception as e:
        logger.error("[Agent] APK source analysis failed: %s", e, exc_info=True)
        _append_step(session, "APK/源码分析异常", "error", str(e))
        return None


async def _run_failure_analysis_pipeline(
    session: dict[str, Any], plan: dict[str, Any], report: dict[str, Any],
    request: AgentRequestShim,
) -> dict[str, Any]:
    """Analyze report failures, diagnose the first failure, and optionally inspect APK source."""
    report_timestamp = report.get("timestamp", "")
    analysis = await _analyze_saved_report(session, report_timestamp)
    if not analysis:
        return {"report_timestamp": report_timestamp, "report_analysis": None}

    failures = [
        _normalize_failure(item, idx)
        for idx, item in enumerate(analysis.get("failures") or [])
    ]
    failures = [item for item in failures if item.get("test_name") or item.get("error_message")]
    result = {"report_timestamp": report_timestamp, "report_analysis": analysis, "failures": failures[:10]}
    if not failures:
        _append_message(session, "assistant", f"报告 {report_timestamp} 已分析，但没有提取到明确失败用例。")
        return result

    primary_failure = failures[0]
    diagnosis = await _diagnose_report_failure(session, report, analysis, primary_failure)
    result["diagnosis"] = diagnosis

    apk_result = None
    if diagnosis and (plan.get("policy") or {}).get("apk_source_analysis"):
        apk_result = await _run_apk_source_analysis(
            session, plan, diagnosis, primary_failure, request
        )
        result["apk_source_analysis"] = apk_result

    root_cause = ((diagnosis or {}).get("ai_result") or {}).get("root_cause") or (diagnosis or {}).get("summary") or "未生成根因"
    message_lines = [
        f"报告分析完成：{report_timestamp}",
        f"- 失败用例数: {len(failures)}",
        f"- 首个失败: {primary_failure.get('test_name') or 'Unknown'}",
        f"- 模块: {primary_failure.get('module') or '未知'}",
        f"- 根因: {root_cause}",
    ]
    if apk_result:
        snippet = apk_result.get("snippet") or {}
        message_lines.append(f"- APK任务: {apk_result.get('task_id')}")
        if snippet.get("path"):
            message_lines.append(f"- 源码命中: {snippet.get('path')}:{snippet.get('line')}")
    message_lines.append("后台分析已完成，当前仍保留在对话Agent页面；需要查看详情时点击“打开报告分析”或“打开APK分析”。")
    _append_message(
        session,
        "assistant",
        "\n".join(message_lines),
        kind="analysis",
        data={
            "report_timestamp": report_timestamp,
            "analysis": result,
            "action": "open_report_analysis",
        },
    )
    return result


async def _start_test_with_plan(session: dict[str, Any], request_shim: AgentRequestShim, plan: dict[str, Any], retry_timestamp: str = "") -> dict[str, Any]:
    from features.test_execution import TestStartRequest, start_test

    req_data = dict(plan.get("request") or {})
    if retry_timestamp:
        req_data["retry_dir"] = retry_timestamp
        req_data["test_module"] = ""
        req_data["test_case"] = ""
    req = TestStartRequest(**req_data)
    response = await start_test(request_shim, help=False, req=req)
    payload = _json_body(response)
    if payload.get("success"):
        correlation = payload.get("data") or {}
        _append_step(
            session,
            "启动测试" if not retry_timestamp else "启动 retry",
            "running",
            retry_timestamp or req.test_module or req.test_suite,
            {"request": req.model_dump(), "correlation": correlation},
        )
        if correlation.get("cluster_job_id"):
            active_run = session.setdefault("active_run", {})
            resolved_worker_id = correlation.get("worker_id") or req.worker_id
            active_run.update({
                "cluster_job_id": correlation["cluster_job_id"],
                "attempt_id": correlation.get("attempt_id", ""),
                "worker_id": resolved_worker_id,
            })
            workspace = session.setdefault("workspace_context", {})
            workspace.update({
                "scope_mode": "single" if _is_local_worker_id(resolved_worker_id) else "cluster",
                "worker_id": resolved_worker_id,
                "device_ids": list(req.devices),
                "suite_path": req.test_suite,
                "cluster_job_id": correlation["cluster_job_id"],
                "attempt_id": correlation.get("attempt_id", ""),
                "origin_page": "agent",
            })
    else:
        _append_step(session, "启动测试失败", "error", payload.get("error") or payload.get("message", ""), payload)
    return payload


async def _run_pre_actions(session: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    actions = plan.get("pre_actions") or []
    if not actions:
        return {"success": True}

    req = plan.get("request") or {}
    devices = req.get("devices") or []
    worker_id = req.get("worker_id") or _local_worker_id()
    for action in actions:
        if action.get("type") != "connect_wifi":
            continue
        if not _is_local_worker_id(worker_id):
            from features.cluster import ClusterDeviceAction, device_action

            response = await device_action(ClusterDeviceAction(
                worker_id=worker_id,
                devices=devices,
                action="wifi",
                ssid=action.get("ssid") or "",
                password=action.get("password") or "",
            ))
            payload = _jsonable(response)
            wifi_ssid = action.get("ssid") or ""
        else:
            from features.devices import WifiConnectRequest, connect_wifi

            wifi_req = WifiConnectRequest(
                devices=devices,
                ssid=action.get("ssid"),
                password=action.get("password"),
            )
            response = await connect_wifi(wifi_req)
            payload = _json_body(response)
            wifi_ssid = wifi_req.ssid
        summary = payload.get("summary") or {}
        results = payload.get("results") or []
        if not summary and results:
            summary = {
                "success": sum(1 for item in results if item.get("success")),
                "total": len(results),
            }
        detail = f"{wifi_ssid}: 成功 {summary.get('success', 0)}/{summary.get('total', len(devices))}"
        if payload.get("success"):
            _append_step(session, "连接 WiFi", "done", detail, payload)
        else:
            _append_step(session, "连接 WiFi 失败", "error", payload.get("error") or payload.get("message", ""), payload)
            return payload
    return {"success": True}


async def _monitor_agent_run(session_id: str, request_shim: AgentRequestShim) -> None:
    session = _agent_sessions.get(session_id)
    if not session:
        return

    plan = session.get("pending_plan") or session.get("active_run", {}).get("plan")
    if not plan:
        return

    client_id = session.get("client_id")
    policy = plan.get("policy", {})
    retries_left = int(policy.get("retry_count") or 0)
    attempt = 0
    latest_report_timestamp = None
    active_run = session.get("active_run") or {}
    baseline_report_timestamp = active_run.get("baseline_report_timestamp")

    try:
        while True:
            active_run = session.get("active_run") or {}
            cluster_job_id = active_run.get("cluster_job_id") or ""
            if cluster_job_id:
                from features.cluster import get_cluster_service

                repository = get_cluster_service().repository
                job = repository.get_job(cluster_job_id)
                while job and job.get("status") in _ACTIVE_CLUSTER_JOB_STATUSES:
                    await asyncio.sleep(3)
                    job = repository.get_job(cluster_job_id)
                if not job:
                    session["status"] = "error"
                    _append_step(session, "读取集群任务", "error", f"任务不存在: {cluster_job_id}")
                    return

                attempt_id = active_run.get("attempt_id") or job.get("current_attempt_id", "")
                report = None
                for _ in range(20):
                    report = _report_for_cluster_job(cluster_job_id, attempt_id)
                    if report and report.get("status") != "collecting":
                        break
                    if job.get("status") != "completed" and not report:
                        break
                    await asyncio.sleep(1)
                latest_report_timestamp = report.get("timestamp") if report else None
                job_failed = job.get("status") != "completed"
                failed = job_failed or _report_failed(report)
                _append_step(
                    session,
                    "读取集群测试结果",
                    "done" if not job_failed else "error",
                    f"{cluster_job_id} / {job.get('status')}"
                    + (f" / 报告 {latest_report_timestamp}" if latest_report_timestamp else " / 未生成报告"),
                    {"job": job, "report": report or {}},
                )

                workspace = session.setdefault("workspace_context", {})
                assigned_worker_id = job.get("assigned_worker_id", "")
                workspace.update({
                    "scope_mode": "single" if _is_local_worker_id(assigned_worker_id) else "cluster",
                    "worker_id": assigned_worker_id,
                    "cluster_job_id": cluster_job_id,
                    "attempt_id": attempt_id,
                    "report_timestamp": latest_report_timestamp or "",
                    "report_id": (report or {}).get("report_id", ""),
                    "artifact_id": (report or {}).get("artifact_id", ""),
                    "origin_page": "agent",
                })

                if failed and retries_left > 0 and latest_report_timestamp:
                    attempt += 1
                    retries_left -= 1
                    _append_message(
                        session, "assistant",
                        f"检测到失败，开始第 {attempt} 次 retry：{latest_report_timestamp}",
                    )
                    payload = await _start_test_with_plan(
                        session, request_shim, plan, latest_report_timestamp
                    )
                    if not payload.get("success"):
                        session["status"] = "error"
                        _append_message(
                            session, "assistant",
                            f"retry 启动失败：{payload.get('error') or payload.get('message')}",
                        )
                        return
                    continue

                if failed and policy.get("analyze_on_failure") and report:
                    session["status"] = "analyzing"
                    await _run_failure_analysis_pipeline(
                        session, plan, report, request_shim
                    )
                elif failed:
                    _append_message(
                        session, "assistant",
                        f"集群测试失败。任务：{cluster_job_id}，报告：{latest_report_timestamp or '未生成'}",
                    )
                else:
                    _append_message(
                        session, "assistant",
                        f"集群测试完成。任务：{cluster_job_id}，报告：{latest_report_timestamp or '未生成'}",
                        data={
                            "cluster_job_id": cluster_job_id,
                            "attempt_id": attempt_id,
                            "report_timestamp": latest_report_timestamp or "",
                        },
                    )
                session["status"] = "done"
                session["active_run"] = None
                return

            user_state = get_or_create_user_state(client_id)
            while user_state.get("running", False):
                await asyncio.sleep(3)
                user_state = get_or_create_user_state(client_id)

            await asyncio.sleep(1)
            report = _latest_report_for_client(client_id, exclude_timestamp=baseline_report_timestamp)
            if report:
                latest_report_timestamp = report.get("timestamp")
                failed = _report_failed(report)
                _append_step(
                    session,
                    "读取测试报告",
                    "done",
                    f"{latest_report_timestamp}，失败 {report.get('fail', 0)} / 总计 {report.get('total', 0)}",
                    {"report": report},
                )
            else:
                failed = False
                _append_step(session, "读取测试报告", "warning", "没有找到本次测试报告")

            if failed and retries_left > 0 and latest_report_timestamp:
                attempt += 1
                retries_left -= 1
                _append_message(session, "assistant", f"检测到失败，开始第 {attempt} 次 retry：{latest_report_timestamp}")
                payload = await _start_test_with_plan(session, request_shim, plan, latest_report_timestamp)
                if not payload.get("success"):
                    session["status"] = "error"
                    _append_message(session, "assistant", f"retry 启动失败：{payload.get('error') or payload.get('message')}")
                    return
                baseline_report_timestamp = latest_report_timestamp
                continue

            if failed and policy.get("analyze_on_failure") and latest_report_timestamp and report:
                session["status"] = "analyzing"
                await _run_failure_analysis_pipeline(
                    session, plan, report, request_shim
                )
            elif failed:
                _append_message(session, "assistant", f"测试完成但有失败。最近报告：{latest_report_timestamp or '未找到'}")
            else:
                _append_message(session, "assistant", f"测试完成，未检测到失败报告。最近报告：{latest_report_timestamp or '未找到'}")

            session["status"] = "done"
            session["active_run"] = None
            return
    except asyncio.CancelledError:
        session["status"] = "cancelled"
        session["active_run"] = None
        _append_step(session, "Agent 监控已停止", "warning", "用户取消了 Agent 后台监控")
        raise
    except Exception as e:
        logger.error("[Agent] monitor failed: %s", e, exc_info=True)
        session["status"] = "error"
        _append_step(session, "Agent 监控异常", "error", str(e))
        _append_message(session, "assistant", f"Agent 监控异常：{e}")
    finally:
        _agent_monitor_tasks.pop(session_id, None)


async def _execute_plan(session: dict[str, Any], request: Request, plan: dict[str, Any]) -> dict[str, Any]:
    req = plan.get("request", {})
    if not req.get("devices"):
        return {"success": False, "error": "没有可用设备"}
    if not req.get("test_suite") and not req.get("retry_dir"):
        return {"success": False, "error": "没有匹配到测试套件"}

    latest_existing_report = _latest_report_for_client(session.get("client_id", ""))
    session["status"] = "running"
    session["active_run"] = {
        "plan": plan,
        "started_at": _now_iso(),
        "baseline_report_timestamp": latest_existing_report.get("timestamp") if latest_existing_report else None,
        "worker_id": req.get("worker_id") or _local_worker_id(),
    }
    request_shim = AgentRequestShim(request)

    pre_result = await _run_pre_actions(session, plan)
    if not pre_result.get("success"):
        session["status"] = "error"
        return pre_result

    payload = await _start_test_with_plan(session, request_shim, plan)
    if not payload.get("success"):
        session["status"] = "error"
        return payload

    session["status"] = "monitoring"
    _append_message(session, "assistant", "测试已启动，我会继续监控完成状态，并按计划处理 retry/分析。")
    old_task = _agent_monitor_tasks.pop(session["session_id"], None)
    if old_task and not old_task.done():
        old_task.cancel()
    task = asyncio.create_task(_monitor_agent_run(session["session_id"], request_shim))
    _agent_monitor_tasks[session["session_id"]] = task
    session["monitor_task_id"] = id(task)
    return payload


@router.post("/api/agent/chat")
async def agent_chat(request: Request, req: AgentChatRequest = Body(...)):
    """Chat with the local GMS workflow Agent."""
    # Probabilistic session cleanup (~2% of requests) to avoid O(N) lock on every message
    if time.time() % 50 < 1:
        await _cleanup_sessions()
    message = (req.message or "").strip()
    if not message:
        return error_response("Message cannot be empty", 400)

    client_id = get_client_id_from_request(request)
    session = await _get_or_create_session(req.session_id, client_id)
    if req.workspace_context:
        supplied_workspace = _normalize_workspace_context(req.workspace_context)
        session["workspace_context"] = {
            **(session.get("workspace_context") or {}),
            **supplied_workspace,
        }
    workspace = session.get("workspace_context") or _normalize_workspace_context({})
    _append_message(session, "user", message)

    record_user_message(session, message)

    # --- 1. Check pending plan confirmation (existing behavior) ---
    pending_plan = session.get("pending_plan")
    wants_execute = req.execute or bool(re.search(r"^(确认执行|开始执行|执行|确认|start|run)$", message, re.IGNORECASE))
    if pending_plan and wants_execute:
        # Check if this is a generic action plan (new) or a test plan (legacy)
        if pending_plan.get("type") == "generic_action":
            intent_data = pending_plan.get("intent")

            if isinstance(intent_data, dict):
                tool_name = intent_data.get("tool_name", "")
                params = _apply_workspace_tool_params(
                    tool_name, intent_data.get("params", {}), workspace
                )
                category = intent_data.get("category", "")
            else:
                tool_name = getattr(intent_data, "tool_name", "")
                params = _apply_workspace_tool_params(
                    tool_name, getattr(intent_data, "params", {}), workspace
                )
                category = ""
            tool = registry.get(tool_name)
            missing = _missing_required_params(tool, params)
            if missing:
                _request_missing_params(session, missing)
                return success_response({"session": _session_payload(session)}, "Agent updated")
            tool_result = await executor.execute(session, request, tool_name, params)

            update_context(
                session, tool_name=tool_result.tool_name,
                category=category or (tool.category if tool else ""),
                entities=tool_result.entities,
                result_summary=tool_result.formatted_text[:200] if tool_result.formatted_text else "",
            )

            resp = gen_response(tool_result)
            _append_message(session, "assistant", resp.content, kind=resp.kind, data=resp.to_message_data())
            session["pending_plan"] = None
            session["status"] = "idle"
        else:
            # Legacy test execution plan
            result = await _execute_plan(session, request, pending_plan)
            if not result.get("success"):
                _append_message(session, "assistant", f"执行失败：{result.get('error') or result.get('message')}")
        return success_response({"session": _session_payload(session)}, "Agent updated")

    # --- 2. Resolve intent through multi-stage router ---
    from features.assistant.intent import resolve as resolve_intent

    if req.action:
        tool = registry.get(req.action)
        if not tool:
            _append_message(session, "assistant", f"未知操作：{req.action}")
            session["status"] = "idle"
            return success_response({"session": _session_payload(session)}, "Agent updated")

        params = _apply_workspace_tool_params(
            tool.name, _jsonable(req.params or {}), workspace
        )
        missing = _missing_required_params(tool, params)
        if missing:
            _request_missing_params(session, missing)
            return success_response({"session": _session_payload(session)}, "Agent updated")
        if tool.requires_confirm and not req.execute:
            session["pending_plan"] = {
                "intent": {"tool_name": tool.name, "params": params, "category": tool.category},
                "type": "generic_action",
            }
            session["status"] = "planning"
            _append_message(
                session,
                "assistant",
                "此操作需要确认：**" + tool.display_name + "**\n\n"
                "参数：" + _format_params(params) + "\n\n"
                "输入\"确认执行\"或点击执行按钮后启动。",
                kind="plan",
                data={"plan": {"tool_name": tool.name, "params": params, "type": "generic_action"}},
            )
            return success_response({"session": _session_payload(session)}, "Agent updated")

        tool_result = await executor.execute(session, request, tool.name, params)
        update_context(
            session,
            tool_name=tool_result.tool_name,
            category=tool.category,
            entities=tool_result.entities,
            result_summary=tool_result.formatted_text[:200] if tool_result.formatted_text else "",
        )
        resp = gen_response(tool_result)
        _append_message(session, "assistant", resp.content, kind=resp.kind, data=resp.to_message_data())
        session["status"] = "idle"
        session["pending_plan"] = None
        return success_response({"session": _session_payload(session)}, "Agent updated")

    intent = resolve_intent(message, session)

    # --- 2a. Low confidence: ask for clarification ---
    if not intent.tool_name or intent.confidence < 0.3:
        scored = registry.find(message, top_k=3, min_score=0.5)
        if scored:
            suggestions = [
                {"label": s.tool.display_name, "action": s.tool.name, "description": s.tool.description}
                for s in scored[:3]
            ]
            resp = generate_clarification(suggestions)
            _append_message(session, "assistant", resp.content, kind=resp.kind, data=resp.to_message_data())
        else:
            _append_message(session, "assistant", generate_capability_overview(), data={"page": "agent"})
        session["status"] = "idle"
        session["pending_plan"] = None
        return success_response({"session": _session_payload(session)}, "Agent updated")

    # --- 2b. Capabilities: special handling ---
    if intent.tool_name == "agent_capabilities":
        text = generate_capability_overview()
        _append_message(session, "assistant", text, data={"page": "agent"})
        session["status"] = "idle"
        session["pending_plan"] = None
        return success_response({"session": _session_payload(session)}, "Agent updated")

    if intent.tool_name == "page_overview":
        _append_message(
            session,
            "assistant",
            generate_page_overview(),
            data={
                "page": "agent",
                "quick_actions": page_quick_actions(),
            },
        )
        session["status"] = "idle"
        session["pending_plan"] = None
        return success_response({"session": _session_payload(session)}, "Agent updated")

    if intent.tool_name == "greeting":
        _append_message(session, "assistant", "你好，我是 GMS 远程测试 Agent。可以查设备/套件/报告/状态，也能生成测试计划、做 retry 和失败分析；问「每个页面功能」可以看完整页面说明。", data={"page": "agent"})
        session["status"] = "idle"
        session["pending_plan"] = None
        return success_response({"session": _session_payload(session)}, "Agent updated")

    # --- 2c. Navigate: special handling ---
    if intent.tool_name == "navigate":
        page = intent.params.get("page", "")
        if page:
            name, _desc = WEBAPP_PAGES.get(page, (page, ""))
            _append_message(session, "assistant", f"已打开「{name}」页面。", data={"page": page, "auto_open": True})
        else:
            _append_message(session, "assistant", "没有识别到要打开的页面。", data={"page": "agent"})
        session["status"] = "idle"
        session["pending_plan"] = None
        return success_response({"session": _session_payload(session)}, "Agent updated")

    # --- 2d. Run test: generate plan (existing behavior) ---
    if intent.is_run_test:
        legacy_intent = _parse_user_intent(message)
        selected_devices, device_details = _select_devices(legacy_intent, workspace)
        suite = _select_suite(legacy_intent, workspace)
        plan = _build_plan(
            legacy_intent, selected_devices, device_details, suite, workspace
        )
        session["pending_plan"] = plan
        session["status"] = "planning"
        _append_step(session, "生成测试计划", "done", "已根据自然语言匹配设备和测试套件", {"plan": plan})
        _append_message(session, "assistant", _summarize_plan(plan), kind="plan", data={"plan": plan})
        return success_response({"session": _session_payload(session)}, "Agent updated")

    tool_params = _apply_workspace_tool_params(
        intent.tool_name, intent.params, workspace
    )
    missing = _missing_required_params(intent.tool, tool_params)
    if missing:
        _request_missing_params(session, missing)
        return success_response({"session": _session_payload(session)}, "Agent updated")

    # --- 2e. Dangerous/confirm-required: show confirmation plan ---
    if intent.needs_confirm and not req.execute:
        tool = intent.tool
        tool_display = tool.display_name if tool else intent.tool_name
        plan_text = (
            "⚠️ 此操作需要确认：**" + tool_display + "**\n\n"
            "参数：" + _format_params(tool_params) + "\n\n"
            "输入\"确认执行\"或点击执行按钮后启动。"
        )
        session["pending_plan"] = {
            "intent": {
                "tool_name": intent.tool_name,
                "params": _jsonable(tool_params),
                "category": tool.category if tool else "",
            },
            "type": "generic_action",
        }
        session["status"] = "planning"
        _append_message(session, "assistant", plan_text, kind="plan",
                        data={"plan": {"tool_name": intent.tool_name, "params": tool_params, "type": "generic_action"}})
        return success_response({"session": _session_payload(session)}, "Agent updated")

    # --- 3. Execute tool ---
    tool_result = await executor.execute(session, request, intent.tool_name, tool_params)

    # --- 4. Update context ---
    update_context(
        session,
        tool_name=tool_result.tool_name,
        category=intent.tool.category if intent.tool else "",
        entities=tool_result.entities,
        result_count=len(tool_result.entities.get(next(iter(tool_result.entities.keys())), [])) if tool_result.entities else 0,
        result_summary=tool_result.formatted_text[:200] if tool_result.formatted_text else "",
    )

    # --- 5. Generate and send response ---
    resp = gen_response(tool_result)
    _append_message(session, "assistant", resp.content, kind=resp.kind, data=resp.to_message_data())

    session["status"] = "idle"
    session["pending_plan"] = None
    return success_response({"session": _session_payload(session)}, "Agent updated")


def _format_params(params: dict[str, Any]) -> str:
    """格式化参数为可读文本。"""
    if not params:
        return "无"
    lines = [f"- {k}: {v}" for k, v in params.items() if v]
    return "\n".join(lines) if lines else "无"


@router.get("/api/agent/sessions/{session_id}")
async def get_agent_session(session_id: str, request: Request):
    """Get Agent session status."""
    session = _agent_sessions.get(session_id)
    client_id = get_client_id_from_request(request)
    if not session or session.get("client_id") != client_id:
        return success_response(_expired_session_payload(session_id), "Agent session expired")
    return success_response({"session": _session_payload(session)})


@router.post("/api/agent/sessions/{session_id}/cancel")
async def cancel_agent_session(session_id: str, request: Request):
    """Cancel Agent monitoring state. Running tests should still be stopped from the test page."""
    session = _agent_sessions.get(session_id)
    client_id = get_client_id_from_request(request)
    if not session or session.get("client_id") != client_id:
        return success_response(_expired_session_payload(session_id), "Agent session expired")
    task = _agent_monitor_tasks.pop(session_id, None)
    if task and not task.done():
        task.cancel()
    session["status"] = "cancelled"
    session["active_run"] = None
    _append_step(session, "Agent 已取消", "warning", "如果测试仍在运行，请到测试界面停止测试")
    _append_message(session, "assistant", "Agent 会话已取消。如果测试仍在运行，请到测试界面停止测试。")
    return success_response({"session": _session_payload(session)})


@router.get("/api/agent/capabilities")
async def get_agent_capabilities():
    """List supported Agent capabilities with full tool catalog."""
    config = config_manager.load_config()
    categories = registry.get_all_categories()
    tools_by_category = {}
    for cat, tools in categories.items():
        tools_by_category[cat] = [
            {
                "name": t.name,
                "description": t.description,
                "api_path": t.api_path,
                "method": t.method,
                "readonly": t.is_readonly,
                "dangerous": t.is_dangerous,
            }
            for t in tools
        ]

    return success_response({
        "tools": [
            "list_devices", "list_test_suites", "start_test", "monitor_test",
            "retry_test", "find_latest_report", "analyze_report", "diagnose_failure",
            "analyze_suite_apk", "read_decompiled_source",
            "automation_dashboard", "automation_runs", "automation_run_cancel",
            "automation_run_retry", "cluster_status", "cluster_workers",
            "cluster_devices", "cluster_jobs", "cluster_job_cancel",
            "cluster_set_mode", "build_jobs", "knowledge_search",
            "knowledge_ask", "knowledge_create",
        ],
        "tool_catalog": tools_by_category,
        "total_tools": len(registry),
        "limits": {"max_retries": MAX_AGENT_RETRIES},
            "suite_source": "local" if config_manager.is_config_host_local(config) else "ssh",
    })
