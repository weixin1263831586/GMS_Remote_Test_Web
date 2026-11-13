"""Native report headers remain discoverable without the registration database."""

import pytest

from worker_agent.config import WorkerConfig
from worker_agent.report_summaries import native_report_summaries
from worker_agent.suite_actions import execute_suite_action


def make_result(root, directory, *, start=1794550973770, test_type="GTS", padding=""):
    run = root / "results" / directory
    run.mkdir(parents=True)
    (run / "test_result.xml").write_text(
        f'<Result suite_name="{test_type}" suite_version="14_r4" start="{start}" '
        'start_display="Fri Nov 13 14:22:53 CST 2026" end_display="Fri Nov 13 15:21:01 CST 2026" '
        f'devices="RK3326GMS1">{padding}<Summary pass="1236" failed="28" '
        'modules_done="186" modules_total="188"/></Result>', encoding="utf-8",
    )
    return run


def test_all_headers_are_compared_before_limit_and_type_filter(tmp_path):
    make_result(tmp_path, "2026.11.13_14.22.45.723_8397")
    make_result(tmp_path, "2026.11.14_10.00.00.000_1234", start=1, test_type="CTS")
    make_result(tmp_path, "2026.11.15_10.00.00.000_1234", start=2)
    result = native_report_summaries(tmp_path, test_type="GTS", limit=1)
    assert result["reports"][0]["result_directory"] == "2026.11.13_14.22.45.723_8397"
    assert result["scanned_results"] == 3 and result["count"] == 2
    assert result["coverage_complete"] and not result["errors"]
    assert result["reports"][0]["pass"] == 1236 and result["reports"][0]["fail"] == 28


def test_large_metadata_prefix_is_not_skipped(tmp_path):
    make_result(tmp_path, "2026.11.13_14.22.45.723_8397", padding="<!--" + "x" * (3 * 1024 * 1024) + "-->")
    result = native_report_summaries(tmp_path, test_type="GTS")
    assert result["count"] == 1 and result["coverage_complete"]


def test_invalid_or_unreadable_report_is_a_coverage_gap(tmp_path):
    make_result(tmp_path, "2026.11.13_14.22.45.723_8397")
    broken = make_result(tmp_path, "2026.11.14_10.00.00.000_1234")
    (broken / "test_result.xml").write_text("<Result>", encoding="utf-8")
    result = native_report_summaries(tmp_path, test_type="GTS", limit=1)
    assert result["count"] == 1 and not result["coverage_complete"]
    assert result["errors"][0]["result_directory"] == broken.name


def test_symlink_and_entity_reports_cannot_read_outside_the_suite(tmp_path):
    suite = tmp_path / "suite"
    outside = tmp_path / "outside"
    outside.mkdir()
    result = make_result(suite, "2026.11.13_14.22.45.723_8397")
    (result / "test_result.xml").unlink()
    (outside / "test_result.xml").write_text("secret", encoding="utf-8")
    (result / "test_result.xml").symlink_to(outside / "test_result.xml")
    assert not native_report_summaries(suite)["coverage_complete"]
    (result / "test_result.xml").unlink()
    (result / "test_result.xml").write_text(
        '<!DOCTYPE Result [<!ENTITY leak SYSTEM "file:///etc/passwd">]>'
        '<Result suite_name="GTS" start="1"><Summary pass="0" failed="0"/></Result>',
        encoding="utf-8",
    )
    assert not native_report_summaries(suite)["coverage_complete"]


def test_typed_worker_action_preserves_configured_root_boundary(tmp_path):
    suite = tmp_path / "android-gts"
    tools = suite / "tools"
    tools.mkdir(parents=True)
    make_result(suite, "2026.11.13_14.22.45.723_8397")
    worker = WorkerConfig(worker_id="worker", controller_url="https://controller", token="test-token", suite_roots=[tmp_path])
    payload = {"action": "report_summaries", "suite_path": str(tools), "test_type": "GTS", "limit": 1}
    assert execute_suite_action(worker, payload)["count"] == 1
    with pytest.raises(ValueError, match="outside configured roots"):
        execute_suite_action(worker, {**payload, "suite_path": str(tmp_path.parent / "other")})


def test_empty_results_are_complete_metadata_coverage(tmp_path):
    assert native_report_summaries(tmp_path)["coverage_complete"]
    assert native_report_summaries(tmp_path)["reports"] == []


def test_real_xml_is_discoverable_under_a_nonstandard_directory_name(tmp_path):
    make_result(tmp_path, "GTSRUN~1")
    result = native_report_summaries(tmp_path, test_type="GTS")
    assert result["coverage_complete"] and result["count"] == 1
    assert result["reports"][0]["result_directory"] == "GTSRUN~1"


def test_latest_alias_does_not_duplicate_a_physical_report(tmp_path):
    run = make_result(tmp_path, "2026.11.13_14.22.45.723_8397")
    (tmp_path / "results" / "latest").symlink_to(run, target_is_directory=True)
    result = native_report_summaries(tmp_path, test_type="GTS")
    assert result["count"] == result["scanned_results"] == 1
    assert result["reports"][0]["result_directory"] == run.name
