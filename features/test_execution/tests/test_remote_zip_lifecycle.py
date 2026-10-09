"""Run the remote Python payload locally to verify cleanup and bounded archives."""

import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from features.test_execution.suite_remote_scripts import SUITE_DIR_CLEANUP_SCRIPT, SUITE_DIR_ZIP_SCRIPT


def setup_suite(tmp_path):
    root = tmp_path / "suite"
    target = root / "results/run"
    (target / "nested").mkdir(parents=True)
    (target / "a.txt").write_bytes(b"first report")
    (target / "nested/b.txt").write_bytes(b"second report")
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    env = dict(os.environ, TMPDIR=str(temporary))
    return root, target, env


def operation_path(env, token):
    return Path(env["TMPDIR"]) / f"gms-suite-downloads-{os.getuid()}" / token


def command(root, target, token, *, max_bytes=1024 ** 2, max_files=10, min_free=1, stale=60,
            timeout=10, prefix="", cleanup=False):
    args = [sys.executable, "-c", prefix + (SUITE_DIR_CLEANUP_SCRIPT if cleanup else SUITE_DIR_ZIP_SCRIPT),
            str(root), str(target), token]
    return args if cleanup else [*args, *map(str, [max_bytes, max_files, min_free, stale, timeout])]


def run_archive(root, target, env, token, **kwargs):
    result = subprocess.run(command(root, target, token, **kwargs), env=env, capture_output=True,
                            text=True, timeout=15, check=True)
    return json.loads(result.stdout)


def paused_payload(marker):
    return f"""
import zipfile, time
original_open = zipfile.ZipFile.open
def paused_open(*args, **kwargs):
    with open({str(marker)!r}, 'w') as ready:
        ready.write('ready')
    time.sleep(60)
    return original_open(*args, **kwargs)
zipfile.ZipFile.open = paused_open
"""


def wait_ready(process, marker):
    deadline = time.monotonic() + 5
    while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists(), "archive process never reached the compression phase"


def test_zip_contents_keep_tree_and_cleanup_removes_operation(tmp_path):
    root, target, env = setup_suite(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("must stay outside archive")
    (target / "outside-link").symlink_to(outside)
    token = "a" * 32
    result = run_archive(root, target, env, token)
    assert result["success"]
    with zipfile.ZipFile(result["zip_path"]) as archive:
        assert sorted(archive.namelist()) == ["a.txt", "nested/b.txt"]
        assert archive.read("a.txt") == b"first report"
    assert run_archive(root, target, env, token, cleanup=True)["success"]
    assert not operation_path(env, token).exists()


@pytest.mark.parametrize("limits,code", [
    ({"max_bytes": 5}, "INVALID_SEMANTICS"),
    ({"max_bytes": 100}, "INVALID_SEMANTICS"),
    ({"max_files": 1}, "INVALID_SEMANTICS"),
    ({"min_free": 10 ** 30}, "DEPENDENCY_UNAVAILABLE"),
])
def test_limits_reject_before_disk_is_exhausted_and_leave_no_archive(tmp_path, limits, code):
    root, target, env = setup_suite(tmp_path)
    token = "a" * 32
    result = run_archive(root, target, env, token, **limits)
    assert result["success"] is False and result["code"] == code
    assert not operation_path(env, token).exists()


def test_alarm_removes_partially_created_zip(tmp_path):
    root, target, env = setup_suite(tmp_path)
    token = "a" * 32
    result = run_archive(root, target, env, token, timeout=1, prefix=paused_payload(tmp_path / "ready"))
    assert result["code"] == "DEPENDENCY_TIMEOUT"
    assert not operation_path(env, token).exists()


def test_controller_cleanup_stops_only_its_archive_process(tmp_path):
    root, target, env = setup_suite(tmp_path)
    token, marker = "a" * 32, tmp_path / "ready"
    process = subprocess.Popen(command(root, target, token, timeout=60, prefix=paused_payload(marker)),
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        wait_ready(process, marker)
        assert run_archive(root, target, env, token, cleanup=True)["success"]
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        assert json.loads(stdout)["code"] == "DEPENDENCY_TIMEOUT"
        assert not operation_path(env, token).exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_stale_collection_skips_live_writer_and_reaps_sigkill_and_legacy_files(tmp_path):
    root, target, env = setup_suite(tmp_path)
    token, marker = "a" * 32, tmp_path / "ready"
    process = subprocess.Popen(command(root, target, token, timeout=60, prefix=paused_payload(marker)),
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        wait_ready(process, marker)
        abandoned = operation_path(env, token)
        old = time.time() - 120
        os.utime(abandoned, (old, old))
        legacy = Path(env["TMPDIR"]) / "suite_dl_legacy.zip"
        legacy.write_bytes(b"abandoned zip")
        os.utime(legacy, (old, old))
        assert run_archive(root, target, env, "b" * 32)["success"]
        assert abandoned.exists() and not legacy.exists()
        process.kill()
        process.communicate(timeout=5)
        assert abandoned.exists()
        assert run_archive(root, target, env, "c" * 32)["success"]
        assert not abandoned.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_early_cancellation_tombstone_blocks_delayed_ssh_process(tmp_path):
    root, target, env = setup_suite(tmp_path)
    token = "a" * 32
    assert run_archive(root, target, env, token, cleanup=True)["success"]
    result = run_archive(root, target, env, token)
    assert result["success"] is False and result["code"] == "DEPENDENCY_TIMEOUT"
    assert not operation_path(env, token).exists()
