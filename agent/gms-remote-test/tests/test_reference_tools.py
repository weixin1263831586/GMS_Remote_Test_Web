"""Mainline reference tools stay read-only and preserve typed CLI arguments."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


def test_reference_tool_contract(monkeypatch):
    package = Path(__file__).resolve().parents[1]
    runtime = package / ("runtime" if (package / "runtime").is_dir() else "scripts")
    monkeypatch.syspath_prepend(str(runtime))
    spec = importlib.util.spec_from_file_location("reference_test_mcp", runtime / "mcp_server.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setenv("GMS_MCP_TOOLSETS", "evidence")
    schemas = {tool["name"]: tool for tool in module.tools()}
    assert "gms_rt_redmine_references" in schemas
    assert schemas["gms_rt_redmine_references"]["inputSchema"]["required"] == ["snapshot_id"]
    assert "gms_rt_redmine_references" in module._TOOL_HANDLERS
    assert module.redmine_references_tool({})[1] is True
    with patch.object(module, "run_cli", return_value=("{}", False)) as run:
        module.redmine_references_tool({"snapshot_id": "ev_1", "query": "CVE-2026-58835 ag/40830952"})
        run.assert_called_once_with("gms-rt-redmine-references", ["ev_1", "--query", "CVE-2026-58835 ag/40830952"])


def test_reference_cli_preserves_query_as_data(tmp_path):
    package = Path(__file__).resolve().parents[1]
    runtime = package / ("runtime" if (package / "runtime").is_dir() else "scripts")
    command = r'''
reference_cli_runtime=$1
reference_cli_snapshot=$2
reference_cli_query=$3
source "$reference_cli_runtime"
api_call() { printf '%s\n' "$1"; }
gms-rt-redmine-references "$reference_cli_snapshot" --query "$reference_cli_query"
'''
    env = {**{key: value for key, value in os.environ.items() if not key.startswith(("GMS_", "ATS_"))},
           "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config"),
           "XDG_STATE_HOME": str(tmp_path / "state")}
    result = subprocess.run(["bash", "-c", command, "reference-cli", str(runtime / "gms-remote-test.sh"),
                             "ev_1", "CVE-2026-58835;$(id)"], env=env,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    target = urlsplit(result.stdout.strip())
    assert target.path == "/redmine-agent/evidence/ev_1/references"
    assert parse_qs(target.query) == {"q": ["CVE-2026-58835;$(id)"]}
