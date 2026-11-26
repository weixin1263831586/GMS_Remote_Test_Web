"""Data models for parsed test reports."""

from dataclasses import dataclass


@dataclass
class TestFailure:
    """测试失败信息数据类"""
    name: str
    reason: str
    module: str = '未知模块'
    stack_trace: str = ''
    raw_log: str = ''  # 原样保留匹配的 host_log FAILURE 块，供前端完整展示


@dataclass
class TestReport:
    """测试报告数据类"""
    test_type: str
    device: str
    suite_version: str      # 测试套件版本（如 16.1_r2）
    android_version: str    # Android版本（从 build_version_release 获取）
    start_time: str
    total: int
    pass_count: int
    fail_count: int
    pass_rate: str
    failures: list[TestFailure]
