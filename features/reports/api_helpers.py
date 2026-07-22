"""Reports router - report management, analysis, and knowledge base APIs."""

import asyncio
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
from typing import Any
from urllib.parse import urlparse

import aiohttp
from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from features.redmine import get_redmine_config_for_request
from features.reports.analyzer import ReportAnalyzer
from features.reports.repository import test_report_db
from foundation.archives import ARCHIVE_EXTENSIONS
from foundation.config import ConfigManager
from foundation.responses import error_response, success_response
from foundation.uploads import (
    extract_report_name_from_upload,
    safe_upload_target_path,
    save_upload_to_path,
)
from workflows.report_to_redmine import (
    COMPILED_REDMINE_ATTACHMENT_PATTERN,
    COMPILED_REDMINE_ISSUE_PATTERN,
    COMPILED_REPORT_NAME_PATTERN,
    REDMINE_ISSUE_PATTERN,
    RedmineClient,
    create_basic_auth_header,
    extract_filename_from_content_disposition,
    extract_redmine_issue_id_from_text,
    redmine_config_manager,
    strip_redmine_report_prefix,
)

from .ai_diagnosis import analyze_failure_with_ai
from .api_models import AnalysisMode, ReportDiagnosisRequest
from .api_support import (
    REDMINE_ISSUE_ID_CACHE,
    REDMINE_ISSUE_ID_CACHE_MAX_SIZE,
    StackTraceUtils,
)
from .dependencies import dependencies
from .service import test_report_manager


logger = logging.getLogger(__name__)

__all__ = [
    "COMPILED_REDMINE_ATTACHMENT_PATTERN",
    "COMPILED_REDMINE_ISSUE_PATTERN",
    "COMPILED_REPORT_NAME_PATTERN",
    "REDMINE_ISSUE_ID_CACHE",
    "REDMINE_ISSUE_ID_CACHE_MAX_SIZE",
    "REDMINE_ISSUE_PATTERN",
    "APIRouter",
    "AnalysisMode",
    "File",
    "Form",
    "JSONResponse",
    "Query",
    "RedmineClient",
    "ReportAnalyzer",
    "ReportDiagnosisRequest",
    "Request",
    "StackTraceUtils",
    "UploadFile",
    "_analyze_report_file",
    "_build_patch_draft",
    "_ensure_uploaded_report_extension",
    "_extract_class_names_from_text",
    "_extract_failure_keywords",
    "_get_knowledge_base",
    "_load_redmine_credentials",
    "_looks_like_redmine_url",
    "_redmine_base_url_for",
    "_redmine_config_manager_for_request",
    "_redmine_public_url_hint",
    "_rename_downloaded_report_if_needed",
    "_save_redmine_credentials",
    "aiohttp",
    "analyze_with_ai",
    "asyncio",
    "config_manager",
    "create_basic_auth_header",
    "dependencies",
    "error_response",
    "extract_filename_from_content_disposition",
    "extract_redmine_issue_id_from_text",
    "extract_report_name_from_upload",
    "json",
    "logger",
    "os",
    "re",
    "safe_upload_target_path",
    "save_upload_to_path",
    "shutil",
    "strip_redmine_report_prefix",
    "success_response",
    "tempfile",
    "test_report_db",
    "test_report_manager",
    "urlparse",
]


class _ReportConfig:
    def __init__(self):
        self.base = ConfigManager()

    def load_config(self):
        return self.base.load_config()

    def get_redmine_config(self):
        return redmine_config_manager.get_redmine_config()

    def get_redmine_base_url(self, config=None):
        return redmine_config_manager.get_redmine_base_url(config)

    def save_redmine_credentials(self, username, password):
        return redmine_config_manager.save_redmine_credentials(username, password)

    def load_redmine_credentials(self):
        return redmine_config_manager.load_redmine_credentials()

    def get_ai_provider_config(self, provider_name: str):
        return self.base.get_ai_provider_config(provider_name)


config_manager = _ReportConfig()


def _redmine_config_manager_for_request(request: Request | None = None):
    if request is None:
        return config_manager
    return get_redmine_config_for_request(request)

def _resolve_redmine_knowledge_service(request: Request | None = None):
    """返回认证用户自己的 Redmine 知识服务，不跨用户回退。"""
    from features.auth import require_authenticated_user
    from features.redmine import get_redmine_service_for_owner

    if request is None:
        return None
    user = require_authenticated_user(request)
    return get_redmine_service_for_owner(user.id).knowledge


def _get_knowledge_base(request: Request | None = None):
    """Resolve the Redmine knowledge service backing diagnosis lookups.

    Returns the per-user :class:`RedmineKnowledgeService` (or ``None``). The old
    implementation imported a non-existent ``scripts.redmine_knowledgebase``
    module, so every diagnosis silently reported "no KB match" regardless of
    what the operator had synced — this delegates to the real per-user service.
    """
    return _resolve_redmine_knowledge_service(request)


def _extract_class_names_from_text(test_name: str, error_message: str) -> list[str]:
    """Extract likely Java/Kotlin class names from test and failure text."""
    class_names = set()
    if test_name:
        match = re.match(r"^([\w.]+)#", test_name)
        if match:
            class_names.add(match.group(1).split("$")[0])
    if error_message:
        patterns = [
            r"([\w.]+Test)#(\w+)",
            r"at\s+([\w.$]+)\.",
            r"\b([A-Z][A-Za-z0-9_]*(?:Test|TestCase|Manager|Helper|Service))\b",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, error_message):
                value = match.group(1) if match.lastindex else match.group(0)
                if not value:
                    continue
                if value.startswith(("java.", "javax.", "android.", "androidx.", "com.google.")):
                    continue
                class_names.add(value.split("$")[0])
    return list(class_names)[:8]


def _extract_failure_keywords(test_name: str, error_message: str, stack_trace: str, module: str, class_names: list[str]) -> list[str]:
    """Extract failure keywords for search."""
    # Use local parse_cts_failure_info
    failure_info = parse_cts_failure_info(test_name, error_message)

    keywords = []
    def add_keyword(value):
        value = str(value or "").strip()
        if value and value not in keywords:
            keywords.append(value)

    add_keyword((failure_info.get("class_name") or "").split(".")[-1])
    add_keyword(failure_info.get("method_name") or "")
    add_keyword(failure_info.get("error_type") or "")
    add_keyword(module)

    failure_location = StackTraceUtils.extract_failure_location(stack_trace or "")
    if failure_location:
        add_keyword(failure_location.get("file_name", ""))

    for class_name in class_names or []:
        add_keyword(class_name.split(".")[-1])

    if stack_trace:
        for pattern in [
            r"([A-Z][A-Za-z0-9_]*(?:Exception|Error|Failure))",
            r"([A-Z][A-Za-z0-9_]*Test)",
            r"([A-Z][A-Za-z0-9_]*Manager)",
        ]:
            for match in re.findall(pattern, stack_trace):
                add_keyword(match)

    for token in re.split(r"[^A-Za-z0-9_.#]+", f"{test_name} {error_message}"):
        if len(token) >= 4 and not token.isdigit():
            add_keyword(token)
    return keywords[:8]


# --- Report analysis endpoints ---

def parse_cts_failure_info(test_name, error_message):
    """从 CTS 用例名和错误消息中提取结构化失败信息。"""
    result = {
        'class_name': None,
        'method_name': None,
        'package': None,
        'error_type': None,
        'error_keywords': []
    }

    # 解析测试名称
    if test_name and '#' in test_name:
        class_part, method_part = test_name.split('#', 1)
        result['class_name'] = class_part.strip()
        result['method_name'] = method_part.strip()

        # 提取包名
        if '.' in result['class_name']:
            parts = result['class_name'].split('.')
            result['package'] = '.'.join(parts[:-1])  # 去掉最后的类名

    # 解析错误类型
    if error_message:
        error_patterns = [
            r'(java\.lang\.(\w+Exception))',
            r'(java\.lang\.(\w+Error))',
            r'(android\.view\.(\w+Exception))',
            r'(android\.util\.(\w+Exception))',
        ]

        for pattern in error_patterns:
            match = re.search(pattern, error_message)
            if match:
                result['error_type'] = match.group(1)
                break

        # 提取错误关键词
        keyword_patterns = [
            r'Process crashed',
            r'Instrumentation run failed',
            r'Permission denied',
            r'SecurityException',
            r'NullPointerException',
            r'IllegalArgumentException',
            r'package not found',
            r'Unable to resolve',
            r'Connection refused',
        ]

        for pattern in keyword_patterns:
            if re.search(pattern, error_message, re.IGNORECASE):
                result['error_keywords'].append(pattern)

    return result


def _rule_based_analysis(test_name, error_message, stack_trace, module):
    """在 AI 不可用时执行规则分析。"""
    failure_info = parse_cts_failure_info(test_name, error_message)

    analysis_parts = []
    suggestions = []
    root_cause = ""
    related_docs = []

    # 根据错误类型分析
    if 'Process crashed' in error_message or 'Instrumentation run failed' in error_message:
        root_cause = "测试进程崩溃，可能是由于目标应用或服务异常退出导致"
        analysis_parts.append("测试执行过程中进程异常终止")
        suggestions.extend([
            "检查设备日志（logcat）查找崩溃原因",
            "验证被测试的应用是否正常安装和运行",
            "检查设备内存是否充足",
            "查看系统日志中是否有ANR或FC信息"
        ])
        related_docs.append({
            'title': 'Android调试指南',
            'url': 'https://source.android.com/docs/core/debug'
        })

    elif 'Permission' in error_message or 'SecurityException' in error_message:
        root_cause = "权限相关错误，缺少必要的权限声明或配置"
        analysis_parts.append("测试用例需要特定权限但未获得授权")
        suggestions.extend([
            "检查AndroidManifest.xml中的权限声明",
            "验证runtime permission是否正确请求",
            "检查签名是否匹配",
            "确认premission-level是否正确"
        ])
        related_docs.append({
            'title': 'Android权限文档',
            'url': 'https://developer.android.com/guide/topics/permissions/overview'
        })

    elif 'AssertionError' in error_message:
        root_cause = "断言失败，测试条件不满足"
        analysis_parts.append("测试断言检查失败")

        if 'multiuser' in test_name.lower():
            analysis_parts.append("多用户功能测试失败")
            suggestions.extend([
                "检查UserManager服务是否正常",
                "验证多用户配置是否正确",
                "确认restricted profile功能已实现",
                "检查用户切换相关API"
            ])
            related_docs.append({
                'title': 'Android多用户文档',
                'url': 'https://source.android.com/docs/core/architecture/configuration/multi-user'
            })

        if 'GmsCore' in test_name or 'gmscore' in test_name.lower():
            analysis_parts.append("GMS Core相关测试失败")
            suggestions.extend([
                "检查GMS Core包是否正确安装",
                "验证GMS服务权限配置",
                "检查Google Play Services版本",
                "确认GMS证书配置正确"
            ])
            related_docs.append({
                'title': 'GMS Core文档',
                'url': 'https://developer.android.com/google/play/services'
            })

    elif 'package not found' in error_message.lower():
        root_cause = "目标包未找到或未安装"
        suggestions.extend([
            "确认目标应用已正确安装",
            "检查包名是否正确",
            "验证应用是否与当前Android版本兼容"
        ])

    # 通用建议
    if not suggestions:
        suggestions = [
            "查看完整的测试日志了解详细错误信息",
            "检查设备状态是否正常",
            "验证测试环境配置",
            "查阅CTS/GTS测试文档了解测试要求"
        ]

    # 组合分析结果
    analysis = "\n".join(analysis_parts) if analysis_parts else "测试执行失败，请查看详细错误信息"

    # 如果没有根本原因，从错误消息中推断
    if not root_cause:
        if failure_info.get('error_type'):
            root_cause = f"错误类型: {failure_info['error_type']}"
        else:
            root_cause = "测试执行过程中出现异常"

    return {
        'analysis': analysis,
        'suggestions': suggestions[:8],  # 最多8条建议
        'root_cause': root_cause,
        'related_docs': related_docs,
        'ai_enabled': False  # 标记这不是AI分析
    }


def analyze_with_ai(test_name, error_message, stack_trace='', module='', class_names=None):
    """调用已配置模型分析测试失败并补充源码信息。"""
    return analyze_failure_with_ai(
        test_name, error_message, stack_trace, module, class_names,
        analyzer_factory=dependencies.universal_analyzer_factory,
        parse_failure_info=parse_cts_failure_info,
        rule_based_analysis=_rule_based_analysis,
        config_manager=config_manager,
        stack_trace_utils=StackTraceUtils,
        logger=logger,
    )


def _build_patch_draft(diagnosis: dict[str, Any]) -> str:
    """Build a draft patch from diagnosis results.

    A verified knowledge-base hit takes priority over the speculative AI root
    cause: when the top KB match is an environment/configuration issue (no code
    fix), we emit an environment-handling note instead of fabricating a Java
    diff that would mislead the operator.
    """
    failure_location = diagnosis.get("failure_location") or {}
    ai_result = diagnosis.get("ai_result") or {}
    suite_target = diagnosis.get("suite_target") or {}
    source_guess = suite_target.get("source_guess") or {}
    source_hits = diagnosis.get("source_search_results") or []
    kb_results = diagnosis.get("knowledge_base_results") or []

    # 环境或配置问题应返回操作建议，不生成源码补丁。
    env_note = _kb_environment_patch(kb_results)
    if env_note:
        return env_note

    target_path = source_guess.get("source_path") or ""
    if not target_path and source_hits:
        target_path = source_hits[0].get("path", "") or source_hits[0].get("display_path", "")
    if not target_path and failure_location.get("file_name"):
        ext = failure_location.get("file_type", "java")
        target_path = f"<source>/{failure_location.get('file_name')}.{ext}"
    if not target_path:
        target_path = "<source file to locate>"

    # Prefer the verified KB root cause over the speculative AI guess.
    kb_root = ""
    for hit in kb_results:
        root = (hit.get("root_cause") or "").strip()
        if root:
            kb_root = root
            break
    if not kb_root and ai_result.get("root_cause_status") != "verified":
        observed = ai_result.get("observed_failure") or diagnosis.get("error_message") or "未提取到明确失败信息"
        hypothesis = ai_result.get("root_cause") or "尚无可靠根因假设"
        lines = [
            "# 暂不生成代码补丁：根因尚未验证",
            f"# 已观察到: {observed}",
            f"# 待验证假设: {hypothesis}",
            "# 下一步: 先复现并采集对应系统服务日志，再依据因果证据确定修改点。",
        ]
        lines.extend(f"# - {item}" for item in (ai_result.get("suggestions") or [])[:3])
        return "\n".join(lines)
    reason = kb_root or ai_result.get("root_cause") or diagnosis.get("summary", "") or "Pending confirmation"
    patch_lines = [
        f"--- a/{target_path}",
        f"+++ b/{target_path}",
        "@@ -1,6 +1,12 @@",
        f"- // Failure: {reason}",
        f"+ // Suggested fix: {reason}",
    ]
    for line in (ai_result.get("suggestions") or [])[:3]:
        patch_lines.append(f"+ // {line}")
    solution = ai_result.get("solution") or {}
    code_example = solution.get("code_example", "") if isinstance(solution, dict) else ""
    if code_example:
        patch_lines.extend(["+ // Reference AI suggested code:", "+ // ----------------------------------------"])
        for raw_line in str(code_example).splitlines()[:12]:
            patch_lines.append(f"+ // {raw_line}")
        patch_lines.append("+ // ----------------------------------------")
    return "\n".join(patch_lines)


# 环境及配置类根因标记。
_ENV_ROOT_MARKERS = ("副屏", "多屏", "secondary display", "测试环境", "环境配置", "known limitation", "已知限制")


def _kb_environment_patch(kb_results: list[dict[str, Any]]) -> str:
    """知识库命中非代码问题时返回操作建议。"""
    if not kb_results:
        return ""
    best: dict[str, Any] | None = None
    for hit in kb_results:
        root = (hit.get("root_cause") or "").strip()
        solution = (hit.get("solution_summary") or hit.get("solution") or "").strip()
        haystack = f"{root} {solution}".lower()
        if any(marker.lower() in haystack for marker in _ENV_ROOT_MARKERS):
            best = {"root": root, "solution": solution, "id": hit.get("id")}
            break
    if best is None:
        return ""
    ref = f"#{best['id']}" if best["id"] else ""
    lines = [
        f"// 知识库命中 {ref}：本失败为测试环境/配置问题，无需代码补丁。",
        f"// 根因：{best['root']}",
        "// 建议处置：",
    ]
    for raw in best["solution"].splitlines():
        text = raw.strip()
        if text:
            lines.append(f"//   {text}")
    return "\n".join(lines)


async def _analyze_report_file(
    file_path: str,
    temp_dir: str | None = None,
) -> dict | None:
    """Analyze report file."""
    if temp_dir is None:
        temp_dir = os.path.dirname(file_path)
    analyzer = ReportAnalyzer(temp_dir=temp_dir)
    return await asyncio.to_thread(analyzer.analyze_file, file_path)


async def _save_redmine_credentials(username: str, password: str, request: Request | None = None):
    return _redmine_config_manager_for_request(request).save_redmine_credentials(username, password)


async def _load_redmine_credentials(request: Request | None = None):
    return _redmine_config_manager_for_request(request).load_redmine_credentials()


def _looks_like_redmine_url(url: str) -> bool:
    """Detect Redmine-looking paths for validation before generic URL handling."""
    return bool(
        COMPILED_REDMINE_ISSUE_PATTERN.search(url or "")
        or COMPILED_REDMINE_ATTACHMENT_PATTERN.search(url or "")
    )


def _redmine_base_url_for(redmine_config: dict[str, Any] | None, configured_match: bool) -> str:
    """Return the configured public Redmine base URL only for approved hosts."""
    if configured_match and redmine_config and redmine_config.get("base_url"):
        return redmine_config["base_url"].rstrip("/")
    return ""


def _redmine_public_url_hint(url: str, redmine_config: dict[str, Any] | None) -> str:
    """Build the public Redmine URL that corresponds to a rejected Redmine-like URL."""
    base_url = config_manager.get_redmine_base_url({"redmine": redmine_config or {}})
    parsed = urlparse(url)
    if parsed.path:
        return f"{base_url.rstrip('/')}{parsed.path}"
    return base_url


def _detected_report_extension(file_path: str, content_type: str = "") -> str:
    """Infer report container extension from content headers or magic bytes."""
    content_type = (content_type or "").lower()
    if "zip" in content_type:
        return ".zip"
    if "rar" in content_type:
        return ".rar"
    if "xml" in content_type:
        return ".xml"

    try:
        with open(file_path, "rb") as f:
            head = f.read(16)
    except Exception:
        return ""

    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06") or head.startswith(b"PK\x07\x08"):
        return ".zip"
    if head.startswith(b"Rar!\x1a\x07"):
        return ".rar"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return ".7z"
    if head.lstrip().startswith(b"<?xml") or head.lstrip().startswith(b"<"):
        return ".xml"
    if head.startswith(b"\x1f\x8b"):
        return ".gz"
    return ""


def _rename_downloaded_report_if_needed(file_path: str, filename: str, content_type: str = "") -> tuple[str, str]:
    """Ensure downloaded reports have an extension the analyzer can route on."""
    current_ext = os.path.splitext(filename or "")[1].lower()
    detected_ext = _detected_report_extension(file_path, content_type)
    if not detected_ext or current_ext == detected_ext:
        return file_path, filename

    if current_ext and current_ext not in {".zip", ".rar", ".xml", ".gz"}:
        return file_path, filename

    base_name = os.path.splitext(filename or "downloaded_report")[0] or "downloaded_report"
    new_filename = f"{base_name}{detected_ext}"
    new_path = safe_upload_target_path(os.path.dirname(file_path), new_filename, allow_nested=False)
    if os.path.abspath(new_path) != os.path.abspath(file_path):
        os.replace(file_path, new_path)
        logger.info(f"[Report Analysis] Detected downloaded file type: {filename} -> {new_filename}")
    return new_path, new_filename


def _ensure_uploaded_report_extension(file_path: str, filename: str, content_type: str = "") -> tuple[str, str]:
    """Ensure uploaded reports keep a parser-recognizable extension."""
    lower_name = (filename or "").lower()
    if lower_name.endswith(ARCHIVE_EXTENSIONS) or lower_name.endswith(".xml"):
        return file_path, filename

    detected_ext = _detected_report_extension(file_path, content_type)
    if not detected_ext:
        return file_path, filename

    base_name = os.path.basename(filename or "uploaded_report")
    if detected_ext == ".gz" and tarfile.is_tarfile(file_path):
        detected_ext = ".tar.gz"
    new_filename = f"{base_name}{detected_ext}"
    new_path = safe_upload_target_path(os.path.dirname(file_path), new_filename, allow_nested=False)
    if os.path.abspath(new_path) != os.path.abspath(file_path):
        os.replace(file_path, new_path)
        logger.info("[Report Analysis] Detected uploaded file type: %s -> %s", filename, new_filename)
    return new_path, new_filename



__all__ = [name for name in globals() if not name.startswith("__")]
