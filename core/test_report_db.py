#!/usr/bin/env python3
"""测试报告数据库模块 - 记录和管理每次测试情况"""

import json
import os
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import defaultdict


class TestReportDB:
    """测试报告数据库 - 使用 JSON 文件存储 + 内存索引"""

    def __init__(self, db_path: str = None):
        """
        初始化数据库

        Args:
            db_path: 数据库文件路径，默认为 reports/test_reports.json
        """
        if db_path is None:
            base_dir = os.path.dirname(os.path.dirname(__file__))
            db_path = os.path.join(base_dir, 'data', 'test_reports.json')

        self.db_path = db_path
        self.lock = threading.Lock()
        self._cache = None  # 数据缓存
        self._cache_dirty = True  # 缓存是否脏

        # 内存索引
        self._indexes = {
            'timestamp': {},  # timestamp -> report
            'test_type': defaultdict(list),  # test_type -> [timestamps]
            'client_id': defaultdict(list),  # client_id -> [timestamps]
            'status': defaultdict(list),  # status -> [timestamps]
            'created_at': defaultdict(list)  # date -> [timestamps]
        }
        self._indexes_dirty = True  # 索引是否需要重建

        # 确保数据目录存在
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        # 初始化数据库文件
        self._init_db()

    def _init_db(self):
        """初始化数据库文件"""
        if not os.path.exists(self.db_path):
            self._save_data({'reports': [], 'last_update': None})

    def _load_data(self) -> Dict:
        """加载数据(带缓存)"""
        # 如果缓存有效,直接返回
        if not self._cache_dirty and self._cache is not None:
            return self._cache

        try:
            with open(self.db_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self._cache = data
                self._cache_dirty = False
                return data
        except Exception as e:
            print(f"[ERROR] 加载数据库失败: {e}")
            data = {'reports': [], 'last_update': None}
            self._cache = data
            self._cache_dirty = False
            return data

    def _save_data(self, data: Dict, invalidate_indexes: bool = True):
        """保存数据"""
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
            print(f"[ERROR] 保存数据库失败: {e}")

    def _build_indexes(self):
        """构建内存索引"""
        if not self._indexes_dirty:
            return

        try:
            # 清空索引
            self._indexes = {
                'timestamp': {},
                'test_type': defaultdict(list),
                'client_id': defaultdict(list),
                'status': defaultdict(list),
                'created_at': defaultdict(list)
            }

            data = self._load_data()
            reports = data.get('reports', [])

            for report in reports:
                timestamp = report.get('timestamp')
                if not timestamp:
                    continue

                # 时间戳索引
                self._indexes['timestamp'][timestamp] = report

                # 测试类型索引
                test_type = report.get('test_type', 'UNKNOWN')
                self._indexes['test_type'][test_type].append(timestamp)

                # 客户端ID索引
                client_id = report.get('client_id')
                if client_id:
                    self._indexes['client_id'][client_id].append(timestamp)

                # 状态索引
                status = report.get('status', 'unknown')
                self._indexes['status'][status].append(timestamp)

                # 创建日期索引
                created_at = report.get('created_at')
                if created_at:
                    try:
                        date = created_at.split('T')[0]  # 提取日期部分
                        self._indexes['created_at'][date].append(timestamp)
                    except:
                        pass

            self._indexes_dirty = False
        except Exception as e:
            print(f"[ERROR] 构建索引失败: {e}")
            # 即使索引构建失败,也要标记为已尝试,避免重复构建
            self._indexes_dirty = False

    def _invalidate_cache(self):
        """使缓存失效"""
        self._cache_dirty = True
        self._indexes_dirty = True

    def add_report(self, report_info: Dict) -> bool:
        """
        添加测试报告记录

        Args:
            report_info: 报告信息字典，包含：
                - timestamp: 时间戳 (YYYY.MM.DD_HH.MM.SS.mmm_pid)
                - test_type: 测试类型 (CTS, GTS, etc.)
                - client_id: 客户端ID
                - user: 用户名
                - devices: 设备列表
                - result_dir: RESULT DIRECTORY 路径
                - pass_count: 通过数量
                - fail_count: 失败数量
                - total: 总数量
                - pass_rate: 通过率
                - suite_path: 测试套件路径
                - start_time: 测试开始时间

        Returns:
            bool: 是否添加成功
        """
        try:
            with self.lock:
                data = self._load_data()

                # 检查是否已存在相同时间戳的报告
                existing = next((r for r in data['reports'] if r['timestamp'] == report_info['timestamp']), None)

                if existing:
                    # 更新现有报告
                    existing.update(report_info)
                    existing['updated_at'] = datetime.now().isoformat()
                else:
                    # 添加新报告
                    report_info['created_at'] = datetime.now().isoformat()
                    report_info['updated_at'] = datetime.now().isoformat()
                    data['reports'].insert(0, report_info)  # 最新的在前面

                data['last_update'] = datetime.now().isoformat()
                self._save_data(data)

                print(f"[ReportDB] 添加报告记录: {report_info['timestamp']} - {report_info['test_type']}")
                return True

        except Exception as e:
            print(f"[ERROR] 添加报告失败: {e}")
            return False

    def get_reports(
        self,
        limit: int = 50,
        test_type: str = None,
        client_id: str = None,
        status: str = None,
        user_only: str = None
    ) -> List[Dict]:
        """
        获取测试报告列表

        Args:
            limit: 返回数量限制
            test_type: 过滤测试类型 (可选)
            client_id: 过滤客户端ID (可选)
            status: 过滤状态 (可选)
            user_only: 仅显示指定用户的报告 (可选)

        Returns:
            List[Dict]: 报告列表
        """
        try:
            # 如果没有过滤条件,直接返回(避免构建索引)
            if not test_type and not client_id and not status and not user_only:
                data = self._load_data()
                return data.get('reports', [])[:limit]

            # 有过滤条件时才构建和使用索引
            self._build_indexes()

            # 使用索引查找
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
                # 过滤当前用户的报告
                user_timestamps = set(self._indexes['client_id'].get(user_only, []))
                timestamps = user_timestamps if timestamps is None else timestamps & user_timestamps

            if timestamps is None:
                return []

            # 根据时间戳获取报告
            reports = [self._indexes['timestamp'][ts] for ts in timestamps if ts in self._indexes['timestamp']]

            # 按时间倒序排序
            reports.sort(key=lambda x: x.get('created_at', ''), reverse=True)

            return reports[:limit]

        except Exception as e:
            print(f"[ERROR] 获取报告列表失败: {e}")
            return []

    def get_report_by_timestamp(self, timestamp: str) -> Optional[Dict]:
        """
        根据时间戳获取报告

        Args:
            timestamp: 报告时间戳

        Returns:
            Optional[Dict]: 报告信息，不存在返回 None
        """
        try:
            # 确保索引已构建
            self._build_indexes()

            # 直接从索引获取
            return self._indexes['timestamp'].get(timestamp)

        except Exception as e:
            print(f"[ERROR] 获取报告失败: {e}")
            return None

    def update_report_status(self, timestamp: str, status: str, **kwargs) -> bool:
        """
        更新报告状态

        Args:
            timestamp: 报告时间戳
            status: 状态 (running, completed, failed)
            **kwargs: 其他更新字段

        Returns:
            bool: 是否更新成功
        """
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
            print(f"[ERROR] 更新报告状态失败: {e}")
            return False

    def delete_report(self, timestamp: str) -> bool:
        """
        删除报告记录

        Args:
            timestamp: 报告时间戳

        Returns:
            bool: 是否删除成功
        """
        try:
            with self.lock:
                data = self._load_data()
                original_count = len(data['reports'])
                data['reports'] = [r for r in data['reports'] if r['timestamp'] != timestamp]

                if len(data['reports']) < original_count:
                    data['last_update'] = datetime.now().isoformat()
                    self._save_data(data)
                    print(f"[ReportDB] 删除报告: {timestamp}")
                    return True

                return False

        except Exception as e:
            print(f"[ERROR] 删除报告失败: {e}")
            return False

    def get_statistics(self) -> Dict:
        """
        获取统计信息

        Returns:
            Dict: 统计信息
        """
        try:
            # 确保索引已构建
            self._build_indexes()

            # 使用索引统计,避免重复加载
            total = len(self._indexes['timestamp'])

            # 使用索引统计测试类型
            type_counts = {}
            for test_type, timestamps in self._indexes['test_type'].items():
                type_counts[test_type] = len(timestamps)

            # 最近7天的报告
            week_ago = datetime.now() - timedelta(days=7)
            recent_count = 0

            for date_str, timestamps in self._indexes['created_at'].items():
                try:
                    date_obj = datetime.fromisoformat(date_str)
                    if date_obj > week_ago:
                        recent_count += len(timestamps)
                except:
                    pass

            # 获取last_update需要重新加载数据
            data = self._load_data()

            return {
                'total_reports': total,
                'type_counts': dict(type_counts),
                'recent_week': recent_count,
                'last_update': data.get('last_update')
            }

        except Exception as e:
            print(f"[ERROR] 获取统计信息失败: {e}")
            return {'total_reports': 0, 'type_counts': {}, 'recent_week': 0}

    def scan_and_sync_remote_reports(self, result_dirs: List[str]) -> int:
        """
        扫描远程测试套件 results 目录并同步到本地数据库

        Args:
            result_dirs: results 目录列表，如：
                ['~/GMS-Suite/android-gts/android-gts/results',
                 '~/GMS-Suite/android-cts/android-cts/results']

        Returns:
            int: 新增的报告数量
        """
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
                                from .report_analyzer import analyzer
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
                                print(f"[WARN] 解析 XML 失败: {xml_path}, {e}")

                        # 添加到数据库
                        if self.add_report(report_info):
                            new_count += 1

            except Exception as e:
                print(f"[ERROR] 扫描目录失败: {result_dir}, {e}")

        return new_count


# 全局实例
test_report_db = TestReportDB()
