"""
Agent Tool Registry — 注册所有项目 API 为可调用的 Agent 工具。

从 core/api_docs_list.API_DOCS_LIST 种子数据构建工具目录，
附加关键词索引用于意图匹配。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# ==================== Data Structures ====================

@dataclass
class AgentTool:
    """一个可被 Agent 调用的工具（映射到一个 API 端点）。"""
    name: str                       # snake_case 工具名，如 "devices_list"
    category: str                   # 分类: device, test, report, system, ...
    description: str                # 中文描述
    api_path: str                   # "/api/devices/list"
    method: str                     # GET / POST / DELETE / WebSocket
    params: List[Dict[str, Any]]    # 参数定义
    keywords: List[str]             # 匹配关键词 (中英文)
    is_readonly: bool               # True=查询类，无副作用
    is_dangerous: bool              # True=烧写/删除等破坏性操作
    requires_confirm: bool          # True=执行前需确认
    executor_ref: str               # "routers.devices:function_name"
    response_type: str              # list / detail / status / file / stream

    @property
    def display_name(self) -> str:
        return self.description.split("（")[0].split("(")[0].strip()


@dataclass
class ScoredTool:
    """带分数的工具匹配结果。"""
    tool: AgentTool
    score: float
    matched_keywords: List[str]


# ==================== Keyword Supplements ====================
# api_docs_list 只有 description，这里按 category 补充高频关键词

_CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "device": ["设备", "device", "adb", "设备列表", "设备管理", "设备信息", "连接设备",
               "手机", "安卓设备", "device list", "device info"],
    "test": ["测试", "test", "跑测试", "启动测试", "运行测试", "CTS", "VTS", "GTS", "STS", "GSI",
             "APTS", "测试套件", "测试模块", "测试用例", "test case", "test module",
             "开始测试", "停止测试", "测试状态", "测试日志", "tradefed", "retry", "重试"],
    "report": ["报告", "report", "测试报告", "报告分析", "报告下载", "报告删除",
               "分析报告", "诊断", "diagnosis", "失败分析", "failure"],
    "desktop": ["桌面", "desktop", "VNC", "vnc", "远程桌面", "noVNC", "桌面控制"],
    "terminal": ["终端", "terminal", "SSH", "ssh", "命令行", "shell", "上传文件", "file upload"],
    "users": ["用户", "user", "在线用户", "用户列表", "用户管理", "用户名", "username"],
    "config": ["配置", "config", "设置", "系统配置", "configuration"],
    "system": ["系统", "system", "健康", "health", "API", "接口", "文档", "docs", "help",
               "帮助", "技能", "skills", "websocket"],
    "ssh": ["SSH", "ssh", "SSH服务", "sshd", "ping", "连通性", "路由", "route"],
    "vpn": ["VPN", "vpn", "虚拟专用网", "VPN连接", "VPN状态"],
    "usbip": ["USB", "usbip", "USB共享", "ADB转发", "adb-forward", "端口转发"],
    "file": ["文件", "file", "上传", "upload", "源码搜索", "opengrok", "搜索代码"],
    "burn": ["烧写", "burn", "flash", "固件", "firmware", "GSI", "镜像", "刷机",
             "序列号", "serial", "烧录"],
    "apk": ["APK", "apk", "反编译", "APK反编译", "反编译APK", "jadx", "源码分析", "decompile",
            "manifest", "权限", "源码查看", "android package"],
    "notification": ["通知", "notification", "消息", "提醒"],
    "audit": ["审计", "audit", "安全审计", "访问日志"],
    "assets": ["工具", "tools", "网址", "favicon", "图标"],
    "agent": ["agent", "对话", "会话", "session", "能力", "capabilities"],
    "redmine": ["Redmine", "redmine", "工单", "问题单", "统计", "待回复"],
    "gerrit": ["Gerrit", "gerrit", "代码评审", "变更", "change", "review", "提交"],
}

# 按 API path 模式补充额外关键词
_PATH_KEYWORDS: Dict[str, List[str]] = {
    "/api/devices/bootloader": ["bootloader", "锁定", "解锁", "lock", "unlock"],
    "/api/devices/reboot": ["重启", "reboot", "重启设备"],
    "/api/devices/remount": ["remount", "挂载", "读写"],
    "/api/devices/wifi": ["wifi", "WiFi", "无线网络", "网络连接"],
    "/api/devices/shell": ["shell", "命令", "adb shell", "执行命令"],
    "/api/devices/scrcpy": ["scrcpy", "屏幕", "投屏", "screen", "镜像"],
    "/api/test/start": ["启动测试", "开始测试", "跑", "执行测试", "run test", "start test"],
    "/api/test/stop": ["停止测试", "终止测试", "stop test", "取消测试"],
    "/api/test/suites": ["测试套件", "套件", "可用套件", "suite list", "test suite"],
    "/api/test/status": ["测试状态", "运行状态", "测试进度", "test status"],
    "/api/reports/download": ["下载报告", "download report", "报告文件"],
    "/api/reports/delete": ["删除报告", "delete report", "移除报告"],
    "/api/desktop/vnc": ["VNC", "vnc", "桌面", "远程"],
    "/api/burn/firmware": ["烧写固件", "firmware burn", "刷固件"],
    "/api/burn/gsi": ["烧写GSI", "GSI burn", "刷GSI", "刷系统"],
    "/api/burn/serial": ["烧写序列号", "serial number", "改序列号"],
    "/api/vpn/connect": ["连接VPN", "VPN连接", "vpn connect"],
    "/api/vpn/disconnect": ["断开VPN", "VPN断开", "vpn disconnect"],
    "/api/vpn/status": ["VPN状态", "VPN连接状态"],
    "/api/usbip/connect": ["USB共享", "USB连接", "usbip connect"],
    "/api/usbip/disconnect": ["USB断开", "usbip disconnect"],
    "/api/adb-forward": ["ADB转发", "端口转发", "adb forward"],
    "/api/opengrok/search": ["源码搜索", "代码搜索", "opengrok"],
    "/api/tailscale/status": ["tailscale", "Tailscale状态", "tailscale status", "Tailscale"],
    "/api/knowledgebase/search": ["知识库", "知识库搜索", "kb search", "knowledge base"],
    "/api/knowledgebase/stats": ["知识库统计", "kb stats"],
    "/api/security-audit/logs": ["审计日志", "访问日志", "audit logs", "安全日志"],
    "/api/notifications": ["通知", "消息", "未读通知", "notifications"],
}

# 按路径标记只读/危险/需确认
_READONLY_PATHS = {
    "GET": True, "WebSocket": True,
}
_DANGEROUS_PATHS = {
    "/api/burn/firmware", "/api/burn/gsi", "/api/burn/serial",
    "/api/reports/delete",
    "/api/devices/bootloader-lock", "/api/devices/bootloader-unlock",
    "/api/config/update",
}
_CONFIRM_PATHS = {
    "/api/test/start", "/api/test/stop", "/api/test/clean",
    "/api/burn/firmware", "/api/burn/gsi", "/api/burn/serial",
    "/api/devices/reboot", "/api/devices/remount",
    "/api/devices/bootloader-lock", "/api/devices/bootloader-unlock",
    "/api/devices/wifi",
    "/api/vpn/connect", "/api/vpn/disconnect",
    "/api/reports/delete", "/api/config/update",
    "/api/usbip/connect", "/api/usbip/disconnect",
    "/api/usbip/install",
}

# category → routers 模块名映射（当两者不一致时需要）
_CATEGORY_MODULE_MAP: Dict[str, str] = {
    "device": "devices",      # category="device" → routers.devices
    "test": "tests",          # category="test" → routers.tests
    "report": "reports",      # category="report" → routers.reports
    "burn": "firmware",       # category="burn" → routers.firmware
    "ssh": "integrations",    # category="ssh" → routers.integrations
    "vpn": "integrations",    # category="vpn" → routers.integrations
    "usbip": "integrations",  # category="usbip" → routers.integrations
    "file": "system",         # category="file" → routers.system
    "notification": "notifications",  # category="notification" → routers.notifications
}

# 响应类型推断
_RESPONSE_TYPE_MAP = {
    "/api/devices/list": "list",
    "/api/devices/management": "list",
    "/api/devices/user-locked": "list",
    "/api/devices/info": "detail",
    "/api/devices/bootloader-status": "status",
    "/api/test/suites": "list",
    "/api/test/status": "status",
    "/api/test/logs/stream": "stream",
    "/api/reports/list": "list",
    "/api/reports/download": "file",
    "/api/system/health": "status",
    "/api/users/list": "list",
    "/api/vpn/status": "status",
    "/api/usbip/status": "status",
    "/api/desktop/vnc/status": "status",
    "/api/files/list": "list",
}

_EXECUTOR_REF_OVERRIDES: Dict[str, str] = {
    "/api/system/health": "routers.system:health_check",
    "/api/config/read": "routers.config:get_config",
    "/api/config/update": "routers.config:update_config",
    "/api/users/current": "routers.users:get_client_info",
    "/api/users/detect": "routers.users:detect_client",
    "/api/users/set-username": "routers.users:set_client_username",
    "/api/users/list": "routers.users:list_users",
    "/api/devices/list": "routers.devices:get_connected_devices",
    "/api/devices/management": "routers.devices:devices_management",
    "/api/devices/user-locked": "routers.devices:list_user_locks",
    "/api/devices/bootloader-lock": "routers.devices:lock_bootloader",
    "/api/devices/bootloader-unlock": "routers.devices:unlock_bootloader",
    "/api/devices/bootloader-status": "routers.devices:check_bootloader_status",
    "/api/devices/info": "routers.devices:get_device_info",
    "/api/devices/reboot": "routers.devices:reboot_devices",
    "/api/devices/remount": "routers.devices:remount_devices",
    "/api/devices/wifi": "routers.devices:connect_wifi",
    "/api/devices/shell": "routers.devices:open_device_shell",
    "/api/devices/scrcpy": "routers.devices:show_device_screens",
    "/api/test/start": "routers.tests:start_test",
    "/api/test/stop": "routers.tests:stop_test",
    "/api/test/clean": "routers.tests:clean_test_logs",
    "/api/test/suites": "routers.tests:list_suites",
    "/api/test/suites/result": "routers.tests:list_tradefed_results",
    "/api/test/status": "routers.tests:get_status",
    "/api/test/logs/save": "routers.tests:save_current_log",
    "/api/reports/list": "routers.reports:list_reports",
    "/api/reports/analyze": "routers.reports:analyze_reports",
    "/api/reports/download": "routers.reports:download_report",
    "/api/reports/delete": "routers.reports:delete_report",
    "/api/desktop/vnc/status": "routers.desktop:get_desktop_vnc_status",
    "/api/desktop/vnc/start": "routers.desktop:start_desktop_vnc",
    "/api/desktop/vnc/stop": "routers.desktop:stop_desktop_vnc",
    "/api/desktop/validate": "routers.desktop:validate_desktop_host",
    "/api/ssh/sshd": "routers.integrations:check_ssh_sshd",
    "/api/ssh/ping": "routers.integrations:ping_route_test",
    "/api/ssh/route": "routers.integrations:check_ssh_route",
    "/api/vpn/status": "routers.integrations:get_vpn_status",
    "/api/vpn/connect": "routers.integrations:connect_vpn",
    "/api/vpn/disconnect": "routers.integrations:disconnect_vpn",
    "/api/adb-forward/start": "routers.integrations:start_adb_forward",
    "/api/adb-forward/stop": "routers.integrations:stop_adb_forward",
    "/api/usbip/status": "routers.integrations:get_usbip_status",
    "/api/usbip/connect": "routers.integrations:start_usbip",
    "/api/usbip/disconnect": "routers.integrations:stop_usbip",
    "/api/usbip/install": "routers.integrations:install_usbipd",
    "/api/files/progress": "routers.assets:get_upload_progress",
    "/api/files/list": "routers.assets:list_files",
    "/api/opengrok/search": "routers.assets:search_opengrok",
    "/api/burn/upload-progress": "routers.firmware:get_firmware_upload_progress",
    "/api/burn/firmware": "routers.firmware:burn_firmware",
    "/api/burn/gsi": "routers.firmware:burn_gsi",
    "/api/burn/serial": "routers.firmware:burn_sn",
    "/api/terminal/open": "routers.terminal:get_ssh_terminal_info",
    "/api/terminal/push": "routers.terminal:upload_file",
    "/api/system/skills": "routers.system:download_skills_zip",
    "/api/system/docs": "routers.system:get_api_docs",
    "/api/system/help": "routers.system:get_api_help",
    # --- 补充：报告 ---
    "/api/reports/diagnose": "routers.reports:diagnose_report_failure",
    "/api/reports/analyze-url": "routers.reports:analyze_report_from_url",
    "/api/reports/extract-redmine-attachment": "routers.reports:extract_redmine_attachment",
    # --- 补充：知识库 ---
    "/api/knowledgebase/search": "routers.reports:knowledgebase_search",
    "/api/knowledgebase/stats": "routers.reports:knowledgebase_stats",
    # --- 补充：测试日志/套件 ---
    "/api/test/logs/list": "routers.tests:list_test_logs",
    "/api/test/logs/get": "routers.tests:get_test_logs",
    "/api/test/logs/batch": "routers.tests:download_test_logs",
    "/api/test/suites/archives": "routers.tests:list_test_suite_archives",
    "/api/test/suites/diagnose-target": "routers.tests:diagnose_suite_target",
    # --- 补充：通知 ---
    "/api/notifications": "routers.notifications:get_notifications",
    "/api/notifications/mark-read": "routers.notifications:mark_notifications_read",
    "/api/notifications/clear": "routers.notifications:clear_notifications",
    # --- 补充：安全审计 ---
    "/api/security-audit/logs": "routers.audit:list_security_audit_logs",
    "/api/security-audit/export": "routers.audit:export_security_audit_logs",
    # --- 补充：网址/工具 ---
    "/api/websites/load": "routers.assets:load_user_tools",
    "/api/tools/list": "routers.assets:list_utility_tools",
    # --- 补充：配置 ---
    "/api/config/ai": "routers.config:get_ai_config",
    "/api/config/opengrok": "routers.config:get_opengrok_config",
    "/api/config/redmine": "routers.reports:get_redmine_config",
    "/api/tailscale/status": "routers.config:get_tailscale_status",
    # --- 补充：APK（路径参数 task_id 作为函数参数） ---
    "/api/apk/status/{task_id}": "routers.apk:get_apk_status",
    "/api/apk/manifest/{task_id}": "routers.apk:get_apk_manifest",
    "/api/apk/permissions/{task_id}": "routers.apk:get_apk_permissions",
    "/api/apk/source/{task_id}": "routers.apk:get_apk_source",
    "/api/apk/search/{task_id}": "routers.apk:search_apk_source_files",
    "/api/apk/definition/{task_id}": "routers.apk:find_apk_symbol_definition",
    "/api/apk/analyze/{task_id}": "routers.apk:analyze_apk",
    # --- 补充：测试套件 ---
    "/api/test/parse-args": "routers.tests:parse_test_args",
    "/api/test/suites/files": "routers.tests:list_suite_files",
    "/api/test/suites/download": "routers.tests:download_suite_file",
    "/api/test/suites/extract": "routers.tests:extract_test_suite_archive",
    "/api/test/suites/download-url": "routers.tests:download_test_suite_from_url",
    "/api/test/suites/extract-start": "routers.tests:start_test_suite_extract",
    "/api/test/suites/add-local": "routers.tests:add_local_test_suite",
    "/api/test/suites/download-status/{task_id}": "routers.tests:get_test_suite_download_status",
    "/api/test/suites/extract-status/{task_id}": "routers.tests:get_test_suite_extract_status",
    # --- 补充：安全审计详情 ---
    "/api/security-audit/detail/{event_id}": "routers.audit:get_security_audit_detail",
    # --- 补充：VPN 连接 ---
    "/api/vpn/connections": "routers.integrations:get_vpn_connections",
}

_AGENT_UNSUPPORTED_DIRECT_PATHS = {
    "/",
    "/api/test/logs/stream",
    "/api/system/websocket/{client_id}",
    "/api/terminal/push",
    "/api/apk/upload",
    "/api/favicon/fetch",
    "/api/favicon/proxy",
    "/api/tools/download/{file_path:path}",
}


# ==================== Tool Registry ====================

class ToolRegistry:
    """Agent 工具注册表。"""

    def __init__(self) -> None:
        self._tools: Dict[str, AgentTool] = {}
        self._keyword_index: Dict[str, List[str]] = {}  # keyword -> [tool_name, ...]
        self._category_index: Dict[str, List[str]] = {}  # category -> [tool_name, ...]
        self._path_index: Dict[str, str] = {}            # api_path -> tool_name

    # ---------- Registration ----------

    def register(self, tool: AgentTool) -> None:
        self._tools[tool.name] = tool
        self._path_index[tool.api_path] = tool.name
        self._category_index.setdefault(tool.category, []).append(tool.name)
        for kw in tool.keywords:
            kw_lower = kw.lower()
            self._keyword_index.setdefault(kw_lower, []).append(tool.name)

    def register_from_api_docs(self, api_docs: List[Dict[str, Any]]) -> None:
        """从 API_DOCS_LIST 批量注册工具。"""
        for entry in api_docs:
            path = entry.get("path", "")
            # 跳过非 API 路径
            if not path.startswith("/api/") and path != "/":
                continue

            method = entry.get("method", "GET")
            category = entry.get("category", "other")
            description = entry.get("description", "")
            params = entry.get("params", [])

            # 生成工具名: /api/devices/list -> devices_list
            name = self._path_to_name(path)
            if not name:
                continue

            # 收集关键词：仅从 description 和精确路径匹配，避免跨工具关键词污染
            keywords = []
            # 从 description 提取中文词（最精准）
            keywords.extend(self._extract_desc_keywords(description))
            # 精确路径关键词（只匹配完整路径前缀）
            for path_prefix, extra_kws in _PATH_KEYWORDS.items():
                if path == path_prefix or path.startswith(path_prefix + "/"):
                    keywords.extend(extra_kws)
            # 补充分类关键词（仅取最短的通用词）
            cat_kws = _CATEGORY_KEYWORDS.get(category, [])
            # 对大类（test, device 等）只加分类名本身，不加所有子工具关键词
            if len(cat_kws) > 5:
                keywords.extend(cat_kws[:2])  # 只加前2个（通常是中英文分类名）
            else:
                keywords.extend(cat_kws)
            # 去重
            keywords = list(dict.fromkeys(keywords))

            # 判断属性
            is_readonly = _READONLY_PATHS.get(method, method == "GET")
            is_dangerous = path in _DANGEROUS_PATHS
            requires_confirm = path in _CONFIRM_PATHS
            response_type = _RESPONSE_TYPE_MAP.get(path, "detail")

            # executor_ref: 目前大部分通过通用查询函数处理
            # category 到模块名的映射（有些 category 与模块名不一致）
            module_name = _CATEGORY_MODULE_MAP.get(category, category)
            executor_ref = _EXECUTOR_REF_OVERRIDES.get(path, "")
            if path not in _AGENT_UNSUPPORTED_DIRECT_PATHS and not executor_ref:
                executor_ref = f"routers.{module_name}:{name}" if category != "other" else ""

            tool = AgentTool(
                name=name,
                category=category,
                description=description,
                api_path=path,
                method=method,
                params=params,
                keywords=keywords,
                is_readonly=is_readonly,
                is_dangerous=is_dangerous,
                requires_confirm=requires_confirm,
                executor_ref=executor_ref,
                response_type=response_type,
            )
            self.register(tool)

    # ---------- Lookup ----------

    def get(self, name: str) -> Optional[AgentTool]:
        return self._tools.get(name)

    def get_by_path(self, api_path: str) -> Optional[AgentTool]:
        name = self._path_index.get(api_path)
        return self._tools.get(name) if name else None

    def get_by_category(self, category: str) -> List[AgentTool]:
        names = self._category_index.get(category, [])
        return [self._tools[n] for n in names if n in self._tools]

    def get_all_categories(self) -> Dict[str, List[AgentTool]]:
        return {cat: self.get_by_category(cat) for cat in sorted(self._category_index)}

    def get_all_tools(self) -> List[AgentTool]:
        return list(self._tools.values())

    def find(self, text: str, top_k: int = 5, min_score: float = 0.1) -> List[ScoredTool]:
        """关键词匹配评分，返回按分数排序的工具列表。"""
        lowered = text.lower().strip()
        scores: Dict[str, float] = {}
        matched: Dict[str, List[str]] = {}

        # 对每个关键词做匹配
        for keyword, tool_names in self._keyword_index.items():
            if keyword in lowered:
                # 长关键词权重更高
                weight = 1.0 + len(keyword) * 0.3
                # 精确短语匹配额外加分
                if keyword == lowered:
                    weight *= 2.0
                for tn in tool_names:
                    scores[tn] = scores.get(tn, 0.0) + weight
                    matched.setdefault(tn, []).append(keyword)

        # 排序
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        results: List[ScoredTool] = []
        for tn, score in ranked[:top_k]:
            tool = self._tools.get(tn)
            if tool and score >= min_score:
                results.append(ScoredTool(
                    tool=tool,
                    score=round(score, 2),
                    matched_keywords=matched.get(tn, []),
                ))
        return results

    # ---------- Helpers ----------

    @staticmethod
    def _path_to_name(path: str) -> str:
        """'/api/devices/list' -> 'devices_list', '/' -> 'home'"""
        if path == "/":
            return "home"
        # 去掉 /api/ 前缀，将 / 替换为 _
        stripped = path
        for prefix in ("/api/", "/"):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):]
        name = stripped.replace("/", "_").replace("-", "_")
        # 清理
        name = re.sub(r"_+", "_", name).strip("_")
        return name if name else ""

    @staticmethod
    def _extract_desc_keywords(description: str) -> List[str]:
        """从 description 提取中文关键词（简单分词：按标点和括号分割）。"""
        # 去除括号内容
        clean = re.sub(r"[（(].*?[）)]", "", description)
        # 按常见分隔符分割
        parts = re.split(r"[，,。.、：:；;！!？?\s]+", clean)
        # 保留 2-8 字的中文片段和英文单词
        keywords = []
        for part in parts:
            part = part.strip()
            if 2 <= len(part) <= 8 and (re.search(r"[一-鿿]", part) or part.isascii()):
                keywords.append(part)
        return keywords

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:
        return f"ToolRegistry({len(self._tools)} tools, {len(self._category_index)} categories)"


# ==================== Global Instance ====================

def _build_registry() -> ToolRegistry:
    """构建并返回全局工具注册表。"""
    from core.api_docs_list import API_DOCS_LIST

    registry = ToolRegistry()
    registry.register_from_api_docs(API_DOCS_LIST)

    # 手动补充一些 API_DOCS_LIST 没有覆盖的重要工具
    _register_extra_tools(registry)
    return registry


def _register_extra_tools(registry: ToolRegistry) -> None:
    """注册 API_DOCS_LIST 未覆盖但 Agent 需要的工具。"""
    # Helper: merge tool-specific keywords with category keywords
    def _kw(category: str, *extra: str) -> List[str]:
        kws = list(_CATEGORY_KEYWORDS.get(category, []))
        kws.extend(extra)
        return list(dict.fromkeys(kws))

    extras = [
        AgentTool(
            name="redmine_workload_stats",
            category="redmine",
            description="统计指定人员的 Redmine 工作量",
            api_path="/api/redmine-agent/statistics/workload/by-users",
            method="GET",
            params=[
                {"name": "names", "type": "array", "required": False, "desc": "人员姓名列表"},
                {"name": "stale_days", "type": "integer", "required": False, "desc": "未回复天数阈值"},
            ],
            keywords=["Redmine统计", "redmine统计", "Redmine信息", "redmine信息", "Redmine问题",
                      "Redmine工单", "问题单统计", "工单统计", "待回复问题", "缺失测试报告",
                      "Redmine", "redmine", "工单", "问题单"],
            is_readonly=True,
            is_dangerous=False,
            requires_confirm=False,
            executor_ref="",
            response_type="list",
        ),
        AgentTool(
            name="redmine_department_stats",
            category="redmine",
            description="统计 Redmine 部门看板超阈值未回复问题",
            api_path="/api/redmine-agent/statistics/department-overdue",
            method="GET",
            params=[
                {"name": "profile_id", "type": "string", "required": False, "desc": "部门看板 profile id"},
                {"name": "stale_days", "type": "integer", "required": False, "desc": "未回复天数阈值"},
            ],
            keywords=["部门Redmine", "部门看板", "Redmine部门统计", "系统一部", "系统二部", "超阈值未回复"],
            is_readonly=True,
            is_dangerous=False,
            requires_confirm=False,
            executor_ref="",
            response_type="list",
        ),
        AgentTool(
            name="redmine_stats_config",
            category="redmine",
            description="查看 Redmine 统计设置和看板配置",
            api_path="/api/redmine-agent/config/stats",
            method="GET",
            params=[],
            keywords=["Redmine设置", "统计设置", "stale_days", "未回复阈值", "Redmine配置"],
            is_readonly=True,
            is_dangerous=False,
            requires_confirm=False,
            executor_ref="",
            response_type="status",
        ),
        AgentTool(
            name="gerrit_dashboard_config",
            category="gerrit",
            description="查看 Gerrit 看板配置",
            api_path="/api/gerrit-dashboard/config",
            method="GET",
            params=[],
            keywords=["Gerrit配置", "Gerrit看板", "gerrit dashboard", "gerrit设置"],
            is_readonly=True,
            is_dangerous=False,
            requires_confirm=False,
            executor_ref="",
            response_type="status",
        ),
        AgentTool(
            name="gerrit_dashboard_changes",
            category="gerrit",
            description="查询 Gerrit 看板变更",
            api_path="/api/gerrit-dashboard/changes",
            method="GET",
            params=[
                {"name": "profile_id", "type": "string", "required": False, "desc": "Gerrit profile id"},
                {"name": "query", "type": "string", "required": False, "desc": "Gerrit query"},
            ],
            keywords=["Gerrit变更", "Gerrit查询", "代码评审", "open changes", "merged changes", "gerrit query"],
            is_readonly=True,
            is_dangerous=False,
            requires_confirm=False,
            executor_ref="",
            response_type="list",
        ),
        AgentTool(
            name="reports_diagnose",
            category="report",
            description="诊断报告中的失败用例",
            api_path="/api/reports/diagnose",
            method="POST",
            params=[
                {"name": "test_name", "type": "string", "required": True, "desc": "测试名"},
                {"name": "error_message", "type": "string", "required": False, "desc": "错误信息"},
                {"name": "stack_trace", "type": "string", "required": False, "desc": "堆栈"},
                {"name": "report_name", "type": "string", "required": False, "desc": "报告名"},
            ],
            keywords=["诊断", "diagnose", "失败诊断", "报错诊断", "root cause", "根因分析",
                       "错误分析", "失败原因", "failure diagnosis"],
            is_readonly=True,
            is_dangerous=False,
            requires_confirm=False,
            executor_ref="routers.reports:diagnose_report_failure",
            response_type="detail",
        ),
        AgentTool(
            name="apk_upload",
            category="apk",
            description="上传 APK/JAR 文件进行反编译分析",
            api_path="/api/apk/upload",
            method="POST",
            params=[{"name": "file", "type": "file", "required": True, "desc": "APK 文件"}],
            keywords=_kw("apk", "上传APK", "APK上传", "analyze apk", "upload apk"),
            is_readonly=False,
            is_dangerous=False,
            requires_confirm=False,
            executor_ref="routers.apk:upload_apk",
            response_type="detail",
        ),
        AgentTool(
            name="apk_tasks",
            category="apk",
            description="列出所有 APK 反编译任务",
            api_path="/api/apk/tasks",
            method="GET",
            params=[],
            keywords=_kw("apk", "APK任务", "APK列表", "apk tasks", "任务列表"),
            is_readonly=True,
            is_dangerous=False,
            requires_confirm=False,
            executor_ref="routers.apk:list_apk_tasks",
            response_type="list",
        ),
        AgentTool(
            name="apk_status",
            category="apk",
            description="查询 APK 反编译任务状态",
            api_path="/api/apk/status",
            method="GET",
            params=[{"name": "task_id", "type": "string", "required": True, "desc": "任务 ID"}],
            keywords=_kw("apk", "APK状态", "apk status", "任务状态"),
            is_readonly=True,
            is_dangerous=False,
            requires_confirm=False,
            executor_ref="routers.apk:get_apk_status",
            response_type="status",
        ),
        AgentTool(
            name="agent_capabilities",
            category="agent",
            description="查看 Agent 能力列表",
            api_path="/api/agent/capabilities",
            method="GET",
            params=[],
            keywords=["你能做什么", "能干什么", "功能", "帮助", "怎么用", "使用方法",
                       "capabilities", "全功能", "agent功能", "帮助"],
            is_readonly=True,
            is_dangerous=False,
            requires_confirm=False,
            executor_ref="",
            response_type="detail",
        ),
        AgentTool(
            name="suites_download",
            category="test",
            description="下载测试套件文件",
            api_path="/api/test/suites/download",
            method="GET",
            params=[{"name": "path", "type": "string", "required": True, "desc": "套件文件路径"}],
            keywords=["下载套件", "下载测试套件", "download suite", "套件下载"],
            is_readonly=False,
            is_dangerous=False,
            requires_confirm=True,
            executor_ref="routers.tests:download_suite_file",
            response_type="file",
        ),
        AgentTool(
            name="suites_extract",
            category="test",
            description="解压测试套件",
            api_path="/api/test/suites/extract",
            method="POST",
            params=[{"name": "path", "type": "string", "required": True, "desc": "套件归档路径"}],
            keywords=["解压套件", "extract suite", "解压", "解包"],
            is_readonly=False,
            is_dangerous=False,
            requires_confirm=False,
            executor_ref="routers.tests:extract_test_suite_archive",
            response_type="status",
        ),
        AgentTool(
            name="suites_apk_analyze",
            category="test",
            description="反编译套件中的 APK/JAR 文件",
            api_path="/api/test/suites/apk/analyze",
            method="POST",
            params=[],
            keywords=["套件反编译", "套件APK分析", "suite apk analyze", "源码分析"],
            is_readonly=False,
            is_dangerous=False,
            requires_confirm=False,
            executor_ref="routers.tests:create_suite_apk_analysis_task",
            response_type="detail",
        ),
        AgentTool(
            name="test_parse_args",
            category="test",
            description="解析测试命令参数",
            api_path="/api/test/parse-args",
            method="POST",
            params=[{"name": "params", "type": "array", "required": True, "desc": "参数列表"}],
            keywords=["解析参数", "parse args", "参数解析"],
            is_readonly=True,
            is_dangerous=False,
            requires_confirm=False,
            executor_ref="routers.tests:parse_test_args",
            response_type="detail",
        ),
        AgentTool(
            name="architecture",
            category="system",
            description="查看系统架构图",
            api_path="/templates/architecture.html",
            method="GET",
            params=[],
            keywords=["架构", "architecture", "系统架构", "架构图"],
            is_readonly=True,
            is_dangerous=False,
            requires_confirm=False,
            executor_ref="",
            response_type="detail",
        ),
        AgentTool(
            name="tools_list",
            category="assets",
            description="管理常用网址工具",
            api_path="/api/assets/tools",
            method="GET",
            params=[],
            keywords=["常用网址", "工具", "网址", "tools", "网站", "收藏"],
            is_readonly=True,
            is_dangerous=False,
            requires_confirm=False,
            executor_ref="",
            response_type="list",
        ),
    ]
    for tool in extras:
        if not registry.get(tool.name):
            registry.register(tool)


# 全局单例
registry = _build_registry()
