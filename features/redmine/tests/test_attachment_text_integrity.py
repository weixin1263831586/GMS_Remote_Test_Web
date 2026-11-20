"""Legacy attachment parsing must expose unreadable or undecodable evidence."""

from pathlib import Path

from features.redmine.analysis_attachments import AttachmentAnalysisMixin


class _Parser(AttachmentAnalysisMixin):
    pass


def test_invalid_utf8_is_not_silently_deleted(tmp_path):
    path = tmp_path / "customer.log"
    path.write_bytes(b"CTS FAILURE\nactual: false\ninvalid:\xff")
    result = _Parser()._analyze_text_attachment(str(path))
    assert result["parsed"] is True
    assert result["details"]["encoding_warning"] is True
    assert result["details"]["utf8_replacement_count"] == 1
    assert "\ufffd" in result["text_excerpt"]


def test_unreadable_file_is_not_reported_as_success(tmp_path):
    result = _Parser()._analyze_text_attachment(str(tmp_path / "missing.log"))
    assert result["parsed"] is False
    assert result["details"]["read_error_kind"] == "FileNotFoundError"
    assert result["text_excerpt"] == ""
