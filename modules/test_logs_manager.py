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
        self.log_dirs = [
            '/tmp/xts-root-dir',
            '/tmp/test-logs',
            '/home/hcq/Logs'
        ]

    def list_log_files(self) -> Dict[str, Any]:
        """列出所有日志文件"""
        log_files = []

        for base_dir in self.log_dirs:
            if not os.path.exists(base_dir):
                continue

            for root, dirs, files in os.walk(base_dir):
                for file in files:
                    if file.endswith('.log') or file.endswith('.txt'):
                        file_path = os.path.join(root, file)
                        stat = os.stat(file_path)
                        log_files.append({
                            'name': file,
                            'path': file_path,
                            'size': stat.st_size,
                            'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                            'base_dir': base_dir
                        })

        # 按修改时间倒序
        log_files.sort(key=lambda x: x['modified'], reverse=True)

        return {
            'success': True,
            'total': len(log_files),
            'files': log_files[:100]  # 限制返回100个
        }

    def get_log_file(self, file_path: str, max_lines: int = 1000) -> Dict[str, Any]:
        """读取日志文件内容"""
        if not os.path.exists(file_path):
            return {
                'success': False,
                'error': f'文件不存在: {file_path}'
            }

        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()

            # 返回最后 max_lines 行
            content = ''.join(lines[-max_lines:]) if len(lines) > max_lines else ''.join(lines)

            return {
                'success': True,
                'file': file_path,
                'total_lines': len(lines),
                'returned_lines': len(content.split('\n')),
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
            # 创建保存目录
            save_dir = '/tmp/test-logs/saved'
            os.makedirs(save_dir, exist_ok=True)

            # 生成文件名
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'test_log_{client_id}_{timestamp}.log'
            file_path = os.path.join(save_dir, filename)

            # 保存文件
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(log_content)

            return {
                'success': True,
                'file_path': file_path,
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
            # 创建临时zip文件
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            if output_path is None:
                zip_dir = '/tmp/test-logs/downloads'
                os.makedirs(zip_dir, exist_ok=True)
                output_path = os.path.join(zip_dir, f'logs_{timestamp}.zip')

            with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for file_path in file_paths:
                    if os.path.exists(file_path):
                        # 使用相对路径作为zip内文件名
                        arcname = os.path.basename(file_path)
                        zipf.write(file_path, arcname)

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
            if not os.path.exists(base_dir):
                continue

            for root, dirs, files in os.walk(base_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    if os.path.getmtime(file_path) < cutoff_time:
                        try:
                            size = os.path.getsize(file_path)
                            os.remove(file_path)
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
