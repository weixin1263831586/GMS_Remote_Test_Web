"""Daily brief concurrency, bounded ranking and presentation contracts."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest

from features.redmine.daily_brief_execution_view import issue_payload
from features.redmine.daily_brief_models import DailyBriefIssue, DailyBriefRun, base_priority_score
from features.redmine.daily_brief_report import render_daily_brief_markdown
from features.redmine.daily_brief_result import ISSUE_RESULT_REQUIRED_FIELDS, ISSUE_RESULT_SCHEMA, validate_issue_result
from features.redmine.kkagent.analyzer import KkAgentRedmineAnalyzer
from features.redmine.kkagent.evidence_gate import evaluate_evidence_gate, gate_errors
from features.redmine.tests.kkagent.test_evidence_gate import _full_trace
from features.redmine.tests.kkagent.test_process import _valid_result
from features.redmine.tests.test_daily_brief_service import make_service


@pytest.mark.parametrize("mode", ["manual", "delta"])
def test_concurrent_creation_returns_persisted_run(tmp_path, mode):
    services = [make_service(tmp_path), make_service(tmp_path)]
    if mode == "delta":
        nightly = services[0].start_run("nightly")
        date = services[0].repository.get_run(nightly["run_id"]).brief_date
    barrier = Barrier(2)

    def start(service):
        original = service.repository.find_run

        def find(*args):
            found = original(*args)
            if args[-1] == mode:
                barrier.wait(timeout=10)
            return found

        with patch.object(service.repository, "find_run", side_effect=find):
            return service.start_refresh(date) if mode == "delta" else service.start_run(mode)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, services))
    assert results[0]["run_id"] == results[1]["run_id"]
    assert results[0]["job_id"] == results[1]["job_id"]
    run_id = results[0]["run_id"]
    assert services[0].repository.get_run(run_id) is not None
    assert services[0].repository.get_active_run_job(run_id) is not None


def test_priority_is_bounded_and_independent_of_attachments():
    entry = {"buckets": ["no_reply_3_days"], "priority_name": "Normal"}
    scores = [base_priority_score({**entry, "unreplied_days": days}) for days in (7, 30, 100, float("inf"))]
    assert len(set(scores)) == 1
    assert scores[0] < base_priority_score({"priority_name": "Urgent"})
    assert base_priority_score({**entry, "attachment_count": 0}) == base_priority_score({**entry, "attachment_count": 5})
    assert 0 <= base_priority_score({**entry, "unreplied_days": float("nan")}) <= 100


def test_zero_stale_report_is_short_and_keeps_detail_separate():
    run = DailyBriefRun(owner_id="u", brief_date="2026-09-15", mode="manual", run_id="r")
    issue = DailyBriefIssue(run_id="r", issue_id=1, buckets=[], result={"detailed_report": "长" * 2000})
    report = render_daily_brief_markdown(run, [issue], [])
    assert "超 3 天未回复 0" in report
    assert "超 03" not in report
    assert "长" * 2000 not in report


def test_triage_does_not_require_history_or_source():
    entry = {"issue_id": 1, "subject": "CTS failure", "analysis_mode": "triage"}
    gate = evaluate_evidence_gate(_full_trace(history_searches=0), entry)
    assert not gate["history_checked"]
    assert not gate["source_evidence_required"]
    assert gate_errors(gate) == []
    prompt = KkAgentRedmineAnalyzer().build_prompt(entry)
    assert "DAILY TRIAGE ONLY" in prompt
    assert "HISTORY SEARCH (mandatory" not in prompt
    assert "EXACTLY these level-2 sections" not in prompt


def test_attachment_listing_is_not_reading():
    issue = DailyBriefIssue(run_id="r", issue_id=1, buckets=[], result={"evidence_gate": {"attachments_checked": False}})
    execution = {"tools": [{"tool_name": "gms_rt_redmine_attachments", "status": "succeeded"}]}
    assert not issue_payload(issue, execution)["ai_execution"]["attachments_checked"]
    issue.result["evidence_gate"]["attachments_checked"] = True
    assert issue_payload(issue, execution)["ai_execution"]["attachments_checked"]


def test_prompt_uses_the_validator_schema():
    import json

    prompt = KkAgentRedmineAnalyzer().build_prompt({"issue_id": 1, "analysis_mode": "triage"})
    schema, _ = json.JSONDecoder().raw_decode(prompt.split("matching this schema:\n", 1)[1])
    assert schema == ISSUE_RESULT_SCHEMA
    for field in ISSUE_RESULT_REQUIRED_FIELDS:
        result = _valid_result()
        result.pop(field)
        assert f"missing field: {field}" in validate_issue_result(result)


def test_nested_evidence_must_have_references():
    result = _valid_result()
    result["evidence"] = [{"source": "journal", "fact": "unsupported"}]
    assert "missing field: evidence.0.reference" in validate_issue_result(result)
