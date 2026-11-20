"""Older Redmine replies must remain discoverable in issue export metadata."""

from features.redmine.analysis_reporting import ReportingAnalysisMixin


class _Reporter(ReportingAnalysisMixin):
    def _detect_version_type(self, version):
        return ""

    def _ensure_code_block(self, text, _language):
        return str(text)


def test_full_history_index_prevents_older_journal_from_disappearing():
    journals = [
        {"id": i, "created_on": f"2026-11-{i:02d}", "user": "FAE",
         "notes": f"comment {i} contains history detail"}
        for i in range(1, 21)
    ]
    exported = _Reporter()._build_issue_document({
        "issue_id": 9, "subject": "Test failure", "journals_json": journals,
        "attachments_json": [], "failures_json": [], "references_json": [],
    })
    assert "较早回复索引" in exported
    for i in range(1, 6):
        assert f"journal #{i}" in exported
        assert f"comment {i} contains history detail" in exported
    assert "最近 15 条详细记录" in exported
    assert "comment 20 contains history detail" in exported
    assert "Redmine 原始回复" in exported
