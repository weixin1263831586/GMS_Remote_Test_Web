"""Conversation Agent router for guided GMS Remote Test workflows."""

import asyncio
import logging
import re
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, Field

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
    "redmine-agent": ("Redmine看板", "个人/部门 Redmine 统计、未回复问题和 RedmineAgent 扫描"),
    "gerrit-dashboard": ("Gerrit看板", "查询 Gerrit 变更和配置 Gerrit dashboard profiles"),
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


class AgentRequestShim:
    """Minimal request object for reusing existing route handlers."""

    def __init__(self, request: Request, query_params: dict[str, Any] | None = None):
        self.headers = request.headers
        self.client = request.client
        self.method = getattr(request, "method", "POST")
        self.url = getattr(request, "url", None)
        self.query_params = query_params if query_params is not None else getattr(request, "query_params", {})

    async def form(self):
        return {}


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


def _select_devices(intent: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    device_ids = device_manager.get_connected_devices(force_refresh=True)
    details = []
    for device_id in device_ids:
        lock_status = device_lock_manager.get_lock_status(device_id)
        details.append({
            "device_id": device_id,
            "locked": bool(lock_status),
            "locked_by": lock_status.get("locked_by", "") if lock_status else "",
        })

    requested = intent.get("devices") or []
    if requested:
        available = {item["device_id"]: item for item in details}
        return [dev for dev in requested if dev in available and not available[dev]["locked"]], details

    unlocked = [item["device_id"] for item in details if not item["locked"]]
    count = int(intent.get("device_count") or 1)
    return unlocked if count <= 0 else unlocked[:count], details


def _score_suite(suite: dict[str, Any], test_type: str, module: str) -> int:
    score = 0
    haystack = " ".join(str(suite.get(key, "")) for key in ("test_type", "version", "tools_path", "binary")).lower()
    if test_type and test_type.lower() in haystack:
        score += 20
    if module:
        module_prefix = re.sub(r"TestCases?$", "", module, flags=re.IGNORECASE).lower()
        if module_prefix and module_prefix in haystack:
            score += 5
    if suite.get("tools_path"):
        score += 1
    return score


def _list_suites() -> list[dict[str, Any]]:
    from features.test_execution import _get_available_test_suites

    config = config_manager.load_config()
    base_path = config.get("suites_path") or get_default_suites_path(config)
    return _get_available_test_suites(config, base_path)


def _select_suite(intent: dict[str, Any]) -> dict[str, Any] | None:
    suites = _list_suites()
    test_type = intent.get("test_type") or ""
    module = intent.get("test_module") or ""
    if not suites:
        return None
    return max(suites, key=lambda suite: _score_suite(suite, test_type, module))


def _build_plan(intent: dict[str, Any], selected_devices: list[str], device_details: list[dict[str, Any]], suite: dict[str, Any] | None) -> dict[str, Any]:
    test_suite = suite.get("tools_path", "") if suite else ""
    test_type = intent.get("test_type") or (suite.get("test_type", "") if suite else "")
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
        steps.append({"title": "连接 WiFi", "detail": "测试前连接到 AndroidWifi"})
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
    }


def _summarize_plan(plan: dict[str, Any]) -> str:
    req = plan.get("request", {})
    policy = plan.get("policy", {})
    lines = [
        "我已经生成执行计划，确认后开始：",
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


def _latest_report_for_client(client_id: str, exclude_timestamp: str | None = None) -> dict[str, Any] | None:
    reports = test_report_db.get_reports(limit=20, user_only=client_id)
    if not reports:
        reports = test_report_db.get_reports(limit=20)
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


async def _wait_for_apk_analysis(task_id: str, timeout_seconds: int = 180) -> dict[str, Any] | None:
    from features.firmware import get_apk_status

    deadline = time.time() + timeout_seconds
    last_status = None
    while time.time() < deadline:
        payload = _json_body(await get_apk_status(task_id))
        data = payload.get("data") or {}
        last_status = data
        if data.get("status") == "completed":
            return data
        if data.get("status") == "error":
            return data
        await asyncio.sleep(3)
    return last_status


async def _read_apk_source_snippet(task_id: str, diagnosis: dict[str, Any], failure: dict[str, Any]) -> dict[str, Any] | None:
    """Find a likely decompiled source file and read a short snippet."""
    from features.firmware import find_apk_symbol_definition, get_apk_source

    symbols = _extract_symbols_for_apk_lookup(diagnosis, failure)
    definition = None
    for symbol in symbols:
        payload = _json_body(await find_apk_symbol_definition(task_id, symbol=symbol))
        if payload.get("success"):
            definition = (payload.get("data") or {}).get("definition")
            if definition:
                break

    if not definition:
        return None

    path = definition.get("path") or ""
    if not path:
        return None

    payload = _json_body(await get_apk_source(task_id, path=path, view=True))
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


async def _run_apk_source_analysis(session: dict[str, Any], plan: dict[str, Any], diagnosis: dict[str, Any], failure: dict[str, Any]) -> dict[str, Any] | None:
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
        create_payload = _json_body(await create_suite_apk_analysis_task(req))
        if not create_payload.get("success"):
            _append_step(session, "APK/源码分析失败", "warning", create_payload.get("error") or "构件导入失败", create_payload)
            return None

        task = create_payload.get("data") or {}
        task_id = task.get("task_id")
        if not task_id:
            _append_step(session, "APK/源码分析失败", "warning", "反编译任务 ID 为空", create_payload)
            return None

        start_payload = _json_body(await analyze_apk(task_id))
        if not start_payload.get("success"):
            _append_step(session, "APK/源码分析失败", "warning", start_payload.get("error") or "反编译启动失败", start_payload)
            return None

        status = await _wait_for_apk_analysis(task_id)
        if not status or status.get("status") != "completed":
            _append_step(session, "APK/源码分析", "warning", f"反编译未完成: {(status or {}).get('status', 'timeout')}", {"task_id": task_id, "status": status})
            return {"task_id": task_id, "status": status}

        snippet = await _read_apk_source_snippet(task_id, diagnosis, failure)
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


async def _run_failure_analysis_pipeline(session: dict[str, Any], plan: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
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
        apk_result = await _run_apk_source_analysis(session, plan, diagnosis, primary_failure)
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
        _append_step(
            session,
            "启动测试" if not retry_timestamp else "启动 retry",
            "running",
            retry_timestamp or req.test_module or req.test_suite,
            {"request": req.model_dump()},
        )
    else:
        _append_step(session, "启动测试失败", "error", payload.get("error") or payload.get("message", ""), payload)
    return payload


async def _run_pre_actions(session: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    actions = plan.get("pre_actions") or []
    if not actions:
        return {"success": True}

    req = plan.get("request") or {}
    devices = req.get("devices") or []
    for action in actions:
        if action.get("type") != "connect_wifi":
            continue
        from features.devices import WifiConnectRequest, connect_wifi

        wifi_req = WifiConnectRequest(
            devices=devices,
            ssid=action.get("ssid"),
            password=action.get("password"),
        )
        response = await connect_wifi(wifi_req)
        payload = _json_body(response)
        summary = payload.get("summary") or {}
        detail = f"{wifi_req.ssid}: 成功 {summary.get('success', 0)}/{summary.get('total', len(devices))}"
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
                await _run_failure_analysis_pipeline(session, plan, report)
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
    _append_message(session, "user", message)

    # Record user message in context
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
                params = intent_data.get("params", {})
                category = intent_data.get("category", "")
            else:
                tool_name = getattr(intent_data, "tool_name", "")
                params = getattr(intent_data, "params", {})
                category = ""
            tool = registry.get(tool_name)
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

        params = _jsonable(req.params or {})
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
        selected_devices, device_details = _select_devices(legacy_intent)
        suite = _select_suite(legacy_intent)
        plan = _build_plan(legacy_intent, selected_devices, device_details, suite)
        session["pending_plan"] = plan
        session["status"] = "planning"
        _append_step(session, "生成测试计划", "done", "已根据自然语言匹配设备和测试套件", {"plan": plan})
        _append_message(session, "assistant", _summarize_plan(plan), kind="plan", data={"plan": plan})
        return success_response({"session": _session_payload(session)}, "Agent updated")

    # --- 2e. Dangerous/confirm-required: show confirmation plan ---
    if intent.needs_confirm and not req.execute:
        tool = intent.tool
        tool_display = tool.display_name if tool else intent.tool_name
        plan_text = (
            "⚠️ 此操作需要确认：**" + tool_display + "**\n\n"
            "参数：" + _format_params(intent.params) + "\n\n"
            "输入\"确认执行\"或点击执行按钮后启动。"
        )
        # Store intent as pending plan for confirmation
        session["pending_plan"] = {
            "intent": {
                "tool_name": intent.tool_name,
                "params": _jsonable(intent.params),
                "category": tool.category if tool else "",
            },
            "type": "generic_action",
        }
        session["status"] = "planning"
        _append_message(session, "assistant", plan_text, kind="plan",
                        data={"plan": {"tool_name": intent.tool_name, "params": intent.params, "type": "generic_action"}})
        return success_response({"session": _session_payload(session)}, "Agent updated")

    # --- 3. Execute tool ---
    tool_result = await executor.execute(session, request, intent.tool_name, intent.params)

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
        ],
        "tool_catalog": tools_by_category,
        "total_tools": len(registry),
        "limits": {"max_retries": MAX_AGENT_RETRIES},
            "suite_source": "local" if config_manager.is_config_host_local(config) else "ssh",
    })
