#!/usr/bin/env python3
"""测试报告数据库模块 - 记录和管理每次测试情况"""

import json
import logging
import os
import threading
from collections import defaultdict
from datetime import datetime, timedelta

from foundation.config import settings


logger = logging.getLogger(__name__)


def _empty_indexes() -> dict:
    """Create a fresh index structure."""
    return {
        'timestamp': {},
        'test_type': defaultdict(list),
        'client_id': defaultdict(list),
        'status': defaultdict(list),
        'created_at': defaultdict(list),
    }


class TestReportDB:
    """测试报告数据库 - 使用 JSON 文件存储 + 内存索引"""

    def __init__(self, db_path: str | None = None):
        """
        初始化数据库

        Args:
            db_path: 数据库文件路径，默认为 test_reports.json
        """
        if db_path is None:
            db_path = str(settings.data_root / 'test_reports.json')

        self.db_path = db_path
        self.lock = threading.Lock()
        self._cache = None  # 数据缓存
        self._cache_dirty = True  # 缓存是否脏

        self._indexes = _empty_indexes()
        self._indexes_dirty = True  # 索引是否需要重建

        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        self._init_db()

    def _init_db(self):
        if not os.path.exists(self.db_path):
            self._save_data({'reports': [], 'last_update': None})

    def _load_data(self) -> dict:
        if not self._cache_dirty and self._cache is not None:
            return self._cache

        try:
            with open(self.db_path, encoding='utf-8') as f:
                data = json.load(f)
                self._cache = data
                self._cache_dirty = False
                return data
        except Exception as e:
            logger.error(f"加载数据库失败: {e}")
            data = {'reports': [], 'last_update': None}
            self._cache = data
            self._cache_dirty = False
            return data

    def _save_data(self, data: dict, invalidate_indexes: bool = True):
        try:
            with open(self.db_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            # 更新缓存
            self._cache = data
            self._cache_dirty = False
            # 只在需要时标记索引需要重建
            if invalidate_indexes:
                self._indexes_dirty = True
        except Exception as e:
            logger.error(f"保存数据库失败: {e}")

    def _build_indexes(self):
        """Rebuild in-memory indexes from cached reports if marked dirty."""
        if not self._indexes_dirty:
            return

        try:
            self._indexes = _empty_indexes()

            data = self._load_data()
            reports = data.get('reports', [])

            for report in reports:
                timestamp = report.get('timestamp')
                if not timestamp:
                    continue

                self._indexes['timestamp'][timestamp] = report

                test_type = report.get('test_type', 'UNKNOWN')
                self._indexes['test_type'][test_type].append(timestamp)

                client_id = report.get('client_id')
                if client_id:
                    self._indexes['client_id'][client_id].append(timestamp)

                status = report.get('status', 'unknown')
                self._indexes['status'][status].append(timestamp)

                created_at = report.get('created_at')
                if created_at:
                    try:
                        date = created_at.split('T')[0]
                        self._indexes['created_at'][date].append(timestamp)
                    except Exception:
                        pass

            self._indexes_dirty = False
        except Exception as e:
            logger.error(f"构建索引失败: {e}")
            # 即使索引构建失败,也要标记为已尝试,避免重复构建
            self._indexes_dirty = False

    def _invalidate_cache(self):
        self._cache_dirty = True
        self._indexes_dirty = True

    def add_report(self, report_info: dict) -> bool:
        """Insert (or update by timestamp) a test report row; return success."""
        try:
            with self.lock:
                data = self._load_data()

                existing = next((r for r in data['reports'] if r['timestamp'] == report_info['timestamp']), None)

                if existing:
                    existing.update(report_info)
                    existing['updated_at'] = datetime.now().isoformat()
                else:
                    report_info['created_at'] = datetime.now().isoformat()
                    report_info['updated_at'] = datetime.now().isoformat()
                    data['reports'].insert(0, report_info)  # newest first

                data['last_update'] = datetime.now().isoformat()
                self._save_data(data)

                logger.info(f"添加报告记录: {report_info['timestamp']} - {report_info['test_type']}")
                return True

        except Exception as e:
            logger.error(f"添加报告失败: {e}")
            return False

    def get_reports(
        self,
        limit: int = 50,
        test_type: str | None = None,
        client_id: str | None = None,
        status: str | None = None,
        user_only: str | None = None,
    ) -> list[dict]:
        """Return up to *limit* reports, optionally filtered by test_type / client_id / status / user_only."""
        try:
            # Skip index build when no filter is provided.
            if not test_type and not client_id and not status and not user_only:
                data = self._load_data()
                return data.get('reports', [])[:limit]

            self._build_indexes()

            timestamps = None

            if test_type:
                type_timestamps = set(self._indexes['test_type'].get(test_type, []))
                timestamps = type_timestamps if timestamps is None else timestamps & type_timestamps

            if client_id:
                client_timestamps = set(self._indexes['client_id'].get(client_id, []))
                timestamps = client_timestamps if timestamps is None else timestamps & client_timestamps

            if status:
                status_timestamps = set(self._indexes['status'].get(status, []))
                timestamps = status_timestamps if timestamps is None else timestamps & status_timestamps

            if user_only:
                user_timestamps = set(self._indexes['client_id'].get(user_only, []))
                timestamps = user_timestamps if timestamps is None else timestamps & user_timestamps

            if timestamps is None:
                return []

            reports = [self._indexes['timestamp'][ts] for ts in timestamps if ts in self._indexes['timestamp']]

            reports.sort(key=lambda x: x.get('created_at', ''), reverse=True)

            return reports[:limit]

        except Exception as e:
            logger.error(f"获取报告列表失败: {e}")
            return []

    def get_report_by_timestamp(self, timestamp: str) -> dict | None:
        """Return the report row for *timestamp*, or None."""
        try:
            self._build_indexes()

            return self._indexes['timestamp'].get(timestamp)

        except Exception as e:
            logger.error(f"获取报告失败: {e}")
            return None

    def update_report_status(self, timestamp: str, status: str, **kwargs) -> bool:
        """Set *status* (running/completed/failed) plus any extra *kwargs* on the *timestamp* row; return success."""
        try:
            with self.lock:
                data = self._load_data()
                report = next((r for r in data['reports'] if r['timestamp'] == timestamp), None)

                if report:
                    old_status = report.get('status')
                    old_test_type = report.get('test_type')
                    old_client_id = report.get('client_id')

                    report['status'] = status
                    report['updated_at'] = datetime.now().isoformat()
                    report.update(kwargs)

                    data['last_update'] = datetime.now().isoformat()

                    # 检查索引字段是否改变
                    indexed_fields_changed = (
                        old_status != status or
                        old_test_type != report.get('test_type') or
                        old_client_id != report.get('client_id')
                    )
                    self._save_data(data, invalidate_indexes=indexed_fields_changed)
                    return True

                return False

        except Exception as e:
            logger.error(f"更新报告状态失败: {e}")
            return False

    def delete_report(self, timestamp: str) -> bool:
        """Delete the *timestamp* report row; return success."""
        try:
            with self.lock:
                data = self._load_data()
                original_count = len(data['reports'])
                data['reports'] = [r for r in data['reports'] if r['timestamp'] != timestamp]

                if len(data['reports']) < original_count:
                    data['last_update'] = datetime.now().isoformat()
                    self._save_data(data)
                    logger.info(f"删除报告: {timestamp}")
                    return True

                return False

        except Exception as e:
            logger.error(f"删除报告失败: {e}")
            return False

    def get_statistics(self) -> dict:
        """Return aggregate {total_reports, type_counts, recent_week, last_update} across reports."""
        try:
            self._build_indexes()

            total = len(self._indexes['timestamp'])

            type_counts = {}
            for test_type, timestamps in self._indexes['test_type'].items():
                type_counts[test_type] = len(timestamps)

            week_ago = datetime.now() - timedelta(days=7)
            recent_count = 0

            for date_str, timestamps in self._indexes['created_at'].items():
                try:
                    date_obj = datetime.fromisoformat(date_str)
                    if date_obj > week_ago:
                        recent_count += len(timestamps)
                except (ValueError, TypeError):
                    pass

            # last_update isn't indexed, so reload the data file.
            data = self._load_data()

            return {
                'total_reports': total,
                'type_counts': dict(type_counts),
                'recent_week': recent_count,
                'last_update': data.get('last_update')
            }

        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {'total_reports': 0, 'type_counts': {}, 'recent_week': 0}

    def scan_and_sync_remote_reports(self, result_dirs: list[str]) -> int:
        """Scan the given test-suite *result_dirs* and insert newly found reports; return how many were added."""
        new_count = 0

        for result_dir in result_dirs:
            if not os.path.exists(result_dir):
                continue

            # 提取测试类型
            test_type = 'UNKNOWN'
            if 'android-gts' in result_dir:
                test_type = 'GTS'
            elif 'android-cts' in result_dir:
                test_type = 'CTS'
            elif 'android-sts' in result_dir:
                test_type = 'STS'
            elif 'android-vts' in result_dir:
                test_type = 'VTS'

            # 扫描时间戳目录
            try:
                for entry in os.scandir(result_dir):
                    if entry.is_dir() and entry.name[0].isdigit():
                        # 检查是否已存在
                        if self.get_report_by_timestamp(entry.name):
                            continue

                        # 尝试解析 test_result.xml
                        xml_path = os.path.join(entry.path, 'test_result.xml')
                        report_info = {
                            'timestamp': entry.name,
                            'test_type': test_type,
                            'result_dir': entry.path,
                            'status': 'completed'
                        }

                        if os.path.exists(xml_path):
                            # 解析 XML 获取详细信息
                            try:
                                from .archive import analyzer
                                result = analyzer.analyze_file(xml_path)
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
                                logger.warning(f"解析 XML 失败: {xml_path}, {e}")

                        # 添加到数据库
                        if self.add_report(report_info):
                            new_count += 1

            except Exception as e:
                logger.error(f"扫描目录失败: {result_dir}, {e}")

        return new_count


test_report_db = TestReportDB()
