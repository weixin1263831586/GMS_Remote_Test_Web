#!/usr/bin/env python3
"""
测试日志管理模块
处理日志文件列表、下载、保存等功能
"""

import logging
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


class TestLogsManager:
    """测试日志管理器"""

    def __init__(self):
        web_app_dir = Path(__file__).resolve().parent.parent
        test_log_root = Path(
            os.environ.get('GMS_TEST_LOG_ROOT', '/tmp/test-logs')
        )
        user_logs_dir = Path(
            os.environ.get('GMS_LOG_DIR', str(Path.home() / 'Logs'))
        )
        self.saved_logs_dir = test_log_root / 'saved'
        self.downloads_dir = test_log_root / 'downloads'

        self.log_dirs = [
            Path('/tmp/xts-root-dir'),
            test_log_root,
            self.saved_logs_dir,
            user_logs_dir,
            web_app_dir / 'logs',
            web_app_dir / 'data' / 'logs'
        ]

    @staticmethod
    def _safe_log_token(value: str) -> str:
        token = re.sub(r'[^A-Za-z0-9_-]+', '_', str(value or '').strip()).strip('_-')[:80]
        return token or 'unknown'

    def _allowed_log_roots(self) -> list[Path]:
        return [path.resolve() for path in self.log_dirs if path.exists()]

    def _resolve_allowed_log_path(self, file_path: str) -> Path:
        candidate = Path(file_path).expanduser().resolve()
        if candidate.suffix != '.log':
            raise ValueError('仅允许访问 .log 文件')
        for root in self._allowed_log_roots():
            if candidate == root or root in candidate.parents:
                return candidate
        raise ValueError(f'日志路径不在允许目录内: {file_path}')

    def list_log_files(self) -> dict[str, Any]:
        """列出所有日志文件"""
        log_files = []

        for base_dir in self.log_dirs:
            base_path = Path(base_dir)
            if not base_path.exists():
                continue

            for log_file in base_path.rglob('*.log'):
                stat = log_file.stat()
                log_files.append({
                    'name': log_file.name,
                    'path': str(log_file),
                    'size': stat.st_size,
                    'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    'base_dir': str(base_path)
                })

        log_files.sort(key=lambda x: x['modified'], reverse=True)

        return {
            'success': True,
            'total': len(log_files),
            'files': log_files[:100]
        }

    def get_log_file(self, file_path: str, max_lines: int = 1000) -> dict[str, Any]:
        """读取日志文件内容"""
        try:
            log_path = self._resolve_allowed_log_path(file_path)
        except ValueError as e:
            return {
                'success': False,
                'error': str(e),
            }

        if not log_path.exists():
            return {
                'success': False,
                'error': f'文件不存在: {file_path}'
            }

        try:
            content = log_path.read_text(encoding='utf-8', errors='ignore')
            lines = content.splitlines()

            if len(lines) > max_lines:
                content = '\n'.join(lines[-max_lines:])
                returned_lines = max_lines
            else:
                returned_lines = len(lines)

            return {
                'success': True,
                'file': file_path,
                'total_lines': len(lines),
                'returned_lines': returned_lines,
                'content': content
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }

    def save_current_log(
        self,
        log_content: str,
        client_id: str
    ) -> dict[str, Any]:
        """保存当前日志"""
        try:
            save_dir = self.saved_logs_dir
            save_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'test_log_{self._safe_log_token(client_id)}_{timestamp}.log'
            file_path = save_dir / filename

            file_path.write_text(log_content, encoding='utf-8')

            return {
                'success': True,
                'file_path': str(file_path),
                'filename': filename,
                'size': len(log_content)
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }

    def download_logs(
        self,
        file_paths: list[str],
        output_path: str | None = None
    ) -> dict[str, Any]:
        """打包下载日志文件"""
        if not file_paths:
            return {
                'success': False,
                'error': '未选择任何文件'
            }

        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            if output_path is None:
                zip_dir = self.downloads_dir
                zip_dir.mkdir(parents=True, exist_ok=True)
                output_path = str(zip_dir / f'logs_{timestamp}.zip')

            with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for file_path_str in file_paths:
                    file_path = self._resolve_allowed_log_path(file_path_str)
                    if file_path.exists() and file_path.is_file():
                        zipf.write(file_path, file_path.name)

            return {
                'success': True,
                'zip_path': output_path,
                'file_count': len(file_paths)
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }

    def clean_old_logs(self, days: int = 7) -> dict[str, Any]:
        """清理旧日志"""
        cleaned = 0
        failed = 0
        total_size = 0
        cutoff_time = datetime.now().timestamp() - (days * 86400)

        for base_dir in self.log_dirs:
            base_path = Path(base_dir)
            if not base_path.exists():
                continue

            for log_file in base_path.rglob('*.log'):
                try:
                    st = log_file.stat()
                    if st.st_mtime < cutoff_time:
                        total_size += st.st_size
                        log_file.unlink()
                        cleaned += 1
                except Exception as exc:
                    failed += 1
                    logger.warning("Failed to clean old log %s: %s", log_file, exc)

        return {
            'success': True,
            'cleaned_files': cleaned,
            'failed_files': failed,
            'freed_space_mb': round(total_size / (1024 * 1024), 2)
        }


# 全局实例
test_logs_manager = TestLogsManager()
