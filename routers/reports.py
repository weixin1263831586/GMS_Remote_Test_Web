"""Reports router - report management, analysis, and knowledge base APIs."""

import os
import re
import json
import shutil
import asyncio
import logging
import tempfile
from datetime import datetime
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, File, Form, Body
from fastapi.responses import JSONResponse, PlainTextResponse

from core.config import config_manager
from core.ssh import ssh_manager
from core.report_analyzer import ReportAnalyzer
from core.test_report_db import test_report_db
from core.redmine_utils import (
    COMPILED_REDMINE_ATTACHMENT_PATTERN,
    COMPILED_REDMINE_ISSUE_PATTERN,
    COMPILED_REPORT_NAME_PATTERN,
    REDMINE_ISSUE_PATTERN,
    build_redmine_download_url,
    create_basic_auth_header,
    extract_filename_from_content_disposition,
    extract_redmine_issue_id_from_text,
    fetch_redmine_attachment_issue_id,
    strip_redmine_report_prefix,
)
from core.api_response import error_response, success_response
from core.state import global_state, REDMINE_ISSUE_ID_CACHE
from core.settings import REDMINE_ISSUE_ID_CACHE_MAX_SIZE
from core.schemas import ReportDiagnosisRequest
from core.clients import get_client_id_from_request, parse_client_id
from core.error_handling import handle_api_errors
from core.upload_utils import extract_report_name_from_upload, safe_upload_target_path, save_upload_to_path
from core.enums import AnalysisMode
from core.common_utils import StackTraceUtils

import aiohttp

logger = logging.getLogger(__name__)

router = APIRouter()

# --- Report analyzer singleton for diagnosis ---
from core.test_report import test_report_manager


def _get_knowledge_base():
    """Lazy-load and return a RedmineKnowledgeBase singleton."""
    from scripts.redmine_knowledgebase import RedmineKnowledgeBase
    return RedmineKnowledgeBase()


def _extract_class_names_from_text(test_name: str, error_message: str) -> List[str]:
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


def _extract_failure_keywords(test_name: str, error_message: str, stack_trace: str, module: str, class_names: List[str]) -> List[str]:
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
    """
    解析CTS失败信息，提取关键信息

    Args:
        test_name: 测试用例名称，如 com.google.android.gts.multiuser.RestrictedProfileHostTest#testUserIsRestricted
        error_message: 错误消息

    Returns:
        dict: 包含解析后的信息
    """
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
    """
    基于规则的分析（当AI不可用时）

    Args:
        test_name: 测试用例名称
        error_message: 错误消息
        stack_trace: 堆栈跟踪
        module: 测试模块

    Returns:
        dict: 分析结果
    """
    # 解析失败信息
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
    """
    调用大模型API分析测试失败（支持多个AI提供商，自动获取源码）

    Args:
        test_name: 测试用例名称
        error_message: 错误消息
        stack_trace: 堆栈跟踪
        module: 测试模块名称
        class_names: 从堆栈中提取的类名列表

    Returns:
        dict: AI分析结果（包含源码分析）
    """
    if class_names is None:
        class_names = []

    # 从堆栈跟踪中提取失败位置（使用可复用工具）
    failure_location = StackTraceUtils.extract_failure_location(stack_trace)
    if failure_location:
        logger.info(f"从堆栈提取失败位置: {failure_location['file_name']}.{failure_location['file_type']}:{failure_location['line_number']}")

    source_search_results = []
    # 优先使用通用AI分析器（内部会自动进行源码搜索，无需手动重复搜索）
    try:
        from core.universal_ai import get_universal_analyzer

        # 获取通用AI分析器
        ai_analyzer = get_universal_analyzer()

        # 解析测试信息
        failure_info = parse_cts_failure_info(test_name, error_message)

        # 调用AI分析（自动获取源码）
        result = ai_analyzer.analyze_test_failure(
            class_name=failure_info.get('class_name', ''),
            method_name=failure_info.get('method_name'),
            error_message=error_message,
            stack_trace=stack_trace,
            auto_fetch_source=True  # 启用自动获取源码
        )

        if result['success']:
            provider_name = result.get('provider', 'unknown')
            # 使用统一的配置接口获取 provider 配置
            provider_config = config_manager.get_ai_provider_config(provider_name)
            provider_display = provider_config.get('name', f'{provider_name.upper()} AI') if provider_config else f'{provider_name.upper()} AI'

            # 简化响应结构，直接使用AI返回的emoji格式
            response = {
                'root_cause': result.get('root_cause', ''),
                'analysis': result.get('analysis', ''),
                'suggestions': result.get('suggestions', []),
                'solution': result.get('solution'),
                'ai_enabled': True,
                'ai_model': provider_display,
                'ai_provider': provider_name,
                'stack_trace': stack_trace
            }

            # 添加源码信息（如果成功获取）
            if result.get('source_info'):
                source_info = result['source_info']
                response['source_code_fetched'] = True
                response['source_file_path'] = source_info.get('file_path', '')
                response['source_url'] = source_info.get('url', '')
                response['source_project'] = source_info.get('project', '')
                logger.info(f"成功获取源码信息: {source_info.get('file_path', 'unknown')}")

            # 添加源码搜索结果（供前端显示OpenGrok链接）
            if source_search_results:
                response['source_search_results'] = source_search_results

            return response
        else:
            logger.warning(f"AI分析失败: {result.get('error')}")
            raise Exception(result.get('error', 'AI分析失败'))

    except ImportError:
        logger.warning("通用AI分析器未安装，使用基于规则的分析")
    except Exception as e:
        logger.warning(f"通用AI分析失败: {str(e)}，使用基于规则的分析")

    # AI调用失败，返回基于规则的分析
    return _rule_based_analysis(test_name, error_message, stack_trace, module)


def _build_patch_draft(diagnosis: Dict[str, Any]) -> str:
    """Build a draft patch from diagnosis results."""
    failure_location = diagnosis.get("failure_location") or {}
    ai_result = diagnosis.get("ai_result") or {}
    suite_target = diagnosis.get("suite_target") or {}
    source_guess = suite_target.get("source_guess") or {}
    source_hits = diagnosis.get("source_search_results") or []

    target_path = source_guess.get("source_path") or ""
    if not target_path and source_hits:
        target_path = source_hits[0].get("path", "") or source_hits[0].get("display_path", "")
    if not target_path and failure_location.get("file_name"):
        ext = failure_location.get("file_type", "java")
        target_path = f"<source>/{failure_location.get('file_name')}.{ext}"
    if not target_path:
        target_path = "<source file to locate>"

    reason = ai_result.get("root_cause") or diagnosis.get("summary", "") or "Pending confirmation"
    patch_lines = [
        f"--- a/{target_path}",
        f"+++ b/{target_path}",
        "@@ -1,6 +1,12 @@",
        f"- // TODO: review failure: {reason}",
        f"+ // Fix suggestion: {reason}",
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


async def _analyze_report_file(file_path: str, temp_dir: str = None) -> Optional[Dict]:
    """Analyze report file."""
    if temp_dir is None:
        temp_dir = os.path.dirname(file_path)
    analyzer = ReportAnalyzer(temp_dir=temp_dir)
    return await asyncio.to_thread(analyzer.analyze_file, file_path)


async def _save_redmine_credentials(username: str, password: str):
    return config_manager.save_redmine_credentials(username, password)


async def _load_redmine_credentials():
    return config_manager.load_redmine_credentials()


# ==================== List Reports ====================

@router.get("/api/reports/list")
async def list_reports(request: Request, user_only: bool = False):
    """Get test report list from database."""
    import time
    start_time = time.time()

    try:
        client_id_filter = None
        if user_only:
            client_id = get_client_id_from_request(request)
            config = config_manager.load_config()
            configured_ip = config.get("client_ip", "")
            username = config.get("client_username", "unknown")

            if configured_ip and ("@127.0.0.1" in client_id or "@::1" in client_id or "@localhost" in client_id):
                client_id_filter = f"{username}@{configured_ip}"
            else:
                client_id_filter = client_id

        db_start = time.time()
        all_reports = test_report_db.get_reports(limit=30, user_only=client_id_filter)
        db_time = (time.time() - db_start) * 1000

        total_time = (time.time() - start_time) * 1000
        logger.info(f"[API] /api/reports/list completed: {len(all_reports)} reports, DB: {db_time:.2f}ms, Total: {total_time:.2f}ms")

        return JSONResponse(content={"reports": all_reports})
    except Exception as e:
        logger.error(f"Failed to get report list: {e}")
        return JSONResponse(content={"reports": []})


# ==================== Download Report ====================

@router.get("/api/reports/download")
async def download_report(
    request: Request,
    report_timestamp: str = Query(None),
    download: bool = Query(False),
    path: str = Query(None),
):
    """Unified report interface: list files, download ZIP, or view file content."""
    from core.file_utils import FileUtils

    try:
        if report_timestamp:
            report = test_report_db.get_report_by_timestamp(report_timestamp)
            if not report:
                logger.error(f"[DOWNLOAD] Report not found: {report_timestamp}")
                return error_response(f"Report not found: {report_timestamp}", 404)

            report_dir = report.get("result_dir")
            if not report_dir or not os.path.exists(report_dir):
                logger.error(f"[DOWNLOAD] Report directory not found: {report_dir}")
                return error_response(f"Report directory not found: {report_dir}", 404)

            android_suite_dir = os.path.dirname(os.path.dirname(report_dir))
            logs_dir = os.path.join(android_suite_dir, "logs", report_timestamp)
            has_logs = os.path.exists(logs_dir)

            if download:
                logger.info(f"[DOWNLOAD] Download report ZIP: timestamp='{report_timestamp}'")
                dir_mapping = {report_dir: ""}
                if has_logs:
                    dir_mapping[logs_dir] = "logs"

                result = FileUtils.create_zip_from_multiple_directories(dir_mapping, zip_filename=f"{report_timestamp}.zip")
                if result is None:
                    logger.warning("[DOWNLOAD] No files found")
                    return error_response("No files found", 500)

                zip_data, file_count = result
                logger.info(f"[DOWNLOAD] ZIP created: {report_timestamp}.zip, {file_count} files")

                from fastapi.responses import Response
                return Response(
                    content=zip_data,
                    media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{report_timestamp}.zip"'},
                )

            logger.info(f"[DOWNLOAD] Get report file list: timestamp='{report_timestamp}'")
            all_files = []
            result_files = FileUtils.list_directory_files(report_dir, max_files=100, relative_to=report_dir)
            all_files.extend(result_files)

            log_files = []
            if has_logs:
                log_files = FileUtils.list_directory_files(logs_dir, max_files=100, relative_to=logs_dir)
                for file_info in log_files:
                    file_info["relative_path"] = os.path.join("logs", file_info["relative_path"])
                all_files.extend(log_files)

            logger.info(f"[DOWNLOAD] Found {len(all_files)} files (results: {len(result_files)}, logs: {len(log_files) if has_logs else 0})")
            return JSONResponse(content={"success": True, "files": all_files})

        elif path:
            logger.info(f"[DOWNLOAD] View file content: path='{path}'")
            config = config_manager.load_config()
            ssh = ssh_manager.get_connection(config)
            if not ssh:
                return error_response("SSH connection failed", 500)

            try:
                cat_cmd = f"cat '{path}' 2>/dev/null"
                output, error, code = ssh_manager.execute_command(ssh, cat_cmd, timeout=30)
                ssh_manager.return_connection(ssh)

                file_ext = os.path.splitext(path)[1].lower()
                if file_ext in [".xml", ".html"]:
                    content_type = "text/html"
                elif file_ext == ".json":
                    content_type = "application/json"
                else:
                    content_type = "text/plain"

                return JSONResponse(content={"success": True, "content": output, "content_type": content_type})
            except Exception:
                ssh_manager.return_connection(ssh)
                raise
        else:
            return error_response("Please provide report_timestamp or path parameter", 400)

    except Exception as e:
        logger.error(f"[DOWNLOAD] Request failed: {e}", exc_info=True)
        return error_response(str(e), 500)


# ==================== Analyze URL ====================

@router.post("/api/reports/analyze-url")
async def analyze_report_from_url(request: Request):
    """Download and analyze test report from URL (supports Redmine attachment auto-download)."""
    try:
        body = await request.json()
        url = body.get("url", "").strip()
        redmine_username = body.get("redmine_username", "").strip()
        redmine_password = body.get("redmine_password", "").strip()
        source_issue_id = str(body.get("source_issue_id") or "").strip()
        source_issue_url = body.get("source_issue_url", "").strip()
        source_issue_id = source_issue_id or extract_redmine_issue_id_from_text(source_issue_url)
        source_issue_id = source_issue_id if source_issue_id and source_issue_id.isdigit() else ""

        if not url:
            return error_response("Missing URL parameter", 400)

        logger.info(f"[Report Analysis] URL analysis request: {url}")

        parsed_url = urlparse(url)
        filename = os.path.basename(parsed_url.path) or "downloaded_file.zip"

        redmine_config = None
        is_redmine = False
        try:
            redmine_config = config_manager.get_redmine_config()
            is_redmine = redmine_config["domain"] in url.lower()
        except ValueError:
            pass

        original_issue_id = source_issue_id or None
        attachment_owner_issue_id = None

        if is_redmine:
            issue_match = COMPILED_REDMINE_ISSUE_PATTERN.search(url)
            if issue_match and "/attachments/" not in url:
                issue_id = issue_match.group(1)
                original_issue_id = issue_id
                logger.info(f"[Report Analysis] Redmine issue page detected: {issue_id}")

                try:
                    if redmine_config:
                        base_url = redmine_config["base_url"]
                    else:
                        return error_response("Redmine config unavailable", 404)

                    stored_creds = await _load_redmine_credentials()
                    if stored_creds:
                        api_url = f"{base_url}/issues/{issue_id}.json?include=attachments"
                        username = stored_creds.get("username")
                        password = stored_creds.get("password")
                        headers = {}
                        headers.update(create_basic_auth_header(username, password))
                        logger.info(f"[Report Analysis] Using stored credentials for issue {issue_id}")
                    else:
                        api_url = f"{base_url}/issues/{issue_id}.json?include=attachments"
                        headers = {}
                        logger.warning("[Report Analysis] No stored credentials, anonymous query")

                    async with aiohttp.ClientSession() as session:
                        async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                            if response.status != 200:
                                return error_response(f"Cannot access issue page: HTTP {response.status}", 400)

                            content_type = response.headers.get("Content-Type", "")
                            if "application/json" not in content_type:
                                return error_response("Redmine API returned non-JSON response, possible auth failure", 401)

                            data = await response.json()
                            attachments = data.get("issue", {}).get("attachments", [])
                            if not attachments:
                                return error_response(f"Issue {issue_id} has no attachments", 404)

                            first_attachment = attachments[0]
                            attachment_id = first_attachment.get("id")
                            filename = first_attachment.get("filename", f"attachment_{attachment_id}")
                            url = build_redmine_download_url(base_url, attachment_id)
                            logger.info(f"[Report Analysis] Extracted attachment: {filename} -> {url}")
                except Exception as extract_error:
                    logger.error(f"[Report Analysis] Attachment extraction failed: {extract_error}")
                    return error_response(f"Cannot extract attachment: {str(extract_error)}", 500)
            else:
                redmine_attach_match = COMPILED_REDMINE_ATTACHMENT_PATTERN.search(url)
                if redmine_attach_match:
                    attachment_id = redmine_attach_match.group(1)
                    logger.info(f"[Report Analysis] Redmine attachment URL, ID: {attachment_id}")

                    if attachment_id in REDMINE_ISSUE_ID_CACHE:
                        cached_issue_id = REDMINE_ISSUE_ID_CACHE[attachment_id]
                        if not original_issue_id:
                            original_issue_id = cached_issue_id
                    else:
                        try:
                            base_url = redmine_config["base_url"]
                            stored_creds = await _load_redmine_credentials()
                            headers = {}
                            if stored_creds:
                                headers.update(create_basic_auth_header(stored_creds.get("username"), stored_creds.get("password")))
                            attachment_owner_issue_id = await fetch_redmine_attachment_issue_id(base_url, attachment_id, headers)
                            if attachment_owner_issue_id:
                                if not original_issue_id:
                                    original_issue_id = attachment_owner_issue_id
                        except Exception as search_error:
                            logger.warning(f"[Report Analysis] Query attachment owner failed: {search_error}")

                    if "/attachments/download/" not in url:
                        url = build_redmine_download_url(redmine_config["base_url"], attachment_id)
                        logger.info(f"[Report Analysis] Converted to download URL: {url}")

                    filename = "downloaded_file.zip"

        logger.info(f"[Report Analysis] Downloading: {filename}")
        temp_dir = tempfile.mkdtemp(prefix="redmine_download_")
        temp_file_path = os.path.join(temp_dir, filename)

        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

            if is_redmine:
                if redmine_username and redmine_password:
                    headers.update(create_basic_auth_header(redmine_username, redmine_password))
                    await _save_redmine_credentials(redmine_username, redmine_password)
                else:
                    stored_creds = await _load_redmine_credentials()
                    if stored_creds:
                        redmine_username = stored_creds.get("username")
                        redmine_password = stored_creds.get("password")
                        headers.update(create_basic_auth_header(redmine_username, redmine_password))
                    else:
                        return JSONResponse(content={"success": False, "error": "Redmine credentials not configured", "requires_auth": True, "is_redmine": True}, status_code=401)

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=120), headers=headers) as response:
                    if response.status == 401 or response.status == 403:
                        if is_redmine:
                            return JSONResponse(content={"success": False, "error": "Redmine auth failed", "requires_auth": True, "is_redmine": True}, status_code=403)
                        else:
                            return error_response(f"Download failed, HTTP {response.status}", 400)
                    elif response.status != 200:
                        return error_response(f"Download failed, HTTP {response.status}", 400)

                    downloaded_size = 0
                    real_filename = extract_filename_from_content_disposition(response.headers.get("Content-Disposition", "")) or filename

                    redmine_prefix_match = COMPILED_REPORT_NAME_PATTERN.match(real_filename)
                    if redmine_prefix_match:
                        extracted_issue_id = redmine_prefix_match.group(1)
                        if not original_issue_id:
                            original_issue_id = extracted_issue_id

                    with open(temp_file_path, "wb") as f:
                        async for chunk in response.content.iter_chunked(8192):
                            f.write(chunk)
                            downloaded_size += len(chunk)

                    logger.info(f"[Report Analysis] Download complete: {downloaded_size} bytes")
                    filename = real_filename

            logger.info(f"[Report Analysis] Analyzing: {temp_file_path}")
            result = await _analyze_report_file(temp_file_path, temp_dir)
            if result:
                logger.info(f"[Report Analysis] Analysis complete - failures: {len(result.get('failures', []))}")
            else:
                logger.warning("[Report Analysis] Empty analysis result")

            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass

            if result:
                report_name = filename

                if is_redmine:
                    if original_issue_id:
                        report_filename = strip_redmine_report_prefix(filename)
                        report_name = f"Redmine-{original_issue_id}-{report_filename}"
                        logger.info(f"[Report Analysis] Redmine prefix: {report_name}")
                    else:
                        issue_match = COMPILED_REDMINE_ISSUE_PATTERN.search(url)
                        if issue_match:
                            issue_id = issue_match.group(1)
                            report_name = f"Redmine-{issue_id}-{filename}"
                        else:
                            redmine_attach_match = COMPILED_REDMINE_ATTACHMENT_PATTERN.search(url)
                            if redmine_attach_match:
                                attachment_id = redmine_attach_match.group(1)
                                report_name = f"Redmine attachment {attachment_id}"
                            else:
                                report_name = "Redmine attachment report"

                result["report_name"] = report_name

                # Update cache
                if is_redmine and original_issue_id:
                    redmine_attach_match = COMPILED_REDMINE_ATTACHMENT_PATTERN.search(url)
                    if redmine_attach_match:
                        attachment_id_for_cache = redmine_attach_match.group(1)
                        if REDMINE_ISSUE_ID_CACHE.get(attachment_id_for_cache) != original_issue_id:
                            if len(REDMINE_ISSUE_ID_CACHE) >= REDMINE_ISSUE_ID_CACHE_MAX_SIZE:
                                REDMINE_ISSUE_ID_CACHE.popitem(last=False)
                            REDMINE_ISSUE_ID_CACHE[attachment_id_for_cache] = original_issue_id

                return JSONResponse(content={"success": True, "data": result, "filename": filename, "mode": "url"})
            else:
                return error_response("Report analysis failed", 500)

        except Exception as download_error:
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass
            raise download_error

    except Exception as e:
        logger.error(f"[Report Analysis] URL analysis failed: {e}", exc_info=True)
        return error_response(f"Download or analysis failed: {str(e)}", 500)


# ==================== Redmine Config ====================

@router.get("/api/config/redmine")
async def get_redmine_config(request: Request):
    """Get Redmine configuration."""
    try:
        redmine_config = config_manager.get_redmine_config()
        return JSONResponse(content={"success": True, "data": redmine_config})
    except ValueError as e:
        return error_response(str(e), status_code=404)
    except Exception as e:
        return error_response(f"Failed to get Redmine config: {str(e)}", status_code=500)


# ==================== Extract Redmine Attachment ====================

@router.post("/api/reports/extract-redmine-attachment")
async def extract_redmine_attachment(request: Request):
    """Extract first attachment URL from Redmine issue page."""
    try:
        body = await request.json()
        issue_url = body.get("issue_url", "").strip()

        if not issue_url:
            return error_response("Missing issue_url", 400)

        issue_match = re.search(REDMINE_ISSUE_PATTERN, issue_url)
        if not issue_match:
            return error_response("Invalid issue URL", 400)

        issue_id = issue_match.group(1)
        logger.info(f"[Redmine Extract] Extracting issue {issue_id} attachment")

        stored_creds = await _load_redmine_credentials()
        if not stored_creds:
            return error_response("Redmine credentials not configured", 401)

        try:
            redmine_config = config_manager.get_redmine_config()
            base_url = redmine_config["base_url"]
        except ValueError as e:
            return error_response(str(e), 404)

        api_url = f"{base_url}/issues/{issue_id}.json?include=attachments"
        username = stored_creds.get("username")
        password = stored_creds.get("password")
        headers = create_basic_auth_header(username, password)
        headers["User-Agent"] = "Mozilla/5.0"

        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    return error_response(f"Cannot access issue: HTTP {response.status}", 400)

                data = await response.json()
                attachments = data.get("issue", {}).get("attachments", [])
                if not attachments:
                    return error_response("Issue has no attachments", 404)

                first_attachment = attachments[0]
                attachment_id = first_attachment.get("id")
                filename = first_attachment.get("filename", f"attachment_{attachment_id}")
                attachment_url = build_redmine_download_url(base_url, attachment_id)

                logger.info(f"[Redmine Extract] Found attachment: {filename} (ID: {attachment_id})")
                return JSONResponse(content={"success": True, "attachment_url": attachment_url, "filename": filename, "attachment_id": attachment_id})

    except Exception as e:
        logger.error(f"[Redmine Extract] Failed: {e}")
        return error_response(f"Extraction failed: {str(e)}", 500)


# ==================== Analyze Reports ====================

@router.post("/api/reports/analyze")
async def analyze_reports(
    mode: AnalysisMode = Form(default=AnalysisMode.UPLOAD),
    report_timestamp: Optional[str] = Form(default=None),
    test_name: Optional[str] = Form(default=None),
    error_message: Optional[str] = Form(default=None),
    stack_trace: Optional[str] = Form(default=None),
    module: Optional[str] = Form(default=None),
    class_names: Optional[str] = Form(default=None),
    file: Optional[UploadFile] = File(default=None),
    files: Optional[List[UploadFile]] = File(default=None),
    files_array: Optional[List[UploadFile]] = File(default=None, alias="files[]"),
):
    """Unified report analysis API."""
    try:
        if mode == AnalysisMode.SAVED:
            if not report_timestamp:
                return error_response("Missing report_timestamp", 400)

            report = test_report_db.get_report_by_timestamp(report_timestamp)
            if not report:
                return error_response("Report not found", 404)

            result_dir = report.get("result_dir")
            if not result_dir:
                return error_response("Report directory not found", 404)

            result_xml = os.path.join(result_dir, "test_result.xml")
            if not await asyncio.to_thread(os.path.exists, result_xml):
                result = await asyncio.to_thread(ReportAnalyzer().analyze_log_dir, result_dir)
                if not result:
                    return error_response("test_result.xml not found and host_log parsing failed", 404)
                result["report_name"] = report_timestamp
                return JSONResponse(content={"success": True, "data": result, "mode": "saved"})

            result = await asyncio.to_thread(test_report_manager.analyze_report, report_timestamp)
            if not result:
                return error_response("Report analysis failed", 500)

            return JSONResponse(content={"success": True, "data": result, "mode": "saved"})

        elif mode == AnalysisMode.AI:
            if not test_name:
                return error_response("Missing test_name", 400)

            parsed_class_names = []
            if class_names:
                try:
                    parsed_class_names = json.loads(class_names)
                except json.JSONDecodeError:
                    parsed_class_names = []

            try:
                ai_result = await asyncio.to_thread(analyze_with_ai, test_name, error_message or "", stack_trace or "", module or "", parsed_class_names)
            except Exception:
                ai_result = {"root_cause": "AI analysis unavailable", "analysis": "N/A"}

            return JSONResponse(content={"success": True, "data": ai_result, "mode": "ai"})

        else:  # upload mode
            all_files = []
            if file:
                all_files = [file]
            elif files_array:
                all_files = files_array
            elif files:
                all_files = files

            if not all_files or len(all_files) == 0:
                return error_response("No files uploaded", 400)

            if len(all_files) == 1 and all_files[0].filename == "":
                return error_response("Empty filename", 400)

            with tempfile.TemporaryDirectory() as temp_dir:
                if len(all_files) == 1:
                    uploaded_file = all_files[0]
                    try:
                        temp_file_path = safe_upload_target_path(temp_dir, uploaded_file.filename, allow_nested=False)
                    except ValueError as e:
                        return error_response(str(e), 400)

                    await save_upload_to_path(uploaded_file, temp_file_path)

                    analyzer = ReportAnalyzer(temp_dir=temp_dir)
                    result = await asyncio.to_thread(analyzer.analyze_file, temp_file_path)

                    if result:
                        result["report_name"] = extract_report_name_from_upload([uploaded_file])
                        return JSONResponse(content={"success": True, "data": result, "mode": "upload"})
                    else:
                        return JSONResponse(status_code=400, content={"success": False, "error": "Cannot parse report file", "message": "Please ensure valid XML or archive format"})

                else:
                    for uploaded_file in all_files:
                        if uploaded_file.filename:
                            try:
                                file_path = safe_upload_target_path(temp_dir, uploaded_file.filename, allow_nested=True)
                            except ValueError as e:
                                return error_response(str(e), 400)
                            await save_upload_to_path(uploaded_file, file_path)

                    analyzer = ReportAnalyzer(temp_dir=temp_dir)
                    xml_path = await asyncio.to_thread(analyzer.file_handler.find_xml_file)

                    if not xml_path:
                        logger.info("test_result.xml not found, trying HostLog analyzer")
                        result = await asyncio.to_thread(analyzer.analyze_log_dir, temp_dir)
                        if not result:
                            return JSONResponse(status_code=400, content={"success": False, "error": "No test_result.xml or host_log found", "message": f"Received {len(all_files)} files but no valid report files"})

                        result["report_type"] = "log"
                        result["report_name"] = extract_report_name_from_upload(all_files)
                        return JSONResponse(content={"success": True, "data": result, "mode": "upload"})

                    result = await asyncio.to_thread(analyzer.analyze_file, xml_path)
                    if result:
                        result["report_name"] = extract_report_name_from_upload(all_files)
                        return JSONResponse(content={"success": True, "data": result, "mode": "upload"})
                    else:
                        return JSONResponse(status_code=400, content={"success": False, "error": "Cannot parse report file", "message": "test_result.xml invalid or corrupted"})

    except Exception as e:
        logger.error(f"Report analysis failed: {e}")
        return error_response("Report analysis failed", 500, message=str(e))


# ==================== Diagnose Report ====================

@router.post("/api/reports/diagnose")
async def diagnose_report_failure(request: ReportDiagnosisRequest):
    """Diagnose one report failure and locate matching suite APK/JAR source."""
    try:
        failure_location = StackTraceUtils.extract_failure_location(request.stack_trace or "")
        class_names = [c for c in (request.class_names or []) if c]
        if not class_names:
            class_names = _extract_class_names_from_text(request.test_name, request.error_message)

        keywords = _extract_failure_keywords(request.test_name, request.error_message, request.stack_trace, request.module, class_names)

        # Resolve suite target inline (simplified from monolith)
        async def _resolve_suite_target():
            try:
                from routers.tests import _resolve_suite_diagnosis_target
                return await asyncio.to_thread(
                    _resolve_suite_diagnosis_target,
                    config_manager.load_config(),
                    test_type=request.test_type,
                    suite_version=request.suite_version,
                    module=request.module,
                    test_name=request.test_name,
                    class_names=class_names,
                    source_path=request.source_path,
                )
            except Exception as target_error:
                logger.warning(f"Suite target resolution failed: {target_error}")
                from routers.tests import _make_empty_suite_target
                return _make_empty_suite_target(
                    test_type=request.test_type,
                    suite_version=request.suite_version,
                    test_name=request.test_name,
                    class_names=class_names,
                    match_notes=[f"Resolution failed: {target_error}"],
                )

        async def _run_ai_analysis():
            try:
                return await asyncio.to_thread(analyze_with_ai, request.test_name, request.error_message or "", request.stack_trace or "", request.module or "", class_names)
            except Exception:
                return {"root_cause": "AI analysis unavailable", "analysis": "N/A"}

        async def _search_knowledge_base():
            try:
                kb = _get_knowledge_base()
                kb_query = " ".join(keywords[:5]) or request.test_name or request.error_message[:80]
                if kb_query.strip():
                    return await asyncio.to_thread(kb.search, kb_query, 8)
            except Exception as kb_error:
                logger.warning(f"Knowledge base search failed: {kb_error}")
            return []

        suite_target, ai_result, kb_results = await asyncio.gather(
            _resolve_suite_target(),
            _run_ai_analysis(),
            _search_knowledge_base(),
        )

        source_search_results = []
        analyzer = ReportAnalyzer()
        search_terms = list(class_names[:3])
        if failure_location and failure_location.get("file_name"):
            search_terms.append(failure_location["file_name"])
        search_terms.extend(keywords[:3])

        source_search_coros = [
            asyncio.to_thread(analyzer.rk_codesearch, term, failure_location, 5)
            for term in search_terms if term
        ]
        if source_search_coros:
            seen_paths = set()
            for hits in await asyncio.gather(*source_search_coros, return_exceptions=True):
                if isinstance(hits, Exception):
                    logger.warning(f"Source search failed: {hits}")
                    continue
                for hit in hits or []:
                    dedup_key = f"{hit.get('path', '')}:{hit.get('line', '')}"
                    if dedup_key not in seen_paths:
                        seen_paths.add(dedup_key)
                        source_search_results.append(hit)

        diagnosis = {
            "test_name": request.test_name,
            "error_message": request.error_message,
            "stack_trace": request.stack_trace,
            "module": request.module,
            "report_name": request.report_name,
            "failure_index": request.failure_index,
            "source_path": request.source_path,
            "source_attached": bool(request.source_code),
            "failure_location": failure_location,
            "class_names": class_names,
            "keywords": keywords,
            "ai_result": ai_result,
            "source_search_results": source_search_results[:10],
            "knowledge_base_results": kb_results[:8],
            "suite_target": suite_target,
        }
        diagnosis["summary"] = ai_result.get("root_cause") or ai_result.get("analysis") or "Diagnosis orchestration complete"
        diagnosis["patch_draft"] = _build_patch_draft(diagnosis)

        return success_response(diagnosis, message="Diagnosis complete")
    except Exception as e:
        logger.error(f"Report diagnosis failed: {e}", exc_info=True)
        return error_response("Report diagnosis failed", status_code=500, detail=str(e))


# ==================== Delete Report ====================

@router.delete("/api/reports/delete")
async def delete_report(request: Request, timestamp: str = Query(...)):
    """Delete test report (owner only)."""
    try:
        client_id = get_client_id_from_request(request)
        report = test_report_db.get_report_by_timestamp(timestamp)

        if not report:
            return error_response("Report not found", 404)

        report_client_id = report.get("client_id")
        if report_client_id != client_id:
            logger.warning(f"[DELETE] Permission denied: {client_id} tried to delete {report_client_id}'s report")
            return error_response("No permission to delete this report", 403)

        result_dir = report.get("result_dir")
        if result_dir and os.path.exists(result_dir):
            try:
                shutil.rmtree(result_dir)
                logger.info(f"Deleted report directory: {result_dir}")
            except Exception as e:
                logger.error(f"Failed to delete report directory: {e}")
                return error_response(f"Failed to delete directory: {str(e)}", 500)

        success = test_report_db.delete_report(timestamp)
        if success:
            return success_response(message="Report deleted")
        else:
            return error_response("Failed to delete DB record", 500)

    except Exception as e:
        logger.error(f"Error deleting report: {e}")
        return error_response(str(e), 500)


# ==================== Knowledge Base ====================

@router.get("/api/knowledgebase/search")
async def knowledgebase_search(query: str = Query(..., min_length=1, max_length=256), limit: int = Query(8, ge=1, le=20)):
    """Search the local Redmine-derived GMS knowledge base."""
    try:
        kb = _get_knowledge_base()
        results = await asyncio.to_thread(kb.search, query.strip(), limit)
        return success_response({"query": query.strip(), "results": results, "count": len(results)})
    except Exception as e:
        logger.error(f"Knowledge base search failed: {e}", exc_info=True)
        return error_response(str(e), status_code=500)


@router.get("/api/knowledgebase/stats")
async def knowledgebase_stats():
    """Return local Redmine-derived GMS knowledge base stats."""
    try:
        kb = _get_knowledge_base()
        stats = await asyncio.to_thread(kb.get_stats)
        return success_response({"stats": stats})
    except Exception as e:
        logger.error(f"Knowledge base stats failed: {e}", exc_info=True)
        return error_response(str(e), status_code=500)
