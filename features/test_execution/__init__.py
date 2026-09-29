"""Test execution feature package."""

from .models import (
    SuiteApkAnalyzeRequest,
    TestParseArgsRequest,
    TestStartRequest,
    TradefedListResultsRequest,
)
from .suite_helpers import get_available_test_suites
from .suite_modules import search_latest_suite_modules
from .suites import detect_test_type_from_suite_path, get_default_suites_path
from .tradefed import execute_tradefed_command, find_tradefed_binary, parse_tradefed_list_results
from .tradefed_results import extract_project_from_result_fields


_LAZY_API_EXPORTS = {
    'create_suite_apk_analysis_task',
    'start_test',
}


def __getattr__(name: str):
    if name in _LAZY_API_EXPORTS:
        from . import api

        value = getattr(api, name)
        globals()[name] = value
        return value
    if name == 'runtime':
        # 组合接缝：bootstrap 通过 runtime.configure_* 注入 ssh_manager /
        # config_manager（features.test_execution.runtime）。导出模块本身
        # 作为公共面，weekly_report 等跨特性消费方不再绕过 __init__ 直引
        # 子模块。
        from . import runtime as runtime_module

        globals()[name] = runtime_module
        return runtime_module
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


__all__ = [
    "SuiteApkAnalyzeRequest",
    "TestParseArgsRequest",
    "TestStartRequest",
    "TradefedListResultsRequest",
    "create_suite_apk_analysis_task",
    "detect_test_type_from_suite_path",
    "execute_tradefed_command",
    "extract_project_from_result_fields",
    "find_tradefed_binary",
    "get_available_test_suites",
    "get_default_suites_path",
    "parse_tradefed_list_results",
    "runtime",
    "search_latest_suite_modules",
    "start_test",
]
