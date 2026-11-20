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

def test_large_log_scans_late_failures_without_read_text(tmp_path, monkeypatch):
    path = tmp_path / "large.log"
    # The decisive stack trace may arrive after hundreds of thousands of lines.
    path.write_bytes(b"INFO: normal event\n" * 80000
                     + b"Caused by: java.lang.IllegalStateException\n")

    def reject_whole_file_read(*_args, **_kwargs):
        raise AssertionError("log parser must stream rather than read all text")

    monkeypatch.setattr(Path, "read_text", reject_whole_file_read)
    result = _Parser()._analyze_text_attachment(str(path))
    assert result["parsed"] is True
    assert result["summary"]["interesting_lines"] == 1
    assert result["summary"]["error_blocks"] == 1
    assert "IllegalStateException" in result["failures"][0]["reason"]
    assert result["text_excerpt"].startswith("INFO: normal event")
    assert len(result["text_excerpt"]) < 3100


def test_large_contiguous_error_block_does_not_fill_report(tmp_path):
    path = tmp_path / "repeated-errors.log"
    path.write_text("Caused by: java.lang.IllegalStateException\n" * 20000,
                    encoding="utf-8")
    result = _Parser()._analyze_text_attachment(str(path))
    assert result["summary"]["interesting_lines"] == 30
    assert result["summary"]["error_blocks"] == 1
    assert len(result["failures"][0]["reason"]) < 1300
