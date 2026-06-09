"""Conversation Agent router for guided GMS Remote Test workflows."""

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.api_response import error_response, success_response
from core.clients import get_client_id_from_request
from core.config import config_manager
from core.devices import device_manager, get_or_create_user_state
from core.test_report_db import test_report_db
from core.test_suite_utils import (
    detect_test_type_from_suite_path,
    get_default_suites_path,
    is_config_host_local,
)
from core.schemas import ReportDiagnosisRequest, SuiteApkAnalyzeRequest
from modules.device_lock_manager import device_lock_manager

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
    "tools": ("常用网址", "管理常用站点"),
    "security-audit": ("安全审计", "查看访问和接口审计"),
    "gms-assistant": ("GMS助手", "外部 GMS 助手"),
    "agent": ("对话Agent", "自然语言操作 Web_app"),
}

_agent_sessions: Dict[str, Dict[str, Any]] = {}
_agent_sessions_lock = asyncio.Lock()
_agent_monitor_tasks: Dict[str, asyncio.Task] = {}


class AgentChatRequest(BaseModel):
    """Request body for Agent chat messages."""

    message: str = Field(default="", max_length=4000)
    session_id: Optional[str] = None
    execute: bool = False
    action: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)


class AgentRequestShim:
    """Minimal request object for reusing existing route handlers."""

    def __init__(self, request: Request, query_params: Optional[Dict[str, Any]] = None):
        self.headers = request.headers
        self.client = request.client
        self.method = getattr(request, "method", "POST")
        self.url = getattr(request, "url", None)
        self.query_params = query_params if query_params is not None else getattr(request, "query_params", {})

    async def form(self):
        return {}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_message(role: str, content: str, kind: str = "text", data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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


def _session_payload(session: Dict[str, Any]) -> Dict[str, Any]:
    return _jsonable(session)


def _expired_session_payload(session_id: str) -> Dict[str, Any]:
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


async def _get_or_create_session(session_id: Optional[str], client_id: str) -> Dict[str, Any]:
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


def _append_step(session: Dict[str, Any], title: str, status: str = "done", detail: str = "", data: Optional[Dict[str, Any]] = None) -> None:
    session.setdefault("steps", []).append({
        "id": str(uuid.uuid4()),
        "title": title,
        "status": status,
        "detail": detail,
        "data": data or {},
        "created_at": _now_iso(),
    })
    session["updated_at"] = _now_iso()


def _append_message(session: Dict[str, Any], role: str, content: str, kind: str = "text", data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    msg = _new_message(role, content, kind=kind, data=data)
    session.setdefault("messages", []).append(msg)
    session["updated_at"] = _now_iso()
    return msg


def _parse_int_cn(text: str, default: int = 1) -> int:
    cn_map = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5}
    for word, value in cn_map.items():
        if word in text:
            return value
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else default


def _extract_test_type(text: str) -> str:
    upper = text.upper()
    for test_type in ["GTS-ROOT", "CTS", "GTS", "STS", "VTS", "APTS", "GSI"]:
        if test_type in upper:
            return test_type
    return ""


def _extract_module_and_case(text: str) -> tuple[str, str]:
    module = ""
    case = ""

    hash_match = re.search(r"([A-Za-z0-9_.-]+)#([A-Za-z0-9_.$-]+)", text)
    if hash_match:
        module = hash_match.group(1)
        case = hash_match.group(2)

    if not module:
        module_match = re.search(
            r"(?:模块|module|跑|执行|测试)\s*(?:测试)?\s*[:：]?\s*([A-Za-z0-9_.-]*(?:TestCases|Tests|Test|Cases|_test)[A-Za-z0-9_.-]*)",
            text,
            re.IGNORECASE,
        )
        if module_match:
            module = module_match.group(1)

    if not module:
        fallback = re.search(
            r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]*(?:TestCases|Tests|Test|Cases|_test)[A-Za-z0-9_.-]*)(?![A-Za-z0-9_.-])",
            text,
            re.IGNORECASE,
        )
        if fallback:
            module = fallback.group(1)

    if not case:
        case_match = re.search(r"(?:\bcase\b|用例)\s*[:：]?\s*([A-Za-z0-9_.$-]+)", text, re.IGNORECASE)
        if case_match:
            case = case_match.group(1)

    return module, case


def _extract_retry_count(text: str) -> int:
    if not re.search(r"retry|重试|失败.*继续|再跑", text, re.IGNORECASE):
        return 0
    match = re.search(r"(?:retry|重试|继续)\s*(\d+)\s*(?:次)?", text, re.IGNORECASE)
    if not match:
        match = re.search(r"(\d+)\s*次.*(?:retry|重试|继续)", text, re.IGNORECASE)
    if match:
        return max(0, min(MAX_AGENT_RETRIES, int(match.group(1))))
    return 1


def _extract_device_count(text: str) -> int:
    if re.search(r"全部|所有|all", text, re.IGNORECASE):
        return 0
    match = re.search(r"(?<![A-Za-z0-9])(\d+)\s*(?:台|个)?\s*(?:设备|device)", text, re.IGNORECASE)
    if match:
        return max(1, min(8, int(match.group(1))))
    if re.search(r"(一|两|二|三|四|五)\s*台", text):
        return _parse_int_cn(text, 1)
    return 1


def _extract_device_ids(text: str) -> List[str]:
    ids = []
    for token in re.findall(r"\b[A-Za-z0-9][A-Za-z0-9_.:-]{5,}\b", text):
        upper = token.upper()
        if re.fullmatch(r"RK\d+", token, re.IGNORECASE):
            continue
        if upper in {"TESTCASES", "ANDROID", "REPORT", "RETRY"}:
            continue
        if any(marker in token for marker in ("Test", "Cases")):
            continue
        ids.append(token)
    return list(dict.fromkeys(ids))[:8]


def _parse_user_intent(message: str) -> Dict[str, Any]:
    text = message.strip()
    lowered = text.lower()
    module, case = _extract_module_and_case(text)
    test_type = _extract_test_type(text)
    retry_count = _extract_retry_count(text)
    explicit_devices = _extract_device_ids(text)

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


def _is_run_test_request(text: str) -> bool:
    """Return True only for requests that clearly start/retry a test run."""
    lowered = text.lower()
    if re.search(r"\b(run|start|retry)\b|启动|开始|执行|重试|再跑|跑测试|跑\s*[a-z0-9_.-]*_test", lowered):
        return True
    if re.search(r"(^|[，,。;\s])跑\s*[A-Za-z0-9_.-]*(?:TestCases|Tests|Test|Cases|_test)", text, re.IGNORECASE):
        return True
    if re.search(r"帮我跑|帮忙跑|给我跑", text):
        return True
    return False


def _detect_webapp_intent(text: str) -> str:
    """Detect the user's Web_app query intent from natural language.

    Strategy: first try strict patterns (topic + query verb), then fall back
    to short-topic detection where the topic word alone implies a query
    (e.g. "测试套件" or "设备" with no other qualifying words).
    """
    lowered = text.lower().strip()
    query_words = r"多少|几个|有哪些|列表|列出|查看|查询|显示|状态|统计|最近|当前|有吗|情况"

    # --- Capabilities / help ---
    if re.search(r"你能做什么|能干什么|功能|帮助|怎么用|使用方法|全功能|web_app", lowered):
        return "capabilities"

    # --- Navigation ---
    if re.search(r"打开|跳转|进入", lowered) and not re.search(query_words, lowered):
        return "navigate"

    # --- Test status / health (exact patterns, no fallback needed) ---
    if re.search(r"测试状态|运行状态|正在跑|是否.*运行|status", lowered):
        return "test_status"
    if re.search(r"系统健康|健康检查|服务状态|health", lowered):
        return "health"

    # --- Topic + query-verb (strict) ---
    if re.search(r"测试套件|suite", lowered) and re.search(query_words, lowered):
        return "suites"
    if re.search(r"设备|adb|device", lowered) and re.search(query_words + r"|空闲|可用|占用", lowered):
        return "devices"
    if re.search(r"报告|report", lowered) and re.search(query_words, lowered):
        return "reports"
    if re.search(r"apk|反编译|jadx", lowered) and re.search(query_words + r"|任务", lowered):
        return "apk_tasks"
    if re.search(r"用户|在线|user", lowered) and re.search(query_words, lowered):
        return "users"
    if re.search(r"配置|主机|local_server|opengrok|ai配置|config", lowered) and re.search(query_words, lowered):
        return "config"

    # --- Short-topic fallback: bare topic word = implicit query ---
    # Only activate for short messages (≤16 chars) that aren't run_test requests.
    # This catches follow-ups like "测试套件", "设备", "报告" after a capabilities listing.
    if len(text) <= 16 and not _is_run_test_request(text):
        if re.search(r"^测试套件|^[有几个]*套件|^suites?$", lowered):
            return "suites"
        if re.search(r"^设备|^[有几空闲可用占用]*设备?|^devices?$|^adb$", lowered):
            return "devices"
        if re.search(r"^报告|^[有几个]*报告|^reports?$", lowered):
            return "reports"
        if re.search(r"^apk|^反编译|^jadx", lowered):
            return "apk_tasks"
        if re.search(r"^用户|^[在线]*用户|^users?$", lowered):
            return "users"
        if re.search(r"^配置|^config$", lowered):
            return "config"

    # --- Navigation with page name ---
    if re.search(r"打开|跳转|进入", lowered):
        return "navigate"

    return ""


def _format_table_lines(items: List[str], max_items: int = 8) -> str:
    return "\n".join(f"- {item}" for item in items[:max_items]) if items else "- 无"


def _suite_summary_text(suites: List[Dict[str, Any]]) -> str:
    counts: Dict[str, int] = {}
    for suite in suites:
        key = str(suite.get("test_type") or "unknown").upper()
        counts[key] = counts.get(key, 0) + 1
    count_line = "，".join(f"{key}: {value}" for key, value in sorted(counts.items())) or "无"
    recent = [
        f"{suite.get('test_type', '').upper()} {suite.get('version') or suite.get('binary') or '-'} -> {suite.get('tools_path')}"
        for suite in suites[:8]
    ]
    return f"当前发现 {len(suites)} 个测试套件。\n按类型统计：{count_line}\n\n前 {min(len(suites), 8)} 个：\n{_format_table_lines(recent)}"


def _device_summary_text(selected_devices: List[str], device_details: List[Dict[str, Any]]) -> str:
    locked = [item for item in device_details if item.get("locked")]
    unlocked = [item for item in device_details if not item.get("locked")]
    lines = [
        f"{item.get('device_id')} {'已占用: ' + item.get('locked_by', '') if item.get('locked') else '空闲'}"
        for item in device_details[:12]
    ]
    return (
        f"当前发现 {len(device_details)} 台 ADB 设备，空闲 {len(unlocked)} 台，占用 {len(locked)} 台。\n"
        f"Agent 默认可选择：{', '.join(selected_devices) or '无'}\n\n设备列表：\n{_format_table_lines(lines, 12)}"
    )


def _reports_summary_text(reports: List[Dict[str, Any]]) -> str:
    stats = test_report_db.get_statistics()
    lines = [
        f"{r.get('timestamp')} | {r.get('test_type', 'UNKNOWN')} | pass {r.get('pass', 0)} fail {r.get('fail', 0)} total {r.get('total', 0)} | {r.get('client_id', '')}"
        for r in reports[:8]
    ]
    return (
        f"当前记录报告 {stats.get('total_reports', len(reports))} 份，最近 7 天 {stats.get('recent_week', 0)} 份。\n"
        f"按类型统计：{stats.get('type_counts', {})}\n\n最近报告：\n{_format_table_lines(lines)}"
    )


async def _handle_webapp_query(session: Dict[str, Any], request: Request, intent: str, text: str) -> bool:
    """Handle non-mutating Web_app queries. Returns True if handled."""
    if not intent:
        return False

    _append_step(session, "识别意图", "done", intent)

    if intent == "suites":
        suites = await asyncio.to_thread(_list_suites)
        _append_message(session, "assistant", _suite_summary_text(suites), data={"page": "test-suites", "suites": suites[:20], "count": len(suites)})
        return True

    if intent == "devices":
        selected, details = await asyncio.to_thread(_select_devices, {"device_count": 1})
        _append_message(session, "assistant", _device_summary_text(selected, details), data={"page": "devices", "devices": details})
        return True

    if intent == "reports":
        reports = test_report_db.get_reports(limit=8)
        _append_message(session, "assistant", _reports_summary_text(reports), data={"page": "reports", "reports": reports})
        return True

    if intent == "apk_tasks":
        from routers.apk import list_apk_tasks
        payload = _json_response_body(await list_apk_tasks())
        tasks = (payload.get("data") or {}).get("tasks", [])
        lines = [f"{t.get('filename') or t.get('task_id')} | {t.get('status')} | {t.get('progress', 0)}%" for t in tasks[:8]]
        _append_message(session, "assistant", f"当前 APK/反编译任务 {len(tasks)} 个。\n{_format_table_lines(lines)}", data={"page": "apk-analysis", "apk_tasks": tasks})
        return True

    if intent == "users":
        from routers.users import list_users
        payload = _json_response_body(await list_users())
        users = payload.get("users", [])
        lines = [f"{u.get('username')}@{u.get('ip')} | {'测试中' if u.get('running') else '空闲'} | 设备: {', '.join(u.get('devices') or []) or '-'}" for u in users[:10]]
        _append_message(session, "assistant", f"当前在线用户 {payload.get('total', len(users))} 个。\n{_format_table_lines(lines, 10)}", data={"page": "users", "users": users})
        return True

    if intent == "test_status":
        client_id = session.get("client_id") or get_client_id_from_request(request)
        user_state = get_or_create_user_state(client_id)
        logs = user_state.get("logs", [])
        tail_logs = [str(item)[-240:] for item in list(logs)[-5:]]
        text = (
            f"当前用户测试状态：{'运行中' if user_state.get('running') else '未运行'}\n"
            f"占用设备：{', '.join(user_state.get('devices') or []) or '无'}\n"
            f"日志条数：{len(logs)}\n\n最近日志：\n{_format_table_lines(tail_logs, 5)}"
        )
        _append_message(session, "assistant", text, data={"page": "test", "running": user_state.get("running", False)})
        return True

    if intent == "health":
        from routers.system import health_check
        payload = _json_response_body(await health_check())
        modules = payload.get("modules", {})
        text = (
            f"系统状态：{payload.get('status')}\n"
            f"服务：{payload.get('service')}\n"
            f"WebSocket连接：{payload.get('websocket_connections')}\n"
            f"模块：{', '.join(f'{k}={v}' for k, v in modules.items())}"
        )
        _append_message(session, "assistant", text, data={"page": "security-audit", "health": payload})
        return True

    if intent == "config":
        config = config_manager.load_config()
        safe = {
            "ubuntu_host": config.get("ubuntu_host"),
            "ubuntu_user": config.get("ubuntu_user"),
            "local_server": config.get("local_server"),
            "suites_path": config.get("suites_path"),
            "ai_enabled": bool((config.get("ai_models") or {}).get("enabled")),
            "opengrok": bool((config.get("opengrok") or {}).get("base_url")),
        }
        lines = [f"{key}: {value}" for key, value in safe.items()]
        _append_message(session, "assistant", "当前关键配置：\n" + _format_table_lines(lines), data={"page": "api-docs", "config": safe})
        return True

    if intent == "navigate":
        page = _resolve_page_from_text(text)
        if page:
            name, desc = WEBAPP_PAGES[page]
            _append_message(session, "assistant", f"可以打开「{name}」页面：{desc}", data={"page": page})
        else:
            pages = [f"{name}({page})" for page, (name, _) in WEBAPP_PAGES.items()]
            _append_message(session, "assistant", "没有识别到要打开的页面。支持页面：\n" + _format_table_lines(pages, 20))
        return True

    if intent == "capabilities":
        _append_message(session, "assistant", _agent_capabilities_text(), data={"page": "agent"})
        return True

    return False


def _resolve_page_from_text(text: str) -> str:
    aliases = {
        "测试界面": "test", "跑测试": "test", "测试日志": "test",
        "主机桌面": "desktop", "桌面": "desktop", "vnc": "desktop",
        "终端": "terminal", "主机终端": "terminal",
        "用户": "users", "用户管理": "users",
        "设备": "devices", "设备管理": "devices", "adb": "devices",
        "报告管理": "reports", "报告列表": "reports",
        "报告分析": "report-analysis", "诊断": "report-analysis",
        "apk": "apk-analysis", "apk分析": "apk-analysis", "反编译": "apk-analysis",
        "测试套件": "test-suites", "套件": "test-suites",
        "接口": "api-docs", "api": "api-docs",
        "架构": "architecture",
        "常用网址": "tools", "网址": "tools",
        "审计": "security-audit", "安全审计": "security-audit",
        "gms助手": "gms-assistant",
        "agent": "agent", "对话agent": "agent",
    }
    lowered = text.lower()
    for key, page in aliases.items():
        if key.lower() in lowered:
            return page
    return ""


def _agent_capabilities_text() -> str:
    page_lines = [f"{name}: {desc}" for _, (name, desc) in WEBAPP_PAGES.items()]
    return (
        "我现在按 Web_app 功能分两类处理：\n"
        "1. 查询类直接回答：设备、测试套件、报告、APK任务、用户、测试状态、系统健康、配置摘要。\n"
        "2. 执行类先生成计划，确认后执行：启动测试、失败 retry、报告诊断、APK/源码分析。\n\n"
        "页面能力：\n" + _format_table_lines(page_lines, 30)
    )


def _select_devices(intent: Dict[str, Any]) -> tuple[List[str], List[Dict[str, Any]]]:
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


def _score_suite(suite: Dict[str, Any], test_type: str, module: str) -> int:
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


def _list_suites() -> List[Dict[str, Any]]:
    from routers.tests import _get_available_test_suites

    config = config_manager.load_config()
    base_path = config.get("suites_path") or get_default_suites_path(config)
    return _get_available_test_suites(config, base_path)


def _select_suite(intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    suites = _list_suites()
    test_type = intent.get("test_type") or ""
    module = intent.get("test_module") or ""
    if not suites:
        return None
    return max(suites, key=lambda suite: _score_suite(suite, test_type, module))


def _build_plan(intent: Dict[str, Any], selected_devices: List[str], device_details: List[Dict[str, Any]], suite: Optional[Dict[str, Any]]) -> Dict[str, Any]:
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
        pre_actions.append({
            "type": "connect_wifi",
            "ssid": intent.get("wifi_ssid") or "AndroidWifi",
            "password": intent.get("wifi_password") or "1234567890",
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


def _summarize_plan(plan: Dict[str, Any]) -> str:
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


def _json_response_body(response: Any) -> Dict[str, Any]:
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
            return json.loads(body)
    except Exception as e:
        logger.warning("[Agent] Failed to parse JSON response from %s: %s", type(response).__name__, e)
    return {"success": False, "error": f"Invalid JSON response: {type(response).__name__}"}


def _latest_report_for_client(client_id: str, exclude_timestamp: Optional[str] = None) -> Optional[Dict[str, Any]]:
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


def _report_failed(report: Optional[Dict[str, Any]]) -> bool:
    if not report:
        return False
    fail_count = report.get("fail", report.get("fail_count", 0)) or 0
    try:
        return int(fail_count) > 0
    except Exception:
        return False


def _normalize_failure(raw_failure: Dict[str, Any], index: int = 0) -> Dict[str, Any]:
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


async def _analyze_saved_report(session: Dict[str, Any], report_timestamp: str) -> Optional[Dict[str, Any]]:
    """Analyze a saved report and append an Agent step."""
    from core.test_report import test_report_manager

    _append_step(session, "报告分析", "running", f"正在分析报告 {report_timestamp}")
    try:
        analysis = await asyncio.to_thread(test_report_manager.analyze_report, report_timestamp)
        if not analysis:
            _append_step(session, "报告分析", "warning", "报告存在，但未能解析 test_result.xml")
            return None

        summary = analysis.get("summary") or {}
        failures = analysis.get("failures") or []
        detail = f"总计 {summary.get('total', 0)}，通过 {summary.get('pass', 0)}，失败 {summary.get('fail', summary.get('failed', 0))}"
        _append_step(session, "报告分析", "done", detail, {"report_analysis": analysis})
        return analysis
    except Exception as e:
        logger.error("[Agent] report analysis failed: %s", e, exc_info=True)
        _append_step(session, "报告分析失败", "error", str(e))
        return None


async def _diagnose_report_failure(session: Dict[str, Any], report: Dict[str, Any], analysis: Dict[str, Any], failure: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Run the existing report diagnosis pipeline for one failure."""
    from routers.reports import diagnose_report_failure

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
        payload = _json_response_body(response)
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


def _extract_symbols_for_apk_lookup(diagnosis: Dict[str, Any], failure: Dict[str, Any]) -> List[str]:
    symbols: List[str] = []
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


async def _wait_for_apk_analysis(task_id: str, timeout_seconds: int = 180) -> Optional[Dict[str, Any]]:
    from routers.apk import get_apk_status

    deadline = time.time() + timeout_seconds
    last_status = None
    while time.time() < deadline:
        payload = _json_response_body(await get_apk_status(task_id))
        data = payload.get("data") or {}
        last_status = data
        if data.get("status") == "completed":
            return data
        if data.get("status") == "error":
            return data
        await asyncio.sleep(3)
    return last_status


async def _read_apk_source_snippet(task_id: str, diagnosis: Dict[str, Any], failure: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Find a likely decompiled source file and read a short snippet."""
    from routers.apk import find_apk_symbol_definition, get_apk_source

    symbols = _extract_symbols_for_apk_lookup(diagnosis, failure)
    definition = None
    for symbol in symbols:
        payload = _json_response_body(await find_apk_symbol_definition(task_id, symbol=symbol))
        if payload.get("success"):
            definition = (payload.get("data") or {}).get("definition")
            if definition:
                break

    if not definition:
        return None

    path = definition.get("path") or ""
    if not path:
        return None

    payload = _json_response_body(await get_apk_source(task_id, path=path, view=True))
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


async def _run_apk_source_analysis(session: Dict[str, Any], plan: Dict[str, Any], diagnosis: Dict[str, Any], failure: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Import a suite APK/JAR, decompile it, and read a likely source snippet."""
    from routers.apk import analyze_apk
    from routers.tests import create_suite_apk_analysis_task

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
        create_payload = _json_response_body(await create_suite_apk_analysis_task(req))
        if not create_payload.get("success"):
            _append_step(session, "APK/源码分析失败", "warning", create_payload.get("error") or "构件导入失败", create_payload)
            return None

        task = create_payload.get("data") or {}
        task_id = task.get("task_id")
        if not task_id:
            _append_step(session, "APK/源码分析失败", "warning", "反编译任务 ID 为空", create_payload)
            return None

        start_payload = _json_response_body(await analyze_apk(task_id))
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


async def _run_failure_analysis_pipeline(session: Dict[str, Any], plan: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
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


async def _start_test_with_plan(session: Dict[str, Any], request_shim: AgentRequestShim, plan: Dict[str, Any], retry_timestamp: str = "") -> Dict[str, Any]:
    from core.schemas import TestStartRequest
    from routers.tests import start_test

    req_data = dict(plan.get("request") or {})
    if retry_timestamp:
        req_data["retry_dir"] = retry_timestamp
        req_data["test_module"] = ""
        req_data["test_case"] = ""
    req = TestStartRequest(**req_data)
    response = await start_test(request_shim, help=False, req=req)
    payload = _json_response_body(response)
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


async def _run_pre_actions(session: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    actions = plan.get("pre_actions") or []
    if not actions:
        return {"success": True}

    req = plan.get("request") or {}
    devices = req.get("devices") or []
    for action in actions:
        if action.get("type") != "connect_wifi":
            continue
        from core.schemas import WifiConnectRequest
        from routers.devices import connect_wifi

        wifi_req = WifiConnectRequest(
            devices=devices,
            ssid=action.get("ssid") or "AndroidWifi",
            password=action.get("password") or "1234567890",
        )
        response = await connect_wifi(wifi_req)
        payload = _json_response_body(response)
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


async def _execute_plan(session: Dict[str, Any], request: Request, plan: Dict[str, Any]) -> Dict[str, Any]:
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
    await _cleanup_sessions()
    message = (req.message or "").strip()
    if not message:
        return error_response("Message cannot be empty", 400)

    client_id = get_client_id_from_request(request)
    session = await _get_or_create_session(req.session_id, client_id)
    _append_message(session, "user", message)

    # Record user message in context
    from core.agent_context import record_user_message
    record_user_message(session, message)

    # --- 1. Check pending plan confirmation (existing behavior) ---
    pending_plan = session.get("pending_plan")
    wants_execute = req.execute or bool(re.search(r"^(确认执行|开始执行|执行|确认|start|run)$", message, re.IGNORECASE))
    if pending_plan and wants_execute:
        # Check if this is a generic action plan (new) or a test plan (legacy)
        if pending_plan.get("type") == "generic_action":
            intent_data = pending_plan.get("intent")
            from core.agent_executor import executor
            from core.agent_response import generate as gen_response
            from core.agent_context import update_context
            from core.agent_tools import registry

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
    from core.agent_intent import resolve as resolve_intent
    from core.agent_executor import executor
    from core.agent_response import generate as gen_response, generate_error, generate_clarification, generate_capability_overview
    from core.agent_context import update_context
    from core.agent_tools import registry

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
        from core.agent_tools import registry
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

    if intent.tool_name == "greeting":
        _append_message(session, "assistant", "你好，我可以帮你查询设备/报告/套件，也可以按确认流程执行测试、retry 和分析。直接说要做什么就行。", data={"page": "agent"})
        session["status"] = "idle"
        session["pending_plan"] = None
        return success_response({"session": _session_payload(session)}, "Agent updated")

    # --- 2c. Navigate: special handling ---
    if intent.tool_name == "navigate":
        page = intent.params.get("page", "")
        if page:
            from routers.agent import WEBAPP_PAGES
            name, desc = WEBAPP_PAGES.get(page, (page, ""))
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
        result_count=len(tool_result.entities.get(list(tool_result.entities.keys())[0], [])) if tool_result.entities else 0,
        result_summary=tool_result.formatted_text[:200] if tool_result.formatted_text else "",
    )

    # --- 5. Generate and send response ---
    resp = gen_response(tool_result)
    _append_message(session, "assistant", resp.content, kind=resp.kind, data=resp.to_message_data())

    session["status"] = "idle"
    session["pending_plan"] = None
    return success_response({"session": _session_payload(session)}, "Agent updated")


def _format_params(params: Dict[str, Any]) -> str:
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
    from core.agent_tools import registry as tool_registry

    config = config_manager.load_config()
    categories = tool_registry.get_all_categories()
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
        "total_tools": len(tool_registry),
        "limits": {"max_retries": MAX_AGENT_RETRIES},
        "suite_source": "local" if is_config_host_local(config) else "ssh",
    })
