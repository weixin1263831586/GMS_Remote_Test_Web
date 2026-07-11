from features.reports.weekly_report_api import _issue_body_for_ai


def test_issue_body_bounds_large_redmine_journal_notes():
    issue = {
        "issue_id": 42,
        "subject": "large log",
        "description": "D" * 2000,
        "journals_json": [
            {"user": "alice", "notes": str(index) + "N" * 5000}
            for index in range(10)
        ],
    }

    text = _issue_body_for_ai(issue)

    assert "描述：" + "D" * 500 in text
    assert "0N" not in text  # only the most recent six notes are retained
    assert len(text) < 6000
