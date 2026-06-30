from __future__ import annotations

import glob
import io
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from foundation.archives import ARCHIVE_EXTENSIONS, safe_extract_member_path
from foundation.config import ConfigManager

from .host_parser import HostLogParser
from .models import TestReport
from .xml_parser import XMLReportParser


logger = logging.getLogger(__name__)


def default_report_temp_dir() -> str:
    return os.environ.get('GMS_REPORT_TEMP_DIR') or str(Path(tempfile.gettempdir()) / 'gms_report')


def get_opengrok_project_for_android_version(android_version: str, opengrok_config: dict) -> str:
    if not android_version or not opengrok_config:
        return (opengrok_config or {}).get('default_project', 'Android16')
    match = re.match(r'(\d+)', android_version)
    if match and match.group(1) in opengrok_config.get('project_mapping', {}):
        return opengrok_config['project_mapping'][match.group(1)]
    return opengrok_config.get('default_project', 'Android16')

class ReportFileHandler:
    """报告文件处理器 - 统一处理文件解压和查找"""

    def __init__(self, temp_dir: str):
        self.temp_dir = temp_dir

    def extract_archive(self, archive_path: str) -> bool:
        try:
            if archive_path.endswith('.zip'):
                self._extract_zip(archive_path)
            elif archive_path.endswith(('.tar.gz', '.tgz', '.tar.bz2', '.tar')):
                self._extract_tar(archive_path)
            elif archive_path.endswith(('.rar', '.7z')):
                self._extract_7z(archive_path)
            else:
                logger.warning(f"不支持的压缩格式: {archive_path}")
                return False
            return True
        except Exception as e:
            logger.error(f"解压失败: {e}")
            return False

    def _extract_zip(self, zip_path: str):
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.infolist():
                target = safe_extract_member_path(self.temp_dir, member.filename)
                if member.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member) as src, open(target, 'wb') as dst:
                    shutil.copyfileobj(src, dst)

    def _extract_tar(self, tar_path: str):
        with tarfile.open(tar_path, 'r:*') as tf:
            for member in tf.getmembers():
                target = safe_extract_member_path(self.temp_dir, member.name)
                if member.isdir():
                    os.makedirs(target, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(member)
                if src:
                    with src, open(target, 'wb') as dst:
                        shutil.copyfileobj(src, dst)

    def _extract_7z(self, archive_path: str):
        """使用系统 7z 解压 RAR/7z 等格式。"""
        if archive_path.lower().endswith('.rar') and shutil.which('rar'):
            subprocess.run(
                ['rar', 'x', '-y', archive_path, self.temp_dir + os.sep],
                check=True,
                capture_output=True,
                timeout=300,
            )
            return
        if not shutil.which('7z'):
            raise RuntimeError('7z command not found, cannot extract RAR archive')
        subprocess.run(
            ['7z', 'x', '-y', f'-o{self.temp_dir}', archive_path],
            check=True,
            capture_output=True,
            timeout=300,
        )

    def find_xml_file(self) -> str | None:
        """查找test_result.xml文件"""
        for root, _dirs, files in os.walk(self.temp_dir):
            for file in files:
                if file == 'test_result.xml':
                    return os.path.join(root, file)
        return None

    def find_host_log(self) -> str | None:
        """查找host_log文件"""
        host_logs = glob.glob(os.path.join(self.temp_dir, '**/host_log_*.txt'), recursive=True)
        return host_logs[0] if host_logs else None


class ReportAnalyzer:
    """报告分析器主类 - 对外统一接口"""

    def __init__(self, temp_dir: str | None = None):
        self.temp_dir = temp_dir or default_report_temp_dir()
        self.parser = XMLReportParser()
        self.host_log_parser = HostLogParser()
        self.file_handler = ReportFileHandler(self.temp_dir)
        self.report = None

    def analyze_file(self, file_path: str) -> dict | None:
        os.makedirs(self.temp_dir, exist_ok=True)

        report = None

        lower_path = file_path.lower()

        if lower_path.endswith(ARCHIVE_EXTENSIONS):
            try:
                from .analysis_agent import ReportAnalysisAgent

                result = ReportAnalysisAgent(temp_dir=self.temp_dir).analyze_path(file_path)
                if result:
                    return result
            except Exception as e:
                logger.warning("ReportAnalysisAgent failed, falling back to legacy archive parser: %s", e)
            report = self._analyze_archive(file_path)
        elif lower_path.endswith('.xml'):
            report = self.parser.parse_file(file_path)
        else:
            logger.error(f"不支持的文件格式: {file_path}")
            return None

        if report:
            self.report = report
            return self._report_to_dict(report)
        return None

    @staticmethod
    def _archive_basename(member_name: str) -> str:
        return os.path.basename(member_name.replace('\\', '/'))

    @classmethod
    def _is_test_result_member(cls, member_name: str) -> bool:
        return cls._archive_basename(member_name) == 'test_result.xml'

    @classmethod
    def _is_host_log_member(cls, member_name: str) -> bool:
        return HostLogParser._is_host_log_filename(cls._archive_basename(member_name))

    def _parse_host_log_stream(self, stream, member_name: str) -> TestReport | None:
        with io.TextIOWrapper(stream, encoding='utf-8', errors='ignore') as text_stream:
            return self.host_log_parser.parse_content(
                text_stream.read(),
                os.path.dirname(member_name.replace('\\', '/'))
            )

    def _analyze_archive(self, archive_path: str) -> TestReport | None:
        """直接从压缩包中读取目标文件，避免完整解压大报告。"""
        lower_path = archive_path.lower()
        try:
            if lower_path.endswith('.zip'):
                return self._analyze_zip_archive(archive_path)
            if lower_path.endswith(('.rar', '.7z')):
                return self._analyze_7z_archive(archive_path)
            return self._analyze_tar_archive(archive_path)
        except Exception as e:
            logger.error(f"压缩包分析失败: {e}")
            return None

    def _analyze_zip_archive(self, archive_path: str) -> TestReport | None:
        with zipfile.ZipFile(archive_path, 'r') as zf:
            file_infos = [info for info in zf.infolist() if not info.is_dir()]

            xml_info = next((info for info in file_infos if self._is_test_result_member(info.filename)), None)
            if xml_info:
                with zf.open(xml_info) as stream:
                    return self.parser.parse_stream(stream)

            host_log_info = next((info for info in file_infos if self._is_host_log_member(info.filename)), None)
            if host_log_info:
                with zf.open(host_log_info) as stream:
                    return self._parse_host_log_stream(stream, host_log_info.filename)

        return None

    def _list_7z_members(self, archive_path: str) -> list[str]:
        if archive_path.lower().endswith('.rar') and shutil.which('rar'):
            return self._list_rar_members(archive_path)
        if not shutil.which('7z'):
            raise RuntimeError('7z command not found, cannot read RAR archive')
        result = subprocess.run(
            ['7z', 'l', '-slt', archive_path],
            check=True,
            capture_output=True,
            text=True,
            errors='replace',
            timeout=60,
        )
        members = []
        current_path = ''
        current_is_file = False
        for line in [*result.stdout.splitlines(), '']:
            if line.startswith('Path = '):
                if current_path and current_is_file:
                    members.append(current_path)
                current_path = line.split(' = ', 1)[1].strip()
                current_is_file = False
            elif line == 'Folder = -':
                current_is_file = True
            elif line == 'Folder = +':
                current_is_file = False
            elif line.startswith('Attributes = '):
                current_is_file = not line.split(' = ', 1)[1].startswith('D')
            elif not line and current_path:
                if current_is_file:
                    members.append(current_path)
                current_path = ''
                current_is_file = False
        return members

    def _open_7z_member_bytes(self, archive_path: str, member_name: str) -> io.BytesIO:
        if archive_path.lower().endswith('.rar') and shutil.which('rar'):
            return self._open_rar_member_bytes(archive_path, member_name)
        result = subprocess.run(
            ['7z', 'x', '-so', archive_path, member_name],
            check=True,
            capture_output=True,
            timeout=60,
        )
        return io.BytesIO(result.stdout)

    def _list_rar_members(self, archive_path: str) -> list[str]:
        result = subprocess.run(
            ['rar', 'lb', archive_path],
            check=True,
            capture_output=True,
            text=True,
            errors='replace',
            timeout=60,
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _open_rar_member_bytes(self, archive_path: str, member_name: str) -> io.BytesIO:
        result = subprocess.run(
            ['rar', 'p', archive_path, member_name],
            check=True,
            capture_output=True,
            timeout=60,
        )
        return io.BytesIO(result.stdout)

    def _analyze_7z_archive(self, archive_path: str) -> TestReport | None:
        members = self._list_7z_members(archive_path)

        xml_member = next((member for member in members if self._is_test_result_member(member)), None)
        if xml_member:
            with self._open_7z_member_bytes(archive_path, xml_member) as stream:
                return self.parser.parse_stream(stream)

        host_log_member = next((member for member in members if self._is_host_log_member(member)), None)
        if host_log_member:
            with self._open_7z_member_bytes(archive_path, host_log_member) as stream:
                return self._parse_host_log_stream(stream, host_log_member)

        return None

    def _analyze_tar_archive(self, archive_path: str) -> TestReport | None:
        with tarfile.open(archive_path, 'r:*') as tf:
            file_members = [member for member in tf.getmembers() if member.isfile()]

            xml_member = next((member for member in file_members if self._is_test_result_member(member.name)), None)
            if xml_member:
                stream = tf.extractfile(xml_member)
                if stream:
                    with stream:
                        return self.parser.parse_stream(stream)

            host_log_member = next((member for member in file_members if self._is_host_log_member(member.name)), None)
            if host_log_member:
                stream = tf.extractfile(host_log_member)
                if stream:
                    with stream:
                        return self._parse_host_log_stream(stream, host_log_member.name)

        return None

    def analyze_log_dir(self, log_dir: str) -> dict | None:
        report = self.host_log_parser.parse_log_dir(log_dir)
        if report:
            return self._report_to_dict(report)
        return None

    def analyze_content(self, xml_content: str) -> dict | None:
        report = self.parser.parse_content(xml_content)
        if report:
            return self._report_to_dict(report)
        return None

    def _run_codesearch(self, cmd: list[str], cwd: str) -> subprocess.CompletedProcess | None:
        """Run a codesearch subprocess with standard error handling."""
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=cwd)
            return result if result.returncode == 0 else None
        except subprocess.TimeoutExpired:
            logger.warning("代码搜索超时（30秒）")
            return None
        except Exception as e:
            logger.error(f"代码搜索异常: {e}")
            return None

    def _attach_opengrok_url(self, item: dict[str, Any], base_url: str, project: str) -> None:
        """Build and attach an OpenGrok xref URL to a search result item, then clean temp keys."""
        path = item.get("path", "")
        line = item.get("line")
        if line:
            item["url"] = f"{base_url}/xref/{project}/{path}#{line}"
        else:
            item["url"] = f"{base_url}/xref/{project}/{path}"
        item.pop("_opengrok_base_url", None)
        item.pop("_opengrok_project", None)

    def rk_codesearch(
        self,
        class_name: str,
        failure_location: dict | None = None,
        max_results: int = 5,
    ) -> list[dict[str, str]]:
        """
        Args:
            class_name: 类名 (如 com.android.cts.permission.PermissionTest)
            failure_location: 从堆栈提取的失败位置 {file_name, file_type, line_number}
            max_results: 最大返回结果数

        Returns:
            List[Dict]: 搜索结果列表，每个包含 {project, path, line, type, file_type}
        """
        web_app_dir = Path(__file__).resolve().parents[2]
        codesearch_dir = web_app_dir / 'skills' / 'rk_codesearch'
        codesearch_script = str(codesearch_dir / 'run.py')

        try:
            # 如果有精确失败位置，优先使用
            if failure_location:
                file_name = failure_location.get('file_name', '')
                file_type = failure_location.get('file_type', '')
                line_number = failure_location.get('line_number', '')

                simple_name = file_name.split('$')[0]

                result = self._run_codesearch(
                    ['python3', codesearch_script, 'search', '--keywords', simple_name, '--search-field', 'path', '--limit', '10'],
                    str(codesearch_dir),
                )
                if result:
                    lines = result.stdout.strip().split('\n')
                    target_file = f"{simple_name}.{file_type}"

                    for i, line_text in enumerate(lines):
                        line_text = line_text.strip()
                        if not line_text:
                            continue
                        if target_file in line_text or (simple_name in line_text and f".{file_type}" in line_text):
                            project = ''
                            for j in range(i + 1, min(len(lines), i + 3)):
                                if lines[j].strip().startswith('project:'):
                                    project = lines[j].strip().split(':', 1)[1].strip()
                                    break
                            item = {
                                'type': 'definition',
                                'path': line_text.replace('[definition] ', '').strip() if line_text.startswith('[definition]') else line_text,
                                'line': line_number,
                                'file_type': file_type,
                                'project': project,
                                'is_exact_location': True,
                            }
                            with suppress(Exception):
                                opengrok_config = ConfigManager().load_config().get('opengrok', {})
                                base_url = opengrok_config.get('base_url', '')
                                selected_project = project or get_opengrok_project_for_android_version(
                                    self.report.android_version if self.report else '', opengrok_config
                                )
                                if base_url and selected_project:
                                    self._attach_opengrok_url(item, base_url, selected_project)
                            return [item][:max_results]
                    # Fall through to class name search

            # 没有失败位置时，使用类名搜索定义
            simple_class_name = class_name.split('.')[-1]

            result = self._run_codesearch(
                ['python3', codesearch_script, 'search', '--keywords', simple_class_name, '--search-field', 'def', '--limit', str(max_results)],
                str(codesearch_dir),
            )
            if not result:
                return []

            # 预加载OpenGrok配置
            opengrok_config = {}
            with suppress(Exception):
                opengrok_config = ConfigManager().load_config().get('opengrok', {})

            selected_project = get_opengrok_project_for_android_version(
                self.report.android_version if self.report else '', opengrok_config
            )
            base_url = opengrok_config.get('base_url', '')

            search_results = []
            lines = result.stdout.strip().split('\n')

            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if not line or not (line.startswith('[') and ']' in line):
                    i += 1
                    continue

                bracket_end = line.index(']')
                bracket_content = line[1:bracket_end]
                rest_of_line = line[bracket_end + 1:].strip()

                result_item = {
                    'type': bracket_content,
                    'path': rest_of_line,
                    'file_type': 'kt' if rest_of_line.endswith('.kt') else 'java',
                }

                if base_url and selected_project:
                    result_item['_opengrok_base_url'] = base_url
                    result_item['_opengrok_project'] = selected_project

                for j in range(i + 1, min(len(lines), i + 3)):
                    next_line = lines[j].strip()
                    if next_line.startswith('project:'):
                        result_item['project'] = next_line.split(':', 1)[1].strip()
                        break
                    elif next_line.startswith('['):
                        break

                for j in range(i + 1, min(len(lines), i + 4)):
                    next_line = lines[j].strip()
                    if (
                        next_line
                        and not next_line.startswith('project:')
                        and not next_line.startswith('[')
                        and ':' in next_line
                    ):
                        line_num_part = next_line.split(':')[0].strip()
                        if line_num_part.isdigit():
                            result_item['line'] = line_num_part
                            break

                if '_opengrok_base_url' in result_item:
                    self._attach_opengrok_url(result_item, base_url, selected_project)

                search_results.append(result_item)
                i += 1

            # 去重：按路径去重
            seen_paths = set()
            unique_results = []
            for item in search_results:
                if item['path'] not in seen_paths:
                    seen_paths.add(item['path'])
                    unique_results.append(item)

            return unique_results[:max_results]

        except subprocess.TimeoutExpired:
            logger.warning("代码搜索超时")
            return []
        except Exception as e:
            logger.error(f"代码搜索异常：{e}")
            return []
    def _report_to_dict(self, report: TestReport) -> dict:
        """将报告对象转换为字典（兼容旧格式）"""
        return {
            'summary': {
                'total': report.total,
                'pass': report.pass_count,
                'fail': report.fail_count,
                'pass_rate': report.pass_rate
            },
            'details': {
                'test_type': report.test_type,
                'device': report.device,
                'suite_version': report.suite_version,        # 套件版本（如 16.1_r2）
                'android_version': report.android_version,    # Android版本（build_version_release）
                'start_time': report.start_time
            },
            'failures': [
                {
                    'name': f.name,
                    'reason': f.reason,
                    'module': f.module,
                    'stack_trace': f.stack_trace
                }
                for f in report.failures
            ]
        }


analyzer = ReportAnalyzer()
