"""Test execution feature package."""

from .models import (
    SuiteApkAnalyzeRequest,
    TestParseArgsRequest,
    TestStartRequest,
    TradefedListResultsRequest,
)
from .suite_helpers import _get_available_test_suites
from .suite_modules import search_latest_suite_modules
from .suites import detect_test_type_from_suite_path, get_default_suites_path
from .tradefed import execute_tradefed_command, find_tradefed_binary, parse_tradefed_list_results
from .tradefed_results import extract_project_from_result_fields


_LAZY_API_EXPORTS = {
    'create_suite_apk_analysis_task',
    'start_test',
}


def __getattr__(name: str):
    if name not in _LAZY_API_EXPORTS:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    from . import api

    value = getattr(api, name)
    globals()[name] = value
    return value


__all__ = [
    "SuiteApkAnalyzeRequest",
    "TestParseArgsRequest",
    "TestStartRequest",
    "TradefedListResultsRequest",
    "_get_available_test_suites",
    "create_suite_apk_analysis_task",
    "detect_test_type_from_suite_path",
    "execute_tradefed_command",
    "extract_project_from_result_fields",
    "find_tradefed_binary",
    "get_default_suites_path",
    "parse_tradefed_list_results",
    "search_latest_suite_modules",
    "start_test",
]
