"""Latest GTS lookup spans the account registry and every permitted native suite."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from features.assistant import report_query
from features.assistant.executor import ActionExecutor
from features.assistant.tests.test_agent_session_safety import _agent_request
from features.auth import CurrentUser
from features.cluster import api as cluster_api
from features.reports import TestReportDB as ReportRepository
from features.reports import files_api
from worker_agent.config import WorkerConfig
from worker_agent.suite_actions import execute_suite_action
from worker_agent.tests.test_report_summaries import make_result


@pytest.fixture
def native_environment(tmp_path, monkeypatch):
    repository = ReportRepository(str(tmp_path / "reports.sqlite3"))
    monkeypatch.setattr(files_api, "test_report_db", repository)
    root = tmp_path / "GMS-Suite"
    tools = root / "android-gts-14-R4" / "android-gts" / "tools"
    tools.mkdir(parents=True)
    run = make_result(tools.parent, "2026.11.13_14.22.45.723_8397")
    suites = [{"worker_id": "worker-1", "suite_type": "gts", "suite_version": "14_r4", "tools_path": str(tools)}]
    service = SimpleNamespace(config=SimpleNamespace(local_worker_id="worker-1"),
                              repository=SimpleNamespace(list_suites=lambda worker: suites if not worker or worker == "worker-1" else []))
    monkeypatch.setattr(cluster_api, "service", lambda: service)
    monkeypatch.setattr(cluster_api, "_require_cluster_enabled", lambda **kwargs: None)
    worker = WorkerConfig(worker_id="worker-1", controller_url="https://controller", token="test-token", suite_roots=[root])

    async def dispatch(worker_id, command_type, payload, **kwargs):
        assert worker_id == "worker-1" and command_type == "suite_action"
        return execute_suite_action(worker, payload)

    queue = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr(cluster_api, "_run_worker_command", queue)
    principal = CurrentUser(id="owner-1", username="reader", role="user")
    request = _agent_request("owner-1")
    request.state.current_user = principal
    return repository, request, queue, run, suites


@pytest.mark.asyncio
async def test_empty_registry_finds_the_actual_gts_directory(native_environment):
    _, request, queue, run, _ = native_environment
    outcome = await ActionExecutor().execute({}, request, "reports_list", {
        "test_type": "GTS", "latest": True, "limit": 1, "worker_id": "worker-1",
    })
    assert outcome.success and outcome.data["latest_confirmed"]
    assert outcome.data["registered_count"] == 0
    report = outcome.data["reports"][0]
    assert report["result_dir"] == str(run) and report["source"] == "native_suite"
    assert report["test_type"] == "GTS" and report["total"] == 1264
    assert report["modules_done"] == "186" and report["modules_total"] == "188"
    assert "暂无报告" not in outcome.formatted_text
    assert queue.await_args.args[2]["action"] == "report_summaries"
    assert report["access"]["report_management"] is False
    assert report["access"]["saved_report_analysis"] is False
    assert report["access"]["failure_details_loaded"] is False
    assert "report_url" not in report
    assert report["browse_url"].startswith("#test-suites?")
    assert outcome.entities["reports"] == []
    assert outcome.page == "test-suites"
    assert outcome.quick_actions == [{"label": "查看原生报告目录", "page": "test-suites", "params": {
        "worker_id": "worker-1", "suite_path": str(run.parent.parent / "tools"),
        "path": f"results/{run.name}",
    }}]


@pytest.mark.asyncio
async def test_latest_compares_all_matching_suite_versions(native_environment, tmp_path):
    _, request, queue, _, suites = native_environment
    older = tmp_path / "GMS-Suite" / "android-gts-15-r1" / "android-gts" / "tools"
    older.mkdir(parents=True)
    make_result(older.parent, "2026.11.12_10.00.00.000_1234", start=1794464573770)
    suites.insert(0, {"worker_id": "worker-1", "suite_type": "gts", "tools_path": str(older)})
    outcome = await report_query.query_reports({}, request, {"test_type": "gts", "latest": True, "limit": 1, "worker_id": "worker-1"})
    assert outcome.data["reports"][0]["suite_version"] == "14_r4"
    assert queue.await_count == 2 and outcome.data["native_scanned"] == 2


@pytest.mark.asyncio
async def test_native_failure_never_turns_into_no_reports_or_confirmed_latest(native_environment):
    _, request, queue, _, _ = native_environment
    from foundation.error_model import ApiError

    queue.side_effect = ApiError.dependency_timeout("Worker response timed out")
    outcome = await report_query.query_reports({}, request, {"test_type": "GTS", "latest": True, "worker_id": "worker-1"})
    assert not outcome.success and not outcome.data["coverage_complete"]
    assert not outcome.data["latest_confirmed"]
    assert "Worker response timed out" in json.dumps(outcome.data)
    assert "暂无报告" not in outcome.formatted_text


@pytest.mark.asyncio
async def test_missing_reports_read_scope_is_denied_before_native_reads(native_environment):
    _, request, queue, _, _ = native_environment
    request.state.current_user = CurrentUser(id="agent:t1", username="agent", role="agent_service", resource_owner_id="owner-1")
    outcome = await report_query.query_reports({}, request, {"latest": True, "worker_id": "worker-1"})
    assert not outcome.success and "reports.read" in outcome.error
    queue.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_only_does_not_claim_ownership_of_unregistered_native_reports(native_environment):
    _, request, queue, _, _ = native_environment
    outcome = await report_query.query_reports({}, request, {"latest": True, "user_only": True, "worker_id": "worker-1"})
    assert outcome.success and not outcome.data["reports"]
    assert outcome.data["native_scanned"] is None
    queue.assert_not_awaited()


def test_explicit_user_type_and_latest_survive_model_omissions():
    assert report_query.apply_report_goal_params("reports_list", {}, "最新GTS测试报告") == {
        "test_type": "GTS", "latest": True, "limit": 1,
    }
    assert report_query.apply_report_goal_params("reports_list", {"test_type": "CTS"}, "最新GTS测试报告")["test_type"] == "GTS"
    assert report_query.apply_report_goal_params("devices_list", {}, "最新GTS测试报告") == {}


@pytest.mark.asyncio
async def test_registration_pages_cannot_hide_a_later_test(native_environment):
    repository, request, _, _, _ = native_environment
    for index in range(102):
        repository.add_report({"report_id": f"report-{index:03}", "owner_id": "owner-1", "test_type": "GTS",
                               "worker_id": "worker-1", "timestamp": f"uploaded-{index}",
                               "start_ms": 1794550973770 + index,
                               "created_at": f"2026-11-{29 - index // 24:02}T{index % 24:02}:00:00+00:00"})
    outcome = await report_query.query_reports({}, request, {"test_type": "GTS", "latest": True, "limit": 1, "worker_id": "worker-1"})
    assert outcome.data["registered_count"] == 102
    assert outcome.data["reports"][0]["report_id"] == "report-101"
    assert outcome.data["latest_confirmed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("role,count", [("user", 1), ("admin", 2)])
async def test_registry_filters_type_worker_and_account_before_limit(native_environment, role, count):
    repository, request, queue, _, _ = native_environment
    request.state.current_user = CurrentUser(id="owner-1", username="reader", role=role)
    for owner, kind, worker, name in [("owner-1", "GTS", "worker-1", "mine"), ("owner-2", "GTS", "worker-1", "other"),
                                       ("owner-1", "CTS", "worker-1", "cts"), ("owner-1", "GTS", "worker-2", "remote")]:
        repository.add_report({"owner_id": owner, "test_type": kind, "worker_id": worker, "timestamp": name})
    outcome = await report_query.query_reports({}, request, {"test_type": "gts", "worker_id": "worker-1"})
    assert outcome.success and len(outcome.data["reports"]) == count
    assert {r["timestamp"] for r in outcome.data["reports"]} == ({"mine", "other"} if role == "admin" else {"mine"})
    assert outcome.page == "reports"
    assert all(r["access"]["report_management"] for r in outcome.data["reports"])
    assert all(a["page"] == "reports" for a in outcome.quick_actions)
    queue.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_suites_scope_is_an_explicit_gap_not_an_empty_native_inventory(native_environment):
    _, request, queue, _, _ = native_environment
    request.state.current_user = CurrentUser(id="agent:t1", username="agent", role="agent_service",
                                            resource_owner_id="owner-1", extra_permissions=frozenset({"reports.read"}))
    outcome = await report_query.query_reports({}, request, {"test_type": "GTS", "latest": True, "worker_id": "worker-1"})
    assert not outcome.success and not outcome.data["latest_confirmed"]
    assert "suites.read" in json.dumps(outcome.data)
    queue.assert_not_awaited()


def test_unparseable_test_time_never_uses_upload_time_as_evidence_of_latest():
    assert report_query.report_time({"start_time": "未知时间", "timestamp": "2026.11.14_01.02.03.000_1234"}) is None
