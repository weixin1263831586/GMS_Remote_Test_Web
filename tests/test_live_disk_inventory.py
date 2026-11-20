"""Exercise the CI inventory and verifier with synthetic Controller roots."""
import json
import sys
import time
from collections import defaultdict
from types import SimpleNamespace

import pytest


@pytest.fixture
def scripts(monkeypatch):
    from tools.scripts._common import find_repo_root

    monkeypatch.syspath_prepend(str(find_repo_root() / "tools/scripts/testing"))
    import scan_live_artifacts
    import verify_live_disk_inventory

    return scan_live_artifacts, verify_live_disk_inventory


def test_inventory_exports_only_fixed_bucket_names(scripts, tmp_path):
    scan, _ = scripts
    files = ("private-root.txt", "redmine/by_user/owner/private-name",
             "automation/private-path/item", "apk_uploads/file.private-extension")
    for name in files:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
    categories = defaultdict(scan.make_stat)
    top = defaultdict(scan.make_stat)
    extensions = defaultdict(lambda: {"files": 0, "bytes": 0})
    budget = {"visited": 0, "limit": 100, "deadline": time.monotonic() + 5}
    report = scan.scan_root(tmp_path, "data", categories, top, extensions, budget, time.time())
    assert report["complete"] and report["bytes"] == 4
    assert categories["redmine/by_user/*/其他"]["files"] == 1
    assert extensions["其他"]["files"] == 1
    assert "private" not in json.dumps([categories, top, extensions])


def test_verify_empty_categories_and_reject_incomplete_reference(scripts, tmp_path, monkeypatch):
    _, verify = scripts
    runner = tmp_path / "actions-runner"
    runner.mkdir()
    (runner / ".runner").touch()
    reference = tmp_path / "reference.json"
    output = tmp_path / "summary.json"
    root = {"root": "data", "bytes": 0, "complete": True}
    reference.write_text(json.dumps({"root_reports": [root], "categories": []}))
    monkeypatch.setenv("GMS_TEST_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setattr(sys, "argv", ["verify", "--host-root", str(tmp_path),
                                      "--reference", str(reference), "--output", str(output)])
    monkeypatch.setattr(verify, "active_disk_root_environment", lambda: ({}, "test"))
    audit = {
        "summary": {"controller_scan_complete": True, "controller_data_bytes": 0,
                    "scan_seconds": 0, "visited_entries": 0, "scanned_categories": 3,
                    "incomplete_categories": 0},
        "items": [{"id": key, "bytes": 0} for key in ("apk", "redmine_evidence", "local_worker_jobs")],
        "readonly": True, "generated_at": "test",
    }
    monkeypatch.setattr(verify, "_load_scanner", lambda *_: SimpleNamespace(collect_disk_artifacts=lambda: audit))
    assert verify.main() == 0
    assert all(check["reference_bytes"] == 0 for check in json.loads(output.read_text())["category_checks"])
    root["complete"] = False
    reference.write_text(json.dumps({"root_reports": [root], "categories": []}))
    with pytest.raises(SystemExit, match="not complete"):
        verify.main()
