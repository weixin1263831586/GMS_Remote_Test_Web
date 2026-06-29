"""
Agent Intent Router — 多阶段意图路由。

Stage 1: 精确命令匹配
Stage 2: 上下文引用解析
Stage 3: 工具关键词评分
Stage 4: 正则回退
Stage 5: 帮助兜底
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from features.assistant.context import resolve_reference
from features.assistant.tools import AgentTool, registry


# ==================== Result ====================

@dataclass
class ResolvedIntent:
    """意图路由结果。"""
    tool_name: str                      # 匹配的工具名
    tool: AgentTool | None           # 工具对象
    confidence: float                   # 0.0 - 1.0
    params: dict[str, Any]              # 提取的参数
    needs_confirm: bool                 # 是否需要用户确认
    is_run_test: bool                   # 是否是测试启动请求
    context_entities: dict[str, Any]    # 上下文解析出的实体
    stage: str                          # 匹配阶段: exact/context/keyword/regex/fallback

    @staticmethod
    def unknown() -> ResolvedIntent:
        return ResolvedIntent(
            tool_name="", tool=None, confidence=0.0, params={},
            needs_confirm=False, is_run_test=False,
            context_entities={}, stage="fallback",
        )


# ==================== Exact Match Patterns ====================

_EXACT_COMMANDS: list[tuple] = [
    # (pattern, tool_name, extra_params)
    (r"^(停止|终止|结束|取消)(测试|任务)?$", "test_stop", {}),
    (r"^(停止测试|stop\s*test)$", "test_stop", {}),
    (r"^(清理|清除)(测试)?(环境)?$", "test_clean", {}),
    (r"^打开(.+)", "navigate", {}),
    (r"^(去|进入|跳转)(.+)", "navigate", {}),
    (r"^(下载|导出)(报告|最新报告)?$", "reports_download", {}),
    (r"^(删除)(报告|最新报告)?$", "reports_delete", {}),
    (r"^(连接)(VPN|vpn)?$", "vpn_connect", {}),
    (r"^(断开|断开VPN|disconnect)", "vpn_disconnect", {}),
    (r"^(VPN状态|vpn\s*status)", "vpn_status", {}),
    (r"^(系统健康|健康检查|health)", "system_health", {}),
    (r"^(测试状态|运行状态|status)", "test_status", {}),
    (r"^(设备|设备列表|空闲设备|可用设备|占用设备|adb设备)$", "devices_list", {}),
    (r"^(报告|报告列表|最近报告|最新报告)$", "reports_list", {}),
    (r"^(APK任务|apk任务|反编译任务)$", "apk_tasks", {}),
    (r"^(重启设备?)", "devices_reboot", {}),
    (r"^(用户|用户列表|在线用户)", "users_list", {}),
]

# Navigation aliases
_NAV_ALIASES: dict[str, str] = {
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
    "常用网址": "websites", "网址": "websites", "网站": "websites",
    "常用工具": "tools", "工具下载": "tools", "下载工具": "tools",
    "审计": "security-audit", "安全审计": "security-audit",
    "gms助手": "gms-assistant",
    "gms ats": "automation", "ats": "automation", "自动化": "automation", "自动化测试": "automation",
    "gms自动化": "automation", "自动化链路": "automation",
    "redmine": "redmine-agent", "redmine看板": "redmine-agent", "部门看板": "redmine-agent",
    "gerrit": "gerrit-dashboard", "gerrit看板": "gerrit-dashboard", "代码评审": "gerrit-dashboard",
    "agent": "agent", "对话agent": "agent",
}


# ==================== Parameter Extraction ====================

def _extract_test_type(text: str) -> str:
    upper = text.upper()
    for test_type in ["GTS-ROOT", "CTS", "GTS", "STS", "VTS", "APTS", "GSI", "CTS_VERIFIER"]:
        if test_type in upper:
            return test_type
    return ""


def _extract_module_and_case(text: str) -> tuple:
    module, case = "", ""
    m = None
    hash_match = re.search(r"([A-Za-z0-9_./$-]+)#([A-Za-z0-9_./$-]+)", text)
    if hash_match:
        module, case = hash_match.group(1), hash_match.group(2)
    if not module:
        m = re.search(
            r"(?:模块|module|跑|执行|测试)\s*(?:测试)?\s*[:：]?\s*([A-Za-z0-9_.-]*(?:TestCases|Tests|Test|Cases|_test)[A-Za-z0-9_.-]*)",
            text,
            re.IGNORECASE,
        )
        if m:
            module = m.group(1)
    if not module and not m:
        m = re.search(
            r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]*(?:TestCases|Tests|Test|Cases|_test)[A-Za-z0-9_.-]*)(?![A-Za-z0-9_.-])",
            text,
            re.IGNORECASE,
        )
        if m:
            module = m.group(1)
    if not case:
        m = re.search(r"(?:\bcase\b|用例)\s*[:：]?\s*([A-Za-z0-9_.$-]+)", text, re.IGNORECASE)
        if m:
            case = m.group(1)
    return module, case


def _extract_device_ids(text: str) -> list[str]:
    ids = []
    for token in re.findall(r"\b[A-Za-z0-9][A-Za-z0-9_.:-]{5,}\b", text):
        if re.fullmatch(r"RK\d+", token, re.IGNORECASE):
            continue
        if any(skip in token for skip in ("Test", "Cases", "TESTCASES", "Android", "REPORT")):
            continue
        ids.append(token)
    return list(dict.fromkeys(ids))[:8]


def _extract_device_keyword(text: str) -> str:
    """Extract a non-serial device keyword such as rk3572 from natural language."""
    lowered = text.lower()
    match = re.search(r"(?<![a-z0-9_-])(rk\d{3,5}[a-z0-9_-]*|rk\d+[a-z0-9_-]*|[a-z0-9_-]*gms[a-z0-9_-]*)(?![a-z0-9_-])", lowered)
    if match:
        return match.group(1)
    if re.search(r"空闲|可用|闲置", lowered):
        return "available"
    if re.search(r"占用|锁定|被锁", lowered):
        return "locked"
    return ""


def _extract_retry_count(text: str) -> int:
    if not re.search(r"retry|重试|失败.*继续|再跑", text, re.IGNORECASE):
        return 0
    match = re.search(r"(?:retry|重试|继续)\s*(\d+)", text, re.IGNORECASE)
    if not match:
        match = re.search(r"(\d+)\s*次.*(?:retry|重试)", text, re.IGNORECASE)
    if match:
        return min(3, max(0, int(match.group(1))))
    return 1


def _extract_report_timestamp(text: str) -> str:
    # 格式: 2026-06-08_22-53-48 或 20260608_225348
    m = re.search(r"(\d{4}[-_]?\d{2}[-_]?\d{2}[_T]?\d{2}[-_]?\d{2}[-_]?\d{2})", text)
    return m.group(1) if m else ""


def _extract_page_name(text: str) -> str:
    lowered = text.lower()
    for alias, page in _NAV_ALIASES.items():
        if alias.lower() in lowered:
            return page
    return ""


def _extract_redmine_names(text: str) -> list[str]:
    cleaned = re.sub(r"(?i)redmine", " Redmine ", text)
    cleaned = re.sub(r"(帮我|帮忙|请|麻烦|查看|查询|统计|看一下|看下|生成|的|Redmine|信息|统计信息|工单|问题单|情况|一下)", " ", cleaned)
    spaced_names = re.findall(r"(?<![\u4e00-\u9fff])([\u4e00-\u9fff]{1,2}\s+[\u4e00-\u9fff]{1,3})(?![\u4e00-\u9fff])", cleaned)
    cleaned = re.sub(r"[，,、；;和及跟与]+", " ", cleaned)
    names = [name.strip() for name in spaced_names if name.strip()]
    spaced_compacts = {name.replace(" ", "") for name in names}
    for token in cleaned.split():
        token = token.strip()
        if not token:
            continue
        if any(token != compact and token in compact for compact in spaced_compacts):
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{1,63}", token):
            names.append(token)
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]{2,4}(?:\s+[\u4e00-\u9fff]{1,3})?", token):
            names.append(token)
    return list(dict.fromkeys(names))[:8]


def _extract_task_id(text: str) -> str:
    m = re.search(r"task[_-]?id\s*[:：]?\s*([a-f0-9\-]{8,})", text, re.IGNORECASE)
    return m.group(1) if m else ""


def _extract_quoted_value(text: str, labels: list[str]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    m = re.search(rf"(?:{label_pattern})\s*[:：=]?\s*[\"'“”]?([^\"'“”\s，,。;；]+)", text, re.IGNORECASE)
    return m.group(1) if m else ""


def _extract_profile_id(text: str) -> str:
    lowered = text.lower()
    if "系统一部" in text or "system-1" in lowered or "sys-1" in lowered:
        return "system-1"
    if "系统二部" in text or "system-2" in lowered or "sys-2" in lowered:
        return "system-2"
    m = re.search(r"profile[_\s-]?id\s*[:：=]?\s*([a-zA-Z0-9_-]+)", text, re.IGNORECASE)
    return m.group(1) if m else ""


def _extract_query_text(text: str) -> str:
    m = re.search(r"(?:query|查询条件)\s*[:：=]\s*(.+)$", text, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _extract_suite_module_query(text: str) -> str:
    cleaned = re.sub(r"\b(?:CTS|VTS|GTS|STS)\b", " ", text, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"(最新|套件|测试套件|testcases?|相关|测试项|测试模块|模块|用例|有哪些|有那些|列表|列出|查询|查看|显示|包含)",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    tokens = re.findall(r"[A-Za-z0-9_.-]+", cleaned)
    if tokens:
        return max(tokens, key=len)
    return ""


def _extract_suite_types(text: str) -> str:
    found = []
    upper = text.upper()
    for suite_type in ("CTS", "VTS", "GTS", "STS"):
        if suite_type in upper:
            found.append(suite_type.lower())
    return ",".join(found)


def _is_run_test_request(text: str) -> bool:
    lowered = text.lower()
    if re.search(r"\b(run|start|retry)\b|启动|开始|执行|重试|再跑|跑测试|跑\s*[a-z0-9_.-]*_test", lowered):
        return True
    module, case = _extract_module_and_case(text)
    if (module or case) and re.search(r"测试|test|用例", text, re.IGNORECASE):
        return True
    if re.search(r"(^|[，,。;\s])跑\s*[A-Za-z0-9_.-]*(?:TestCases|Tests|Test|Cases|_test)", text, re.IGNORECASE):
        return True
    if re.search(r"帮我跑|帮忙跑|给我跑", text):
        return True
    return False


# ==================== Intent Router ====================

def resolve(message: str, session: dict[str, Any]) -> ResolvedIntent:
    """多阶段意图路由。"""
    text = message.strip()
    lowered = text.lower()

    if re.fullmatch(r"(你好|您好|hi|hello|hey|在吗|哈喽)[！!。.\s]*", lowered):
        return ResolvedIntent(
            tool_name="greeting", tool=None, confidence=1.0,
            params={}, needs_confirm=False, is_run_test=False,
            context_entities={}, stage="exact",
        )

    # --- Stage 0: Capabilities ---
    if re.search(r"你是谁|介绍.*你|自我介绍|你能做什么|能帮.*什么|能干什么|能干嘛|功能|帮助|怎么用|使用方法|全功能|web_app", lowered):
        if re.search(r"每个页面|页面功能|页面.*介绍|功能页面|页面说明|导航说明", lowered):
            return ResolvedIntent(
                tool_name="page_overview", tool=None, confidence=1.0,
                params={}, needs_confirm=False, is_run_test=False,
                context_entities={}, stage="exact",
            )
        tool = registry.get("agent_capabilities")
        return ResolvedIntent(
            tool_name="agent_capabilities", tool=tool, confidence=1.0,
            params={}, needs_confirm=False, is_run_test=False,
            context_entities={}, stage="exact",
        )

    # --- Stage 1: Exact command match ---
    for pattern, tool_name, extra in _EXACT_COMMANDS:
        if re.search(pattern, lowered):
            tool = registry.get(tool_name)
            params = dict(extra)
            if tool_name == "navigate":
                page = _extract_page_name(text)
                if page:
                    params["page"] = page
                else:
                    continue  # "打开" 但没有匹配到页面，跳过
            if tool_name == "devices_reboot":
                params["devices"] = _extract_device_ids(text)
            if tool_name == "devices_list":
                keyword = _extract_device_keyword(text)
                if keyword:
                    params["query"] = keyword
            return ResolvedIntent(
                tool_name=tool_name, tool=tool, confidence=0.95,
                params=params, needs_confirm=(tool.requires_confirm if tool else False),
                is_run_test=False, context_entities={}, stage="exact",
            )

    # --- Stage 2: Context reference resolution ---
    ctx_result = resolve_reference(session, text)
    if ctx_result.get("resolved"):
        hint = ctx_result.get("intent_hint", "")
        entities = ctx_result.get("entities", {})
        hint_tool_map = {
            "device_detail": "devices_info",
            "devices_detail": "devices_info",
            "report_detail": "reports_analyze",
            "reports_detail": "reports_analyze",
            "report_download": "reports_download",
            "report_delete": "reports_delete",
            "suite_detail": "test_suites",
            "suites_detail": "test_suites",
            "apk_detail": "apk_status",
            "tasks_detail": "apk_status",
        }
        tool_name = hint_tool_map.get(hint, "")
        if tool_name:
            tool = registry.get(tool_name)
            params = {}
            if entities.get("devices"):
                params["devices"] = entities["devices"]
            if entities.get("reports"):
                params["report_timestamp"] = entities["reports"][0]
            return ResolvedIntent(
                tool_name=tool_name, tool=tool, confidence=0.8,
                params=params, needs_confirm=(tool.requires_confirm if tool else False),
                is_run_test=False, context_entities=entities, stage="context",
            )

    # --- Stage 3: Keyword scoring ---
    lowered_text = text.lower()
    if ("redmine" in lowered_text or "工单" in text or "问题单" in text) and (
        "部门" in text or "系统一部" in text or "系统二部" in text
    ):
        tool = registry.get("redmine_department_stats")
        if tool:
            return ResolvedIntent(
                tool_name="redmine_department_stats",
                tool=tool,
                confidence=0.9,
                params=_extract_params_for_tool(text, tool),
                needs_confirm=False,
                is_run_test=False,
                context_entities={},
                stage="rule",
            )
    if "gerrit" in lowered_text and ("配置" in text or "设置" in text):
        tool = registry.get("gerrit_dashboard_config")
        if tool:
            return ResolvedIntent(
                tool_name="gerrit_dashboard_config",
                tool=tool,
                confidence=0.9,
                params=_extract_params_for_tool(text, tool),
                needs_confirm=False,
                is_run_test=False,
                context_entities={},
                stage="rule",
            )

    if re.search(r"testcases?|测试项|测试模块|模块", lowered_text) and re.search(r"哪些|列表|列出|查询|查看|相关|module", lowered_text):
        tool = registry.get("suite_modules")
        if tool:
            params = {"query": _extract_suite_module_query(text)}
            suite_types = _extract_suite_types(text)
            if suite_types:
                params["suite_types"] = suite_types
            return ResolvedIntent(
                tool_name="suite_modules",
                tool=tool,
                confidence=0.9,
                params=params,
                needs_confirm=False,
                is_run_test=False,
                context_entities={},
                stage="rule",
            )

    scored = registry.find(text, top_k=5, min_score=1.0)
    if scored:
        # Pick best non-test_start result first (test_start is handled separately)
        best_non_test = next((s for s in scored if s.tool.name != "test_start"), None)
        best = best_non_test or scored[0]

        # But if user explicitly wants to run a test, use test_start
        if _is_run_test_request(text):
            return _resolve_run_test(text, session)

        params = _extract_params_for_tool(text, best.tool)

        confidence = min(1.0, best.score / 10.0)
        return ResolvedIntent(
            tool_name=best.tool.name, tool=best.tool,
            confidence=max(confidence, 0.5),
            params=params,
            needs_confirm=best.tool.requires_confirm,
            is_run_test=False,
            context_entities={},
            stage="keyword",
        )

    # --- Stage 4: Regex fallback ---
    fallback = _legacy_intent_detect(text)
    if fallback:
        tool = registry.get(fallback)
        if tool:
            params = _extract_params_for_tool(text, tool)
            return ResolvedIntent(
                tool_name=fallback, tool=tool, confidence=0.6,
                params=params, needs_confirm=tool.requires_confirm,
                is_run_test=False, context_entities={}, stage="regex",
            )

    # --- Stage 5: Run test detection ---
    if _is_run_test_request(text):
        return _resolve_run_test(text, session)

    # --- Stage 6: Fallback ---
    return ResolvedIntent.unknown()


# ==================== Run Test Resolution ====================

def _resolve_run_test(text: str, session: dict[str, Any]) -> ResolvedIntent:
    """解析测试启动请求为特殊 intent。"""
    test_type = _extract_test_type(text)
    module, case = _extract_module_and_case(text)
    module_case_tokens = set(
        re.findall(r"[A-Za-z0-9_.:-]{2,}", f"{module} {case}".replace("/", " "))
    )
    device_ids = [
        device_id for device_id in _extract_device_ids(text)
        if device_id not in module_case_tokens
    ]
    retry_count = _extract_retry_count(text)
    analyze_on_failure = bool(re.search(r"报告分析|分析报告|失败|fail|analy", text.lower()))
    apk_source = bool(re.search(r"apk|反编译|源码|source", text.lower()))
    connect_wifi = bool(re.search(r"wifi|wi-fi|无线网络|连接网络", text.lower()))

    tool = registry.get("test_start")
    return ResolvedIntent(
        tool_name="test_start",
        tool=tool,
        confidence=0.9,
        params={
            "test_type": test_type,
            "test_module": module,
            "test_case": case,
            "devices": device_ids,
            "retry_count": retry_count,
            "analyze_on_failure": analyze_on_failure,
            "apk_source_analysis": apk_source,
            "connect_wifi": connect_wifi,
        },
        needs_confirm=True,
        is_run_test=True,
        context_entities={},
        stage="exact",
    )


# ==================== Helpers ====================

def _extract_params_for_tool(text: str, tool: AgentTool) -> dict[str, Any]:
    """根据工具参数定义从文本中提取参数。"""
    params: dict[str, Any] = {}
    if not tool:
        return params

    for pdef in tool.params:
        pname = pdef.get("name", "")
        if pname == "devices":
            ids = _extract_device_ids(text)
            if ids:
                params["devices"] = ids
        elif pname in ("report_timestamp", "timestamp"):
            ts = _extract_report_timestamp(text)
            if ts:
                params["report_timestamp"] = ts
        elif pname in ("page", "target_page"):
            page = _extract_page_name(text)
            if page:
                params["page"] = page
        elif pname == "task_id":
            tid = _extract_task_id(text)
            if tid:
                params["task_id"] = tid
        elif pname in ("ssid", "vpn_name", "username", "ip", "device_host", "url", "path", "archive_path", "suite_path", "sn_code", "serial_no"):
            value = _extract_quoted_value(text, [pname, pdef.get("desc", ""), "名称", "地址", "路径", "序列号"])
            if value:
                params[pname] = value
        elif pname == "password":
            value = _extract_quoted_value(text, ["password", "密码"])
            if value:
                params[pname] = value
        elif pname == "command":
            value = _extract_quoted_value(text, ["command", "命令", "shell"])
            if value:
                params[pname] = value
        elif pname == "names":
            extracted = _extract_redmine_names(text)
            if extracted:
                params["names"] = extracted
        elif pname == "profile_id":
            profile_id = _extract_profile_id(text)
            if profile_id:
                params["profile_id"] = profile_id
        elif pname == "query":
            query = _extract_query_text(text)
            if not query and tool.name == "suite_modules":
                query = _extract_suite_module_query(text)
            if query:
                params["query"] = query
        elif pname == "suite_types":
            suite_types = _extract_suite_types(text)
            if suite_types:
                params["suite_types"] = suite_types

    if tool.name in {"devices_list", "devices_management"}:
        keyword = _extract_device_keyword(text)
        if keyword:
            params["query"] = keyword

    return params


def _legacy_intent_detect(text: str) -> str:
    """保留旧版正则意图检测作为回退。"""
    lowered = text.lower().strip()
    query_words = r"多少|几个|有哪些|列表|列出|查看|查询|显示|状态|统计|最近|当前"

    if re.search(r"测试状态|运行状态|正在跑|是否.*运行|status", lowered):
        return "test_status"
    if re.search(r"系统健康|健康检查|服务状态|health", lowered):
        return "system_health"
    if re.search(r"测试套件|suite", lowered):
        return "test_suites"
    if re.search(r"设备|adb|device|rk\d{3,5}", lowered) and re.search(query_words + r"|空闲|可用|占用|锁定|rk\d{3,5}", lowered):
        return "devices_list"
    if re.search(r"报告|report", lowered):
        return "reports_list"
    if re.search(r"apk|反编译|jadx", lowered):
        return "apk_tasks"
    if re.search(r"用户|在线|user", lowered):
        return "users_list"
    if re.search(r"配置|config", lowered):
        return "config_read"
    if re.search(r"vpn", lowered):
        return "vpn_status"
    if re.search(r"烧写|烧录|burn|flash|固件|刷机", lowered):
        return "burn_firmware"
    if re.search(r"桌面|vnc|desktop", lowered):
        return "desktop_vnc_status"
    if re.search(r"终端|terminal|ssh", lowered):
        return "terminal_open"
    return ""
