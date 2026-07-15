from unittest.mock import patch

from features.reports.weekly_config import android17_sheet_url
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


def test_android17_sheet_url_prefers_environment_then_config():
    with patch.dict("os.environ", {"GMS_ANDROID17_SHEET_URL": "https://env.example/sheet"}), \
            patch("features.reports.weekly_config.config_manager") as manager:
        assert android17_sheet_url() == "https://env.example/sheet"
        manager.get_runtime_config.assert_called_once()

    with patch.dict("os.environ", {}, clear=True), \
            patch("features.reports.weekly_config.config_manager") as manager:
        manager.get_runtime_config.return_value = {}
        manager.load_config.return_value = {
            "weekly_report": {"android17_sheet_url": "https://config.example/sheet"}
        }
        assert android17_sheet_url() == "https://config.example/sheet"
