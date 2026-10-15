"""Updates must preserve loaded MCP files and diagnose stale connections."""

import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ("runtime" if (ROOT / "runtime").is_dir() else "scripts")
sys.path.insert(0, str(RUNTIME))
import mcp_server  # noqa: E402
from gms_agent import package_manager as pm  # noqa: E402
from gms_agent.runtime_processes import running_runtimes  # noqa: E402


def process(proc, pid, *argv):
    directory = proc / str(pid)
    directory.mkdir(parents=True)
    (directory / "cmdline").write_bytes(b"\0".join(os.fsencode(arg) for arg in argv) + b"\0")


def test_process_scan_matches_only_exact_versioned_runtime_scripts(tmp_path):
    versions = tmp_path / "versions"
    proc = tmp_path / "proc"
    # The version directory need not exist: this must diagnose old clients
    # whose loaded runtime was already deleted by an earlier package.
    script = versions / "1.0.0/scripts/mcp_server.py"
    process(proc, 101, "/usr/bin/python3", script)
    process(proc, 102, "/usr/bin/python3", versions / "2.0.0/scripts/mcp_launcher.py")
    process(proc, 103, "/bin/sh", "-c", script)
    process(proc, 104, "/usr/bin/python3", tmp_path / "versions-other/1.0.0/scripts/mcp_server.py")
    process(proc, 105, "/usr/bin/python3", versions / "1.0.0/scripts/unrelated.py")
    (proc / "106").mkdir()  # exited during inspection
    assert running_runtimes(versions, proc) == [
        {"pid": 101, "version": "1.0.0"}, {"pid": 102, "version": "2.0.0"},
    ]
    assert running_runtimes(versions, tmp_path / "unavailable") == []


def test_prune_preserves_live_mcp_and_removes_it_after_disconnect(tmp_path, monkeypatch):
    versions = tmp_path / "versions"
    current, previous, active, unused = [versions / name for name in ("4", "3", "2", "1")]
    for version in (current, previous, active, unused):
        version.mkdir(parents=True)
    link = tmp_path / "current"
    link.symlink_to(current)
    monkeypatch.setattr(pm, "VERSIONS_DIR", versions)
    monkeypatch.setattr(pm, "CURRENT_LINK", link)
    with patch.object(pm, "running_runtimes", return_value=[{"pid": 101, "version": "2"}]):
        assert pm.prune_old_versions(previous) == ["1"]
        assert active.is_dir()
    with patch.object(pm, "running_runtimes", return_value=[]):
        assert pm.prune_old_versions(previous) == ["2"]
    assert current.is_dir() and previous.is_dir()


def test_doctor_warns_about_stale_mcp_without_failing_installation(monkeypatch):
    monkeypatch.setattr(pm, "configured_clients", lambda: [])
    monkeypatch.setattr(pm, "detect_clients", lambda: [])
    monkeypatch.setattr(pm, "installed_version", lambda: "2")
    monkeypatch.setattr(pm, "runtime_version", lambda: "2")
    monkeypatch.setattr(pm, "cli_version_in", lambda *_: "2")
    monkeypatch.setattr(pm, "_host_environment_status", lambda: {"tmpdir_writable": True})
    processes = [{"pid": 101, "version": "1"}, {"pid": 102, "version": "2"}]
    with patch.object(pm, "running_runtimes", return_value=processes):
        report = pm.doctor_report()
    assert report["ok"] is True
    assert report["mcp_processes"] == processes
    assert len(report["warnings"]) == 1
    assert "101" in report["warnings"][0] and "reconnect" in report["warnings"][0]


@pytest.mark.parametrize("method", ["tools/list", "tools/call"])
def test_dispatch_failure_returns_rpc_error_and_next_request_still_works(method):
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"name": "gms_rt_context"}},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]
    stdin = io.StringIO("".join(json.dumps(message) + "\n" for message in messages))
    stdout = io.StringIO()
    with patch.object(mcp_server, "tools", side_effect=AttributeError("broken registry")), \
            patch("sys.stdin", stdin), patch("sys.stdout", stdout), patch("sys.stderr", io.StringIO()):
        mcp_server.main()
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert replies[0]["id"] == 1 and replies[0]["error"]["code"] == -32603
    assert replies[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}
