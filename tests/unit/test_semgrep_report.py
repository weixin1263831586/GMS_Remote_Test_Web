from tools.scripts.maintenance import check_semgrep_report as gate


def _report(*, errors=None, findings=None):
    return {
        "errors": errors or [],
        "results": findings or [],
        "paths": {"scanned": sorted(gate.REQUIRED_TARGETS)},
    }


def test_unknown_parser_error_or_process_exit_blocks(monkeypatch):
    monkeypatch.setattr(gate, "reviewed_parser_limit", lambda issue: False)
    assert gate.review_report(_report(errors=[{"path": "features/example.py"}]), 3) == 1
    assert gate.review_report(_report(), 2) == 1
    assert gate.review_report(_report(), 0) == 0


def test_reviewed_execution_requires_exact_rule_path_line_and_blob(monkeypatch):
    path, line, rule = next(iter(gate.APPROVED_EXECUTION))
    finding = {"path": path, "start": {"line": line}, "check_id": rule,
               "extra": {"severity": "ERROR"}}
    monkeypatch.setattr(gate, "blob_matches", lambda path, expected: True)
    assert gate.review_report(_report(findings=[finding]), 0) == 0
    assert gate.review_report(_report(findings=[{**finding, "start": {"line": line + 1}}]), 0) == 1
    monkeypatch.setattr(gate, "blob_matches", lambda path, expected: False)
    assert gate.review_report(_report(findings=[finding]), 0) == 1


def test_missing_scan_target_blocks():
    report = _report()
    report["paths"]["scanned"] = []
    assert gate.review_report(report, 0) == 1
