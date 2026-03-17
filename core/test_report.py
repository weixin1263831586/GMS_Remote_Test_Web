"""
报告管理 - 核心业务逻辑

特性：
- 报告列表查询
- 报告文件浏览
- 报告分析
- 失败用例提取
"""

import os
import logging
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Optional
from datetime import datetime

from test_report_db import test_report_db
from report_analyzer import ReportAnalyzer

logger = logging.getLogger(__name__)


class TestReportManager:
    """
    测试报告管理器

    特性：
    - 报告列表管理
    - 报告文件浏览
    - 报告内容分析
    - 失败用例提取
    """

    def __init__(self):
        """初始化报告管理器"""
        self.test_report_db = test_report_db
        self.report_analyzer = ReportAnalyzer()

    def list_reports(
        self,
        client_id: str,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        获取报告列表（当前用户的报告）

        Args:
            client_id: 客户端ID
            limit: 返回数量限制

        Returns:
            报告列表
        """
        try:
            # 从数据库获取所有报告
            all_reports = self.test_report_db.get_reports(limit=limit)

            # 过滤当前用户的报告
            user_reports = [r for r in all_reports if r.get('client_id') == client_id]

            return user_reports

        except Exception as e:
            logger.error(f"Error listing reports: {e}")
            return []

    def get_report_files(
        self,
        report_timestamp: str
    ) -> List[Dict[str, Any]]:
        """
        获取报告文件列表

        Args:
            report_timestamp: 报告时间戳

        Returns:
            文件列表
        """
        try:
            # 从数据库获取报告信息
            report = self.test_report_db.get_report_by_timestamp(report_timestamp)

            if not report:
                logger.warning(f"Report not found: {report_timestamp}")
                return []

            # 获取结果目录
            result_dir = report.get('result_dir')
            if not result_dir or not os.path.exists(result_dir):
                logger.warning(f"Report directory not found: {result_dir}")
                return []

            # 列出文件
            files = []
            for root, dirs, filenames in os.walk(result_dir):
                for filename in filenames:
                    file_path = os.path.join(root, filename)
                    # 相对于报告目录的路径
                    rel_path = os.path.relpath(file_path, result_dir)

                    try:
                        file_size = os.path.getsize(file_path)
                    except:
                        file_size = 0

                    files.append({
                        'name': filename,
                        'path': file_path,
                        'relative_path': rel_path,
                        'size': file_size
                    })

                    # 限制返回数量
                    if len(files) >= 50:
                        break

                if len(files) >= 50:
                    break

            return files

        except Exception as e:
            logger.error(f"Error listing report files: {e}")
            return []

    def view_report_file(
        self,
        file_path: str
    ) -> Optional[Dict[str, Any]]:
        """
        查看报告文件内容

        Args:
            file_path: 文件路径

        Returns:
            文件内容字典
        """
        try:
            if not os.path.exists(file_path):
                return None

            # 读取文件内容
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # 确定内容类型
            file_ext = os.path.splitext(file_path)[1].lower()
            if file_ext in ['.xml', '.html']:
                content_type = 'text/html'
            elif file_ext == '.json':
                content_type = 'application/json'
            elif file_ext in ['.log', '.txt']:
                content_type = 'text/plain'
            else:
                content_type = 'text/plain'

            return {
                'content': content,
                'content_type': content_type,
                'file_name': os.path.basename(file_path),
                'file_size': len(content)
            }

        except Exception as e:
            logger.error(f"Error viewing report file: {e}")
            return None

    def analyze_report(
        self,
        report_timestamp: str
    ) -> Optional[Dict[str, Any]]:
        """
        分析测试报告

        Args:
            report_timestamp: 报告时间戳

        Returns:
            分析结果
        """
        try:
            # 从数据库获取报告信息
            report = self.test_report_db.get_report_by_timestamp(report_timestamp)

            if not report:
                logger.warning(f"Report not found: {report_timestamp}")
                return None

            # 获取结果目录
            result_dir = report.get('result_dir')
            if not result_dir or not os.path.exists(result_dir):
                logger.warning(f"Report directory not found: {result_dir}")
                return None

            # 查找test_result.xml
            result_xml = os.path.join(result_dir, 'test_result.xml')
            if not os.path.exists(result_xml):
                logger.warning(f"test_result.xml not found: {result_xml}")
                return None

            # 使用ReportAnalyzer解析XML
            result = self.report_analyzer.analyze_file(result_xml)

            if not result:
                logger.error("Failed to analyze test_result.xml")
                return None

            # 转换为前端需要的格式
            analysis = {
                'summary': result.get('summary', {}),
                'device_info': {
                    'device': result.get('details', {}).get('device', ''),
                    'android_version': result.get('details', {}).get('android_version', '')
                },
                'test_info': {
                    'start_time': result.get('details', {}).get('start_time', ''),
                    'test_type': result.get('details', {}).get('test_type', '')
                },
                'failures': result.get('failures', [])
            }

            # 查找并解析失败HTML
            failures_html = os.path.join(result_dir, 'test_result_failures_suite.html')
            if os.path.exists(failures_html):
                try:
                    with open(failures_html, 'r', encoding='utf-8') as f:
                        failures_content = f.read()
                    analysis['failures_html'] = self._parse_failures_html(failures_content)
                except Exception as e:
                    logger.error(f"Error parsing failures HTML: {e}")

            # 查找invocation目录中的日志文件
            inv_dirs = [d for d in os.listdir(result_dir) if d.startswith('inv_')]
            if inv_dirs:
                inv_dir = os.path.join(result_dir, inv_dirs[0])

                # 查找host_log
                host_logs = [f for f in os.listdir(inv_dir) if f.startswith('host_log') and f.endswith('.txt')]
                if host_logs:
                    try:
                        with open(os.path.join(inv_dir, host_logs[0]), 'r', encoding='utf-8') as f:
                            host_log_content = f.read()
                        analysis['host_log_errors'] = self._extract_log_errors(host_log_content, 'host')
                    except Exception as e:
                        logger.error(f"Error reading host log: {e}")

                # 查找device_logcat_test
                device_logs = [f for f in os.listdir(inv_dir) if f.startswith('device_logcat_test') and f.endswith('.txt')]
                if device_logs:
                    try:
                        with open(os.path.join(inv_dir, device_logs[0]), 'r', encoding='utf-8') as f:
                            device_log_content = f.read()
                        analysis['device_log_errors'] = self._extract_log_errors(device_log_content, 'device')
                    except Exception as e:
                        logger.error(f"Error reading device log: {e}")

            return analysis

        except Exception as e:
            logger.error(f"Error analyzing report: {e}")
            return None

    def _parse_failures_html(self, html_content: str) -> Dict[str, Any]:
        """解析失败用例HTML"""
        import re

        try:
            failures = []

            # 提取失败用例
            # 匹配模式: <td class="testname">test_name</td>
            test_name_pattern = r'<td class="testname">([^<]+)</td>'
            test_names = re.findall(test_name_pattern, html_content)

            # 匹配失败详情
            # <div class="details">...</div>
            details_pattern = r'<div class="details">([^<]*(?:<[^>]+>[^<]*</[^>]+>[^<]*)*)</div>'
            details_list = re.findall(details_pattern, html_content, re.DOTALL)

            # 组合结果
            for i, test_name in enumerate(test_names[:50]):  # 限制数量
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
        import re

        try:
            errors = []
            stack_traces = []

            if log_type == 'host':
                # 提取ModuleListener FAILURE块
                module_listener_pattern = r'(\d{2}-\d{2} \d{2}:\d{2}:\d{2}) I/ModuleListener:.*?FAILURE:.*?(?=\n\d{2}-\d{2} \d{2}:\d{2}:\d{2}|$)'
                blocks = re.findall(module_listener_pattern, log_content, re.MULTILINE | re.DOTALL)
                stack_traces.extend(blocks[:5])

                # 提取ConsoleReporter失败
                console_pattern = r'(\d{2}-\d{2} \d{2}:\d{2}:\d{2}) I/ConsoleReporter:.*?fail:.*?(?=\n\d{2}-\d{2} \d{2}:\d{2}:\d{2}|$)'
                matches = re.findall(console_pattern, log_content, re.MULTILINE)
                stack_traces.extend(matches[:5])

            elif log_type == 'device':
                # 提取FATAL EXCEPTION
                fatal_pattern = r'(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+).*?FATAL EXCEPTION.*?(?=\n\d{2}-\d{2} \d{2}:\d{2}:\d{2}|$)'
                matches = re.findall(fatal_pattern, log_content, re.DOTALL)
                stack_traces.extend(matches[:3])

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
        """
        保存测试报告到数据库

        Args:
            client_id: 客户端ID
            config: 配置字典
            test_params: 测试参数
            user_logs: 用户日志列表

        Returns:
            报告时间戳
        """
        try:
            # 从日志中提取RESULT DIRECTORY
            result_dir = None
            for log in reversed(user_logs):
                log_str = str(log)
                if 'RESULT DIRECTORY' in log_str:
                    import re
                    match = re.search(r'RESULT DIRECTORY\s*:\s*(/[^\s]+)', log_str)
                    if match:
                        result_dir = match.group(1).strip()
                        logger.info(f"Found RESULT DIRECTORY: {result_dir}")
                        break

            if not result_dir or not os.path.exists(result_dir):
                logger.warning(f"RESULT DIRECTORY not found or doesn't exist: {result_dir}")
                return None

            # 提取时间戳
            timestamp = os.path.basename(result_dir)

            # 检查是否已记录
            existing = self.test_report_db.get_report_by_timestamp(timestamp)
            if existing:
                logger.info(f"Report already exists: {timestamp}")
                return timestamp

            # 解析test_result.xml
            xml_path = os.path.join(result_dir, 'test_result.xml')
            report_info = {
                'timestamp': timestamp,
                'test_type': test_params.get('test_type', 'UNKNOWN').upper(),
                'client_id': client_id,
                'devices': test_params.get('devices', []),
                'result_dir': result_dir,
                'suite_path': test_params.get('test_suite', ''),
                'status': 'completed'
            }

            # 提取用户名
            if '@' in client_id:
                report_info['user'] = client_id.split('@')[0]

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

            # 添加到数据库
            if self.test_report_db.add_report(report_info):
                logger.info(f"Report saved: {timestamp}")
                return timestamp

            return None

        except Exception as e:
            logger.error(f"Error saving test report: {e}")
            return None


# 全局报告管理器实例
test_report_manager = TestReportManager()
