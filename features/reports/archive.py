from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

from foundation.archives import (
    UPLOAD_ARCHIVE_EXTENSIONS,
    copy_archive_member,
    enforce_post_extraction_safety,
    preflight_system_archive,
    safe_extract_member_path,
)

from .host_parser import HostLogParser
from .models import TestReport
from .xml_parser import XMLReportParser


logger = logging.getLogger(__name__)


def default_report_temp_dir() -> str:
    return os.environ.get('GMS_REPORT_TEMP_DIR') or str(Path(tempfile.gettempdir()) / 'gms_report')


# Archive security helpers moved to foundation.archives (single shared
# policy); private aliases keep intra-feature call sites stable.
_enforce_post_extraction_safety = enforce_post_extraction_safety
_preflight_system_archive = preflight_system_archive

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
        budget: dict[str, int] = {}
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.infolist():
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError(
                        f'压缩包包含不安全符号链接: {member.filename}'
                    )
                target = safe_extract_member_path(self.temp_dir, member.filename)
                if member.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member) as src, open(target, 'wb') as dst:
                    copy_archive_member(src, dst, budget)

    def _extract_tar(self, tar_path: str):
        budget: dict[str, int] = {}
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
                        copy_archive_member(src, dst, budget)

    def _extract_7z(self, archive_path: str):
        """使用系统 7z 解压 RAR/7z 等格式。

        系统命令本身不做 Zip Slip / 文件数 / 解压炸弹防护，
        因此在解压完成后立即扫描目标目录，执行与 zip/tar 相同的安全约束。
        """
        if archive_path.lower().endswith('.rar') and shutil.which('rar'):
            _preflight_system_archive(archive_path, self.temp_dir, 'rar', timeout=300)
            subprocess.run(
                ['rar', 'x', '-y', archive_path, self.temp_dir + os.sep],
                check=True,
                capture_output=True,
                timeout=300,
            )
        elif shutil.which('7z'):
            _preflight_system_archive(archive_path, self.temp_dir, '7z', timeout=300)
            subprocess.run(
                ['7z', 'x', '-y', f'-o{self.temp_dir}', archive_path],
                check=True,
                capture_output=True,
                timeout=300,
            )
        else:
            raise RuntimeError('7z command not found, cannot extract RAR archive')
        _enforce_post_extraction_safety(self.temp_dir)

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

        if lower_path.endswith(UPLOAD_ARCHIVE_EXTENSIONS):
            try:
                from .analysis_agent import ReportAnalysisAgent, ReportScanIncompleteError

                result = ReportAnalysisAgent(temp_dir=self.temp_dir).analyze_path(file_path)
                if result:
                    return result
            except ReportScanIncompleteError:
                raise
            except Exception as e:
                logger.error("ReportAnalysisAgent failed: %s", e)
            return None
        elif lower_path.endswith('.xml'):
            report = self.parser.parse_file(file_path)
        else:
            logger.error(f"不支持的文件格式: {file_path}")
            return None

        if report:
            self.report = report
            return self._report_to_dict(report)
        return None

    def analyze_log_dir(self, log_dir: str) -> dict | None:
        report = self.host_log_parser.parse_log_dir(log_dir)
        if report:
            return self._report_to_dict(report)
        return None

    def _report_to_dict(self, report: TestReport) -> dict:
        """将报告对象转换为 API 字典。"""
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
                'suite_version': report.suite_version,
                'android_version': report.android_version,
                'start_time': report.start_time
            },
            'failures': [
                {
                    'name': f.name,
                    'reason': f.reason,
                    'module': f.module,
                    'stack_trace': f.stack_trace,
                    'raw_log': f.raw_log
                }
                for f in report.failures
            ]
        }
