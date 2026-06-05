"""报告分析辅助函数"""
import logging
import os
import re
from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, List, Optional

from core.state import global_state

logger = logging.getLogger(__name__)


# ==================== XML 分析缓存 ====================

@lru_cache(maxsize=128)
def cached_xml_analysis(xml_path: str, mtime: float) -> Dict:
    """带缓存的XML分析结果"""
    from core.report_analyzer import ReportAnalyzer
    return ReportAnalyzer().analyze_file(xml_path)


# ==================== 日志解析辅助 ====================

_FINAL_LOG_PATTERNS = [re.compile(r'process final logs:\s*(/[^\s]+)')]
_RESULT_DIR_RE = re.compile(r'RESULT DIRECTORY\s*:\s*(/[^\s]+)')


def _extract_result_dir_from_logs(user_logs: List[str]) -> Optional[str]:
    """Extract a report/log directory from Tradefed output.

    CTS/GGS commonly print RESULT DIRECTORY, while some VTS paths only appear
    as "process final logs: /tmp/.../vts/inv_x/inv_y/end_host_log_*.txt".
    Single reverse pass checks both patterns.
    """
    for log in reversed(user_logs or []):
        log_str = str(log)
        # Primary: RESULT DIRECTORY
        if 'RESULT DIRECTORY' in log_str:
            match = _RESULT_DIR_RE.search(log_str)
            if match:
                result_dir = match.group(1).strip()
                if os.path.isdir(result_dir):
                    logger.info(f"[ReportDB] 找到 RESULT DIRECTORY: {result_dir}")
                    return result_dir
        # Fallback: process final logs (VTS)
        for pattern in _FINAL_LOG_PATTERNS:
            match = pattern.search(log_str)
            if match:
                path = match.group(1).strip()
                candidate_dir = path if os.path.isdir(path) else os.path.dirname(path)
                if candidate_dir and os.path.isdir(candidate_dir):
                    logger.info(f"[ReportDB] 从 final logs 推导报告目录: {candidate_dir}")
                    return candidate_dir

    return None


def _apply_report_stats(report_info: Dict, details: Dict, summary: Dict, report_type: str = ''):
    """Update report_info with pass/fail/total/pass_rate and optional extras from analysis."""
    report_info.update({
        'pass': summary.get('pass', 0),
        'fail': summary.get('fail', 0),
        'total': summary.get('total', 0),
        'pass_rate': summary.get('pass_rate', '0%'),
    })
    if report_type:
        report_info['report_type'] = report_type
    if details.get('device'):
        report_info['device'] = details['device']
    if details.get('start_time'):
        report_info['start_time'] = details['start_time']
    if details.get('suite_version'):
        report_info['suite_version'] = details['suite_version']
    if details.get('android_version'):
        report_info['android_version'] = details['android_version']


def _build_report_timestamp(result_dir: str) -> str:
    """从结果目录名提取时间戳，回退到当前时间"""
    basename = os.path.basename(result_dir.rstrip('/'))
    if basename:
        return basename
    return datetime.now().strftime('%Y.%m.%d_%H.%M.%S')


# ==================== 报告数据库保存 ====================

def save_test_report_to_db(
    client_id: str,
    config: Dict[str, Any],
    test_params: Dict[str, Any],
    user_logs: List[str]
) -> Optional[str]:
    """
    从测试日志中提取 RESULT DIRECTORY 并记录测试报告到数据库

    Args:
        client_id: 客户端ID
        config: 配置字典
        test_params: 测试参数
        user_logs: 用户日志列表

    Returns:
        报告时间戳，如果失败则返回 None
    """
    from core.clients import parse_client_id
    from core.report_analyzer import ReportAnalyzer
    from core.test_report_db import test_report_db

    try:
        result_dir = _extract_result_dir_from_logs(user_logs)

        if not result_dir or not os.path.exists(result_dir):
            logger.warning(f"[ReportDB] 未找到 RESULT DIRECTORY 或目录不存在: {result_dir}")
            return None

        # 提取时间戳
        timestamp = _build_report_timestamp(result_dir)

        # 检查是否已记录
        existing = test_report_db.get_report_by_timestamp(timestamp)
        if existing:
            logger.info(f"[ReportDB] 报告已存在: {timestamp}")
            return timestamp

        # 解析 test_result.xml
        xml_path = os.path.join(result_dir, 'test_result.xml')
        report_info = {
            'timestamp': timestamp,
            'test_type': test_params.get('test_type', 'UNKNOWN').upper(),
            'test_module': test_params.get('test_module', ''),
            'test_case': test_params.get('test_case', ''),
            'client_id': client_id,
            'devices': test_params.get('devices', []),
            'result_dir': result_dir,
            'suite_path': test_params.get('test_suite', ''),
            'status': 'completed'
        }

        # 提取用户名
        if '@' in client_id:
            report_info['user'] = parse_client_id(client_id)[0]

        # 解析 XML 获取测试结果统计（使用缓存）；VTS 可能没有 test_result.xml，回退解析 host log。
        if os.path.exists(xml_path):
            try:
                stat = os.stat(xml_path)
                result = cached_xml_analysis(xml_path, stat.st_mtime)
                if result:
                    _apply_report_stats(report_info, result['details'], result['summary'])
            except Exception as e:
                logger.warning(f"[ReportDB] 解析 XML 失败: {e}")
        else:
            try:
                result = ReportAnalyzer().analyze_log_dir(result_dir)
                if result:
                    _apply_report_stats(report_info, result.get('details', {}), result.get('summary', {}), report_type='host_log')
                    details = result.get('details', {})
                    if details.get('test_type') and report_info.get('test_type') == 'UNKNOWN':
                        report_info['test_type'] = details.get('test_type')
                    logger.info(f"[ReportDB] 已从 host log 解析报告统计: {result_dir}")
                else:
                    _apply_report_stats(report_info, {}, {}, report_type='host_log')
                    logger.info(f"[ReportDB] 未找到 XML，记录 VTS/host_log 目录占位报告: {result_dir}")
            except Exception as e:
                logger.warning(f"[ReportDB] 解析 host log 失败: {e}")

        if test_report_db.add_report(report_info):
            logger.info(f"[ReportDB] 报告已记录: {timestamp}")
            return timestamp

        return None

    except Exception as e:
        logger.error(f"[ERROR] 保存报告到数据库失败: {e}")
        return None
