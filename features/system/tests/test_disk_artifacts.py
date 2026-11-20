"""Regression coverage for the bounded, read-only runtime disk inventory."""

from __future__ import annotations

import os
import time
from pathlib import Path

from features.system import disk_artifacts


def _inventory(tmp_path: Path, monkeypatch, **extra):
    monkeypatch.setattr(
        disk_artifacts, "upload_temp_root",
        lambda namespace="gms_uploads": str(tmp_path / "uploads" / namespace),
    )
    return disk_artifacts.collect_disk_artifacts(
        tmp_path, test_log_root=tmp_path / "empty-test-logs",
        worker_root=tmp_path / "missing-worker", **extra,
    )


def _rows(result):
    return {item["id"]: item for item in result["items"]}


def test_one_pass_counts_apk_and_redmine_without_double_count(tmp_path: Path, monkeypatch):
    apk = tmp_path / "apk_uploads" / "a"
    apk.mkdir(parents=True)
    (apk / "sample.apk").write_bytes(b"a" * 37)
    (apk / "jadx_output").mkdir()
    (apk / "jadx_output" / "Main.java").write_bytes(b"j" * 41)
    owner = tmp_path / "redmine" / "by_user" / "owner1" / "evidence"
    owner.mkdir(parents=True)
    (owner / "evidence.log").write_bytes(b"e" * 73)
    db = owner.parent / "daily_brief.sqlite3-wal"
    db.write_bytes(b"d" * 11)
    (tmp_path / "security_audit.json.rotated").write_bytes(b"l" * 5)
    (tmp_path / "cluster" / "state.sqlite3").parent.mkdir()
    (tmp_path / "cluster" / "state.sqlite3").write_bytes(b"c" * 13)
    worker = tmp_path.parent / ("worker-" + tmp_path.name)
    (worker / "job-a").mkdir(parents=True)
    (worker / "job-a" / "stdout.log").write_bytes(b"w" * 17)
    try:
        # Include a same-host Worker root outside the Controller data directory.
        monkeypatch.setattr(disk_artifacts, "upload_temp_root",
                            lambda namespace="gms_uploads":
                            str(tmp_path / "uploads" / namespace))
        result = disk_artifacts.collect_disk_artifacts(
            tmp_path, test_log_root=tmp_path / "missing-test-logs",
            worker_root=worker,
        )
        rows = _rows(result)
        assert rows["apk"]["bytes"] == 78
        assert rows["redmine_evidence"]["bytes"] == 73
        assert rows["redmine_databases"]["bytes"] == 11
        assert rows["security_audit_logs"]["bytes"] == 5
        assert rows["cluster_other"]["bytes"] == 13
        assert rows["local_worker_jobs"]["bytes"] == 17
        assert result["summary"]["controller_data_bytes"] == 180
        assert result["summary"]["measured_bytes"] == 197
        assert all(row["complete"] for row in rows.values())
        assert result["readonly"] is True
        assert rows["apk"]["breakdown"][0] in (
            {"type": ".java", "bytes": 41, "files": 1},
            {"type": ".apk", "bytes": 37, "files": 1},
        )
    finally:
        import shutil
        shutil.rmtree(worker)


def test_skipped_symlink_does_not_count_external_content(tmp_path, monkeypatch):
    apk = tmp_path / "apk_uploads" / "task"
    apk.mkdir(parents=True)
    (apk / "app.apk").write_bytes(b"apk-content")
    external = tmp_path.parent / (tmp_path.name + "-external.bin")
    external.write_bytes(b"private-data-outside-root")
    try:
        (apk / "escape").symlink_to(external)
        result = _inventory(tmp_path, monkeypatch)
        row = _rows(result)["apk"]
        assert row["bytes"] == len(b"apk-content")
        assert row["complete"] is False
        assert "符号链接" in row["note"]
        assert _rows(result)["redmine_evidence"]["complete"] is True
    finally:
        external.unlink(missing_ok=True)


def test_redmine_owner_root_symlink_is_not_followed(tmp_path, monkeypatch):
    external = tmp_path.parent / (tmp_path.name + "-external-redmine")
    (external / "owner" / "evidence").mkdir(parents=True)
    (external / "owner" / "evidence" / "secret.bin").write_bytes(b"x" * 55)
    redmine = tmp_path / "redmine"
    redmine.mkdir()
    (redmine / "by_user").symlink_to(external, target_is_directory=True)
    try:
        result = _inventory(tmp_path, monkeypatch)
        row = _rows(result)["redmine_evidence"]
        assert row["bytes"] == 0
        assert row["complete"] is False
        assert "符号链接" in row["note"]
    finally:
        import shutil
        shutil.rmtree(external)


def test_symlinked_ancestor_is_not_traversed(tmp_path, monkeypatch):
    external = tmp_path.parent / (tmp_path.name + "-outside")
    (external / "artifact-stage").mkdir(parents=True)
    (external / "artifact-stage" / "image.bin").write_bytes(b"f" * 120)
    (tmp_path / "automation").symlink_to(external, target_is_directory=True)
    try:
        result = _inventory(tmp_path, monkeypatch)
        stage = _rows(result)["firmware_stage"]
        assert stage["bytes"] == 0
        assert stage["complete"] is False
    finally:
        import shutil
        shutil.rmtree(external)


def test_older_than_reference_counts_but_is_not_deletable(tmp_path, monkeypatch):
    apk = tmp_path / "apk_uploads"
    apk.mkdir()
    path = apk / "old.apk"
    path.write_bytes(b"old-apk")
    old = time.time() - 45 * 86400
    os.utime(path, (old, old))
    result = _inventory(tmp_path, monkeypatch)
    row = _rows(result)["apk"]
    assert row["aged_bytes"] == 7
    assert row["older_7d_bytes"] == 7
    assert row["older_30d_bytes"] == 7
    assert row["older_90d_bytes"] == 0
    assert result["coverage"][-1].startswith("参考超龄")


def test_owner_limit_and_global_truncation_advertise_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_artifacts, "MAX_OWNER_ENTRIES", 2)
    owner = tmp_path / "redmine" / "by_user"
    for name in ("u1", "u2", "u3"):
        folder = owner / name / "evidence"
        folder.mkdir(parents=True)
        (folder / "a.log").write_bytes(b"x" * 10)
    result = _inventory(tmp_path, monkeypatch)
    assert _rows(result)["redmine_evidence"]["complete"] is False
    assert "上限" in _rows(result)["redmine_evidence"]["note"]
    # Distinct case: a small total visit cap must not claim exact totals.
    monkeypatch.setattr(disk_artifacts, "MAX_VISITED_ENTRIES", 2)
    second = _inventory(tmp_path, monkeypatch)
    assert second["summary"]["controller_scan_complete"] is False
    assert second["summary"]["incomplete_categories"] > 0


def test_custom_data_root_counts_actual_agent_transcripts_once(tmp_path, monkeypatch):
    project = tmp_path / "project"
    data = tmp_path / "custom-data"
    real = project / "data" / "assistant" / "agent_transcripts"
    real.mkdir(parents=True)
    (real / "session.json").write_bytes(b"x" * 23)
    stale = data / "assistant" / "agent_transcripts"
    stale.mkdir(parents=True)
    (stale / "old.json").write_bytes(b"y" * 47)
    monkeypatch.setattr(disk_artifacts, "PROJECT_ROOT", str(project))
    monkeypatch.setattr(disk_artifacts, "settings",
                        type("Settings", (), {"data_root": data})())
    monkeypatch.setattr(disk_artifacts, "upload_temp_root",
                        lambda namespace="gms_uploads":
                        str(tmp_path / "unused" / namespace))
    result = disk_artifacts.collect_disk_artifacts(
        test_log_root=tmp_path / "missing", worker_root=tmp_path / "worker-jobs",
    )
    rows = _rows(result)
    assert rows["agent_transcripts"]["bytes"] == 23
    assert rows["runtime_other"]["bytes"] == 47
    assert result["summary"]["measured_bytes"] == 70


def test_forced_refresh_bypasses_cache_and_cached_reads_are_labeled(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(disk_artifacts, "settings",
                        type("Settings", (), {"data_root": tmp_path})())
    monkeypatch.setattr(disk_artifacts, "_CACHE", None)
    monkeypatch.setattr(disk_artifacts, "collect_disk_artifacts",
                        lambda: calls.append(True) or {"generated_at": str(len(calls))})
    first = disk_artifacts.cached_disk_artifacts()
    cached = disk_artifacts.cached_disk_artifacts()
    forced = disk_artifacts.cached_disk_artifacts(refresh=True)
    assert len(calls) == 2
    assert "cache_hit" not in first
    assert cached["cache_hit"] is True
    assert forced["generated_at"] == "2"



def test_overlapping_external_roots_count_nested_uploads_once(tmp_path, monkeypatch):
    data = tmp_path / "controller" / "data"
    data.mkdir(parents=True)
    parent = tmp_path / "uploads"
    child = parent / "gms_firmware_uploads"
    child.mkdir(parents=True)
    (parent / "other.bin").write_bytes(b"a" * 17)
    (child / "chunk.bin").write_bytes(b"b" * 23)
    monkeypatch.setattr(
        disk_artifacts, "upload_temp_root",
        lambda namespace="gms_uploads": str(
            child if namespace == "gms_firmware_uploads" else parent
        ),
    )
    result = disk_artifacts.collect_disk_artifacts(
        data, test_log_root=tmp_path / "missing-logs",
        worker_root=tmp_path / "missing-worker",
    )
    rows = _rows(result)
    assert rows["firmware_chunks"]["bytes"] == 23
    assert rows["generic_uploads"]["bytes"] == 17
    assert result["summary"]["measured_bytes"] == 40


def test_parent_external_root_excludes_controller_data(tmp_path, monkeypatch):
    data = tmp_path / "controller" / "data"
    data.mkdir(parents=True)
    (data / "index.db").write_bytes(b"d" * 31)
    (data.parent / "extra.bin").write_bytes(b"e" * 11)
    monkeypatch.setattr(
        disk_artifacts, "upload_temp_root",
        lambda namespace="gms_uploads": str(
            data.parent if namespace == "gms_uploads" else tmp_path / "missing-firmware"
        ),
    )
    result = disk_artifacts.collect_disk_artifacts(
        data, test_log_root=tmp_path / "missing-logs",
        worker_root=tmp_path / "missing-worker",
    )
    rows = _rows(result)
    assert rows["runtime_other"]["bytes"] == 31
    assert rows["generic_uploads"]["bytes"] == 11
    assert result["summary"]["measured_bytes"] == 42


def test_temp_directory_enumeration_budget_reports_partial(tmp_path, monkeypatch):
    root = tmp_path / "controller"
    root.mkdir()
    synthetic = tmp_path / "temp-candidates"
    synthetic.mkdir()
    # Nonmatching entries also consume the enumeration budget.
    for i in range(8):
        (synthetic / f"unrelated-{i}").mkdir()
    monkeypatch.setattr(disk_artifacts, "MAX_TEMP_DISCOVERY_ENTRIES", 3)
    original_scandir = disk_artifacts.os.scandir

    def fake_scandir(path):
        return original_scandir(synthetic if str(path) == "/tmp" else path)

    monkeypatch.setattr(disk_artifacts.os, "scandir", fake_scandir)
    roots, note = disk_artifacts._additional_roots(
        root, test_log_root=None, worker_root=None, requested_data_root=False,
    )
    assert "3 项" in note
    assert not [item for item in roots if item[1] == "report_temp"]
    _, timed_out = disk_artifacts._additional_roots(
        root, test_log_root=None, worker_root=None,
        requested_data_root=False, deadline=time.monotonic() - 1,
    )
    assert "时间保护上限" in timed_out


def test_temp_report_directory_limit_marks_partial(tmp_path, monkeypatch):
    root = tmp_path / "deployment"
    root.mkdir()
    synthetic = tmp_path / "temp-entries"
    synthetic.mkdir()
    for i in range(202):
        (synthetic / f"gms-report-download-{i}").mkdir()
    monkeypatch.setattr(disk_artifacts, "PROJECT_ROOT", str(root))
    monkeypatch.setattr(
        disk_artifacts, "settings",
        type("Settings", (), {"data_root": root / "data"})(),
    )
    monkeypatch.setattr(
        disk_artifacts, "upload_temp_root",
        lambda namespace="gms_uploads": str(root / "uploads" / namespace),
    )
    real_scandir = disk_artifacts.os.scandir

    def fake_scandir(path):
        return real_scandir(synthetic if str(path) == "/tmp" else path)

    monkeypatch.setattr(disk_artifacts.os, "scandir", fake_scandir)
    result = disk_artifacts.collect_disk_artifacts()
    row = _rows(result)["report_temp"]
    assert row["complete"] is False
    assert "200" in row["note"]
    assert result["summary"]["incomplete_categories"] >= 1
