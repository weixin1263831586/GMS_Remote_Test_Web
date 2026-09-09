"""Reconciler tests for agent_mcp_config.py (10.txt §十四/§十五)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "skills"
    / "gms-remote-test"
    / "scripts"
    / "agent_mcp_config.py"
)
spec = importlib.util.spec_from_file_location("agent_mcp_config", SCRIPT)
mod = importlib.util.module_from_spec(spec)
sys.modules["agent_mcp_config"] = mod
spec.loader.exec_module(mod)

SERVER = "https://controller:5001"
MCP = "/opt/gms/mcp_server.py"
PROFILE = "kimi-host01-1000"
TOKEN = "/state/kimi-host01-1000.token"
CA = "/etc/gms/ca.pem"


def desired_env(ca: str = "") -> dict[str, str]:
    return mod._desired_env(SERVER, PROFILE, TOKEN, ca)


def test_kimi_fresh_registration(tmp_path):
    config = tmp_path / "mcp.json"
    result = mod.reconcile_kimi(config, MCP, SERVER, PROFILE, TOKEN, CA)
    assert "registered" in result
    data = json.loads(config.read_text())
    assert data["mcpServers"]["gms"]["args"] == [MCP]
    assert data["mcpServers"]["gms"]["env"]["GMS_CURL_CA_CERT"] == CA


def test_kimi_updates_stale_gms_block_preserving_others(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({
        "mcpServers": {
            "other": {"command": "uvx", "args": ["foo"]},
            "gms": {
                "command": "python3",
                "args": ["/old/mcp_server.py"],
                "env": {"GMS_REMOTE_TEST_SERVER": "https://old:5001"},
            },
        }
    }))
    result = mod.reconcile_kimi(config, MCP, SERVER, PROFILE, TOKEN, CA)
    assert "updated" in result
    data = json.loads(config.read_text())
    assert data["mcpServers"]["other"] == {"command": "uvx", "args": ["foo"]}
    assert data["mcpServers"]["gms"]["env"]["GMS_REMOTE_TEST_SERVER"] == SERVER
    # Old config preserved as a backup.
    backups = list(tmp_path.glob("mcp.json.bak.*"))
    assert backups, "update must keep a backup of the previous config"


def test_kimi_unchanged_is_idempotent(tmp_path):
    config = tmp_path / "mcp.json"
    mod.reconcile_kimi(config, MCP, SERVER, PROFILE, TOKEN, CA)
    before = config.read_text()
    result = mod.reconcile_kimi(config, MCP, SERVER, PROFILE, TOKEN, CA)
    assert result.startswith("unchanged")
    assert config.read_text() == before


def test_kimi_invalid_json_fails_and_never_overwrites(tmp_path):
    # 10.txt §十四: a single comma error must FAIL the install, never
    # rewrite the user's config down to only-GMS.
    config = tmp_path / "mcp.json"
    config.write_text('{"mcpServers": {"user": {"command": "x",},}}')  # trailing commas
    before = config.read_text()
    try:
        mod.reconcile_kimi(config, MCP, SERVER, PROFILE, TOKEN, CA)
    except mod.ReconcileError as error:
        assert "备份" in str(error)
    else:
        raise AssertionError("invalid JSON must raise ReconcileError")
    assert json.loads(config.read_text().replace(",}", "}").replace(",}}", "}}")) or True
    # Original bytes untouched.
    assert config.read_text() == before


def test_codex_reconcile_updates_stale_block(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        'model = "gpt"\n\n[mcp_servers.old]\ncommand = "x"\n'
        "\n[mcp_servers.gms_remote_test]\n"
        'command = "python3"\n'
        'args = ["/old/mcp_server.py"]\n'
        "\n[mcp_servers.gms_remote_test.env]\n"
        'GMS_REMOTE_TEST_SERVER = "https://old:5001"\n'
    )
    result = mod.reconcile_codex(config, MCP, SERVER, PROFILE, TOKEN, CA)
    assert "updated" in result
    text = config.read_text()
    assert 'model = "gpt"' in text
    assert "[mcp_servers.old]" in text
    assert f'GMS_REMOTE_TEST_SERVER = "{SERVER}"' in text
    assert "https://old:5001" not in text
    assert "[mcp_servers.gms_remote_test.env]" in text


def test_codex_reconcile_idempotent(tmp_path):
    config = tmp_path / "config.toml"
    mod.reconcile_codex(config, MCP, SERVER, PROFILE, TOKEN, CA)
    before = config.read_text()
    result = mod.reconcile_codex(config, MCP, SERVER, PROFILE, TOKEN, CA)
    assert result.startswith("unchanged")
    assert config.read_text() == before


def test_cli_wrapper_exit_codes(tmp_path):
    import subprocess

    config = tmp_path / "mcp.json"
    config.write_text("not json {")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "kimi", str(config), MCP, SERVER, PROFILE, TOKEN, CA],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "备份" in result.stderr
