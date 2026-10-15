"""Failed analyses remain visible in new and historical morning reports."""

from features.redmine.daily_brief_models import DailyBriefIssue, DailyBriefRun
from features.redmine.daily_brief_report import human_review_count, summarize_daily_brief
from features.redmine.tests.test_daily_brief_service import make_service


def populated_service(tmp_path):
    service = make_service(tmp_path)
    run = DailyBriefRun(
        owner_id="u1", brief_date="2026-10-12", mode="nightly", run_id="diagnostics",
        status="partial", report_json={"counts": {"needs_human_review": 0, "total": 9}},
    )
    service.repository.create_run(run)
    for issue_id in range(1, 10):
        service.repository.upsert_issue(DailyBriefIssue(
            run_id=run.run_id, issue_id=issue_id, buckets=[], subject="Synthetic issue",
            status="completed" if issue_id <= 5 else "failed",
            result={"evidence_quality": "verified", "detailed_report": "Synthetic report"}
            if issue_id <= 5 else {},
        ))
    return service, run


def test_human_review_count_includes_failures_and_flagged_completed_only():
    issues = [DailyBriefIssue(
        run_id="r", issue_id=index, buckets=[], status=status,
        result={"needs_human_review": flagged},
    ) for index, (status, flagged) in enumerate([
        ("failed", False), ("completed", True), ("completed", False),
        ("pending", True), ("running", True), ("cancelled", True),
    ])]
    assert human_review_count(issues) == 2


def test_new_report_counts_failed_analyses_as_needing_review(tmp_path):
    service, run = populated_service(tmp_path)
    report = summarize_daily_brief(service.repository, run, {})
    assert report.status == "partial"
    assert report.report_json["counts"]["needs_human_review"] == 4
    assert report.report_json["counts"]["evidence_verified"] == 5


def test_historical_payload_corrects_count_without_rewriting_report(tmp_path):
    service, run = populated_service(tmp_path)
    payload = service.run_payload(run)
    assert payload["run"]["report_json"]["counts"]["needs_human_review"] == 4
    assert run.report_json["counts"]["needs_human_review"] == 0
    persisted = service.repository.get_run(run.run_id)
    assert persisted.report_json["counts"]["needs_human_review"] == 0
