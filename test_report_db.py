#!/usr/bin/env python3
"""测试报告数据库模块 - 记录和管理每次测试情况"""

import json
import os
import threading
from datetime import datetime
from typing import Dict, List, Optional


class TestReportDB:
    """测试报告数据库 - 使用 JSON 文件存储"""

    def __init__(self, db_path: str = None):
        """
        初始化数据库

        Args:
            db_path: 数据库文件路径，默认为 web_app/data/test_reports.json
        """
        if db_path is None:
            # 默认路径：web_app/data/test_reports.json
            base_dir = os.path.dirname(__file__)
            db_path = os.path.join(base_dir, 'data', 'test_reports.json')

        self.db_path = db_path
        self.lock = threading.Lock()

        # 确保数据目录存在
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        # 初始化数据库文件
        self._init_db()

    def _init_db(self):
        """初始化数据库文件"""
        if not os.path.exists(self.db_path):
            self._save_data({'reports': [], 'last_update': None})

    def _load_data(self) -> Dict:
        """加载数据"""
        try:
            with open(self.db_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"[ERROR] 加载数据库失败: {e}")
            return {'reports': [], 'last_update': None}

    def _save_data(self, data: Dict):
        """保存数据"""
        try:
            with open(self.db_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[ERROR] 保存数据库失败: {e}")

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

    def get_reports(self, limit: int = 50, test_type: str = None) -> List[Dict]:
        """
        获取测试报告列表

        Args:
            limit: 返回数量限制
            test_type: 过滤测试类型 (可选)

        Returns:
            List[Dict]: 报告列表
        """
        try:
            data = self._load_data()
            reports = data.get('reports', [])

            if test_type:
                reports = [r for r in reports if r.get('test_type') == test_type]

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
            data = self._load_data()
            return next((r for r in data['reports'] if r['timestamp'] == timestamp), None)

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
                    report['status'] = status
                    report['updated_at'] = datetime.now().isoformat()
                    report.update(kwargs)

                    data['last_update'] = datetime.now().isoformat()
                    self._save_data(data)
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
            data = self._load_data()
            reports = data.get('reports', [])

            total = len(reports)

            # 按测试类型统计
            type_counts = {}
            for r in reports:
                t = r.get('test_type', 'UNKNOWN')
                type_counts[t] = type_counts.get(t, 0) + 1

            # 最近7天的报告
            from datetime import timedelta
            week_ago = datetime.now() - timedelta(days=7)
            recent_reports = [r for r in reports if datetime.fromisoformat(r.get('created_at', '')) > week_ago]

            return {
                'total_reports': total,
                'type_counts': type_counts,
                'recent_week': len(recent_reports),
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
                ['/home/hcq/GMS-Suite/android-gts-13.1-R1/android-gts/results',
                 '/home/hcq/GMS-Suite/android-cts-16_r3-1/android-cts/results']

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
                                from report_analyzer import analyzer
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
