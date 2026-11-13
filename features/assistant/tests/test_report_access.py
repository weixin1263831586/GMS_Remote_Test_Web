"""Navigation capabilities come from registration and actual suite locators."""

from urllib.parse import parse_qs

import pytest

from features.assistant.execution_result import ToolResult
from features.assistant.report_access import bind_report_access
from features.assistant.response import generate


def test_native_result_uses_the_existing_suite_browser_route_and_escaped_parameters():
    report = {"registered": False, "worker_id": "worker&1", "suite_path": "/suites/GTS #1/tools",
              "result_directory": "GTSRUN~1", "report_url": "/api/cluster/suites/file?invented"}
    action = bind_report_access(report)
    assert action["page"] == "test-suites"
    query = parse_qs(report["browse_url"].split("?", 1)[1])
    assert query == {"worker_id": ["worker&1"], "suite_path": ["/suites/GTS #1/tools"], "path": ["results/GTSRUN~1"]}
    assert "report_url" not in report and not report["access"]["saved_report_analysis"]
    response = generate(ToolResult(success=True, tool_name="reports_list", page="test-suites", quick_actions=[action]))
    assert all(item["page"] != "reports" for item in response.quick_actions)


@pytest.mark.parametrize("directory", ["", "..", "../private", "nested/run", "nested\\run"])
def test_missing_or_unsafe_native_locator_never_gets_a_guessed_navigation(directory):
    report = {"registered": False, "worker_id": "worker", "suite_path": "/suites/tools", "result_directory": directory,
              "report_url": "/guessed-api", "browse_url": "#reports"}
    assert bind_report_access(report) is None
    assert "browse_url" not in report
    assert "report_url" not in report
    assert not report["access"]["report_management"]


def test_registered_reports_keep_the_management_entry():
    report = {"registered": True, "report_id": "r1", "timestamp": "t1", "worker_id": "worker"}
    assert bind_report_access(report)["page"] == "reports"
    assert report["access"]["saved_report_analysis"]
