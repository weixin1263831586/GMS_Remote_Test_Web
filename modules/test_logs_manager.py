#!/usr/bin/env python3
"""
测试日志管理模块
处理日志文件列表、下载、保存等功能
"""

import os
import zipfile
from datetime import datetime
from typing import Dict, List, Optional, Any
from pathlib import Path


class TestLogsManager:
    """测试日志管理器"""

    def __init__(self):
        web_app_dir = Path(__file__).resolve().parent.parent

        self.log_dirs = [
            Path('/tmp/xts-root-dir'),
            Path('/tmp/test-logs'),
            Path('/tmp/test-logs/saved'),
            Path('/home/hcq/Logs'),
            web_app_dir / 'logs',
            web_app_dir / 'data' / 'logs'
        ]

    def list_log_files(self) -> Dict[str, Any]:
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

    def get_log_file(self, file_path: str, max_lines: int = 1000) -> Dict[str, Any]:
        """读取日志文件内容"""
        log_path = Path(file_path)

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
    ) -> Dict[str, Any]:
        """保存当前日志"""
        try:
            save_dir = Path('/tmp/test-logs/saved')
            save_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'test_log_{client_id}_{timestamp}.log'
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
        file_paths: List[str],
        output_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """打包下载日志文件"""
        if not file_paths:
            return {
                'success': False,
                'error': '未选择任何文件'
            }

        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            if output_path is None:
                zip_dir = Path('/tmp/test-logs/downloads')
                zip_dir.mkdir(parents=True, exist_ok=True)
                output_path = str(zip_dir / f'logs_{timestamp}.zip')

            with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for file_path_str in file_paths:
                    file_path = Path(file_path_str)
                    if file_path.exists():
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

    def clean_old_logs(self, days: int = 7) -> Dict[str, Any]:
        """清理旧日志"""
        cleaned = 0
        total_size = 0

        cutoff_time = datetime.now().timestamp() - (days * 86400)

        for base_dir in self.log_dirs:
            base_path = Path(base_dir)
            if not base_path.exists():
                continue

            for log_file in base_path.rglob('*.log'):
                try:
                    if log_file.stat().st_mtime < cutoff_time:
                        size = log_file.stat().st_size
                        log_file.unlink()
                        cleaned += 1
                        total_size += size
                except Exception:
                    pass

        return {
            'success': True,
            'cleaned_files': cleaned,
            'freed_space_mb': round(total_size / (1024 * 1024), 2)
        }


# 全局实例
test_logs_manager = TestLogsManager()
