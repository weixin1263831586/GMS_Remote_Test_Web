"""
报告管理 - 核心业务逻辑

特性:
- 报告列表查询
- 报告文件浏览
- 报告分析
- 失败用例提取
"""

import os
import logging
import re
from typing import Dict, Any, List, Optional
from datetime import datetime

from .test_report_db import test_report_db
from .report_analyzer import ReportAnalyzer, HostLogParser

logger = logging.getLogger(__name__)


class TestReportManager:
    """
    测试报告管理器

    特性:
    - 报告列表管理
    - 报告文件浏览
    - 报告内容分析
    - 失败用例提取
    """

    def __init__(self):
        """初始化报告管理器"""
        self.test_report_db = test_report_db
        self.report_analyzer = ReportAnalyzer()
        self.host_log_parser = HostLogParser()

    def list_reports(
        self,
        client_id: str,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        获取报告列表 (当前用户的报告)

        Args:
            client_id: 客户端 ID
            limit: 返回数量限制

        Returns:
            报告列表
        """
        try:
            all_reports = self.test_report_db.get_reports(limit=limit)
            user_reports = [r for r in all_reports if r.get('client_id') == client_id]
            return user_reports
        except Exception as e:
            logger.error(f"Error listing reports: {e}")
            return []

    def get_report_files(
        self,
        report_timestamp: str
    ) -> List[Dict[str, Any]]:
        """获取报告文件列表"""
        try:
            report = self.test_report_db.get_report_by_timestamp(report_timestamp)
            if not report:
                logger.warning(f"Report not found: {report_timestamp}")
                return []

            result_dir = report.get('result_dir')
            if not result_dir or not os.path.exists(result_dir):
                logger.warning(f"Report directory not found: {result_dir}")
                return []

            files = []
            for root, dirs, filenames in os.walk(result_dir):
                for filename in filenames:
                    file_path = os.path.join(root, filename)
                    rel_path = os.path.relpath(file_path, result_dir)
                    try:
                        file_size = os.path.getsize(file_path)
                    except Exception:
                        file_size = 0

                    files.append({
                        'name': filename,
                        'path': file_path,
                        'relative_path': rel_path,
                        'size': file_size
                    })
                    if len(files) >= 50:
                        break
                if len(files) >= 50:
                    break

            return files
        except Exception as e:
            logger.error(f"Error listing report files: {e}")
            return []

    def view_report_file(self, file_path: str) -> Optional[Dict[str, Any]]:
        """查看报告文件内容"""
        try:
            if not os.path.exists(file_path):
                return None

            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            file_ext = os.path.splitext(file_path)[1].lower()
            content_type = {
                '.xml': 'text/html',
                '.html': 'text/html',
                '.json': 'application/json',
                '.log': 'text/plain',
                '.txt': 'text/plain'
            }.get(file_ext, 'text/plain')

            return {
                'content': content,
                'content_type': content_type,
                'file_name': os.path.basename(file_path),
                'file_size': len(content)
            }
        except Exception as e:
            logger.error(f"Error viewing report file: {e}")
            return None

    def analyze_report(self, report_timestamp: str) -> Optional[Dict[str, Any]]:
        """分析测试报告"""
        try:
            report = self.test_report_db.get_report_by_timestamp(report_timestamp)
            if not report:
                logger.warning(f"Report not found: {report_timestamp}")
                return None

            result_dir = report.get('result_dir')
            if not result_dir or not os.path.exists(result_dir):
                logger.warning(f"Report directory not found: {result_dir}")
                return None

            result_xml = os.path.join(result_dir, 'test_result.xml')
            if not os.path.exists(result_xml):
                logger.warning(f"test_result.xml not found: {result_xml}")
                return None

            result = self.report_analyzer.analyze_file(result_xml)
            if not result:
                logger.error("Failed to analyze test_result.xml")
                return None

            analysis = {
                'report_name': report_timestamp,  # 显示报告时间戳
                'summary': result.get('summary', {}),
                'details': {
                    'device': result.get('details', {}).get('device', ''),
                    'suite_version': result.get('details', {}).get('suite_version', ''),       # 套件版本（如 16.1_r2）
                    'android_version': result.get('details', {}).get('android_version', ''),   # Android版本（build_version_release）
                    'start_time': result.get('details', {}).get('start_time', ''),
                    'test_type': result.get('details', {}).get('test_type', '')
                },
                'failures': result.get('failures', [])
            }

            # 读取 invocation_summary.txt 获取 log 目录路径
            summary_content = ""
            inv_summary = os.path.join(result_dir, 'invocation_summary.txt')
            if os.path.exists(inv_summary):
                try:
                    with open(inv_summary, 'r') as f:
                        summary_content = f.read()
                except Exception as e:
                    logger.error(f"Error reading invocation summary: {e}")

            # 如果 XML 显示 0 个测试，从 host_log 中提取模块执行失败信息
            if analysis['summary'].get('total', 0) == 0 and not result.get('failures'):
                logger.info("No tests found in XML, checking host_log for module failures")
                if summary_content:
                    log_dir_match = re.search(r'LOG DIRECTORY\s*:\s*(/[^\s]+)', summary_content)
                    if log_dir_match:
                        log_dir = log_dir_match.group(1).strip()
                        inv_dirs = [d for d in os.listdir(log_dir) if d.startswith('inv_')]
                        if inv_dirs:
                            inv_dir = os.path.join(log_dir, inv_dirs[0])
                            log_result = self.host_log_parser.parse_log_dir(inv_dir)
                            if log_result:
                                analysis['summary']['total'] = log_result.total
                                analysis['summary']['pass'] = log_result.pass_count
                                analysis['summary']['failed'] = log_result.fail_count
                                analysis['failures'] = [
                                    {'name': f.name, 'reason': f.reason, 'module': f.module}
                                    for f in log_result.failures
                                ]
                                logger.info(f"Extracted {len(log_result.failures)} module failures from host_log")

            # 解析失败 HTML
            failures_html = os.path.join(result_dir, 'test_result_failures_suite.html')
            if os.path.exists(failures_html):
                try:
                    with open(failures_html, 'r', encoding='utf-8') as f:
                        failures_content = f.read()
                    analysis['failures_html'] = self._parse_failures_html(failures_content)
                except Exception as e:
                    logger.error(f"Error parsing failures HTML: {e}")

            # 从 log 目录提取 host_log 和 device_log 错误
            if summary_content:
                log_dir_match = re.search(r'LOG DIRECTORY\s*:\s*(/[^\s]+)', summary_content)
                if log_dir_match:
                    log_dir = log_dir_match.group(1).strip()
                    if os.path.exists(log_dir):
                        inv_dirs = [d for d in os.listdir(log_dir) if d.startswith('inv_')]
                        if inv_dirs:
                            inv_dir = os.path.join(log_dir, inv_dirs[0])

                            # 单次遍历收集所有日志文件
                            host_log_path = None
                            device_log_path = None
                            for f in os.listdir(inv_dir):
                                if f.startswith('host_log') and f.endswith('.txt'):
                                    host_log_path = os.path.join(inv_dir, f)
                                elif f.startswith('device_logcat_test') and f.endswith('.txt'):
                                    device_log_path = os.path.join(inv_dir, f)

                            # 读取并分析 host_log
                            if host_log_path:
                                try:
                                    with open(host_log_path, 'r', encoding='utf-8') as f:
                                        host_log_content = f.read()
                                    analysis['host_log_errors'] = self._extract_log_errors(host_log_content, 'host')
                                except Exception as e:
                                    logger.error(f"Error reading host log: {e}")

                            # 读取并分析 device_log
                            if device_log_path:
                                try:
                                    with open(device_log_path, 'r', encoding='utf-8') as f:
                                        device_log_content = f.read()
                                    analysis['device_log_errors'] = self._extract_log_errors(device_log_content, 'device')
                                except Exception as e:
                                    logger.error(f"Error reading device log: {e}")

            return analysis
        except Exception as e:
            logger.error(f"Error analyzing report: {e}")
            return None

    def _parse_failures_html(self, html_content: str) -> Dict[str, Any]:
        """解析失败用例 HTML"""
        try:
            failures = []
            test_name_pattern = r'<td class="testname">([^<]+)</td>'
            test_names = re.findall(test_name_pattern, html_content)

            details_pattern = r'<div class="details">([^<]*(?:<[^>]+>[^<]*</[^>]+>[^<]*)*)</div>'
            details_list = re.findall(details_pattern, html_content, re.DOTALL)

            for i, test_name in enumerate(test_names[:50]):
                failure_msg = ''
                if i < len(details_list):
                    failure_msg = re.sub(r'<[^>]+>', '', details_list[i])
                    failure_msg = failure_msg.replace('&nbsp;', ' ').replace('&#39;', "'").replace('&quot;', '"')
                    failure_msg = ' '.join(failure_msg.split())

                if failure_msg or 'failed' in test_name.lower():
                    failures.append({
                        'test_name': test_name.strip(),
                        'message': failure_msg
                    })

            return {'failures': failures}
        except Exception as e:
            logger.error(f"Error parsing failures HTML: {e}")
            return {'failures': []}

    def _extract_log_errors(self, log_content: str, log_type: str) -> Dict[str, Any]:
        """从日志中提取错误"""
        try:
            stack_traces = []

            if log_type == 'host':
                # 提取 ModuleListener FAILURE 块
                module_listener_pattern = r'(\d{2}-\d{2} \d{2}:\d{2}:\d{2}) I/ModuleListener:.*?FAILURE:.*?(?=\n\d{2}-\d{2} \d{2}:\d{2}:\d{2}|$)'
                blocks = re.findall(module_listener_pattern, log_content, re.MULTILINE | re.DOTALL)
                stack_traces.extend(blocks[:5])

                # 提取 ConsoleReporter 失败
                console_pattern = r'(\d{2}-\d{2} \d{2}:\d{2}:\d{2}) I/ConsoleReporter:.*?fail:.*?(?=\n\d{2}-\d{2} \d{2}:\d{2}:\d{2}|$)'
                matches = re.findall(console_pattern, log_content, re.MULTILINE)
                stack_traces.extend(matches[:5])

                # 提取 HarnessRuntimeException
                harness_pattern = r'HarnessRuntimeException\[[A-Z_]+\|\d+\|[A-Z_]+\]:\s*([^\n]+)'
                harness_errors = re.findall(harness_pattern, log_content)
                for err in harness_errors[:3]:
                    stack_traces.append(f"Module execution failed: {err.strip()}")

                # 提取一般性 TestInvocation 错误
                if not stack_traces:
                    invocation_errors = re.findall(r'E/TestInvocation: ([^\n]+)', log_content)
                    stack_traces.extend([f"Test invocation error: {err.strip()}" for err in invocation_errors[:2]])

            elif log_type == 'device':
                # 提取 FATAL EXCEPTION（限制搜索范围，避免大文件超时）
                lines = log_content.split('\n')
                fatal_lines = [i for i, line in enumerate(lines) if 'FATAL EXCEPTION' in line]
                for idx in fatal_lines[:3]:
                    # 收集异常堆栈（最多 20 行）
                    block = '\n'.join(lines[idx:min(idx+20, len(lines))])
                    if block.strip():
                        stack_traces.append(block[:500])

            # 去重
            seen = set()
            unique_errors = []
            for error in stack_traces[:50]:
                error_key = re.sub(r'\s+', ' ', error).strip()
                if error_key not in seen and len(error_key) > 50:
                    seen.add(error_key)
                    unique_errors.append(error_key[:800])

            return {
                'errors': unique_errors,
                'total_errors': len(unique_errors)
            }
        except Exception as e:
            logger.error(f"Error extracting log errors: {e}")
            return {'errors': [], 'total_errors': 0}

    def save_test_report(
        self,
        client_id: str,
        config: Dict[str, Any],
        test_params: Dict[str, Any],
        user_logs: List[str]
    ) -> Optional[str]:
        """保存测试报告到数据库"""
        try:
            result_dir = None
            for log in reversed(user_logs):
                log_str = str(log)
                if 'RESULT DIRECTORY' in log_str:
                    match = re.search(r'RESULT DIRECTORY\s*:\s*(/[^\s]+)', log_str)
                    if match:
                        result_dir = match.group(1).strip()
                        logger.info(f"Found RESULT DIRECTORY: {result_dir}")
                        break

            if not result_dir or not os.path.exists(result_dir):
                logger.warning(f"RESULT DIRECTORY not found or doesn't exist: {result_dir}")
                return None

            timestamp = os.path.basename(result_dir)
            existing = self.test_report_db.get_report_by_timestamp(timestamp)
            if existing:
                logger.info(f"Report already exists: {timestamp}")
                return timestamp

            report_info = {
                'timestamp': timestamp,
                'test_type': test_params.get('test_type', 'UNKNOWN').upper(),
                'client_id': client_id,
                'devices': test_params.get('devices', []),
                'result_dir': result_dir,
                'suite_path': test_params.get('test_suite', ''),
                'status': 'completed'
            }

            if '@' in client_id:
                report_info['user'] = client_id.split('@')[0]

            xml_path = os.path.join(result_dir, 'test_result.xml')
            if os.path.exists(xml_path):
                try:
                    result = self.report_analyzer.analyze_file(xml_path)
                    if result:
                        report_info.update({
                            'pass': result['summary']['pass'],
                            'fail': result['summary']['fail'],
                            'total': result['summary']['total'],
                            'pass_rate': result['summary']['pass_rate'],
                            'device': result['details']['device'],
                            'start_time': result['details']['start_time']
                        })
                except Exception as e:
                    logger.error(f"Error parsing XML: {e}")

            if self.test_report_db.add_report(report_info):
                logger.info(f"Report saved: {timestamp}")
                return timestamp

            return None
        except Exception as e:
            logger.error(f"Error saving test report: {e}")
            return None


test_report_manager = TestReportManager()
