"""Help/update stay credential-free; terminal identity choices stay explicit."""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ("runtime" if (ROOT / "runtime").is_dir() else "scripts")
sys.path.insert(0, str(RUNTIME))
from gms_agent import cli_context, package_manager, profile_store  # noqa: E402


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_store, "PROFILE_ROOT", tmp_path / "config/gms-agent/profiles")
    monkeypatch.setattr(profile_store, "STATE_DIR", tmp_path / "state/gms-remote-test")
    for name, client in (("codex-one", "codex"), ("kimi-two", "kimi")):
        profile_store.write_profile_toml(name, client, "https://controller.example:5001", "")
        token = profile_store.token_file(name)
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("synthetic-" + name)
        token.chmod(0o600)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GMS_")}
    env.update({"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
                "XDG_STATE_HOME": str(tmp_path / "state"), "NO_COLOR": "1"})
    return env


def run_cli(environment, *args, runtime=RUNTIME):
    return subprocess.run(["bash", str(runtime / "gms-remote-test.sh"), *args],
                          env=environment, capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize("args", [
    (), ("--help",), ("--version",), ("gms-rt-system-help",),
    ("gms-rt-system-capabilities", "--json"),
    ("gms-rt-system-commands", "--json"),
    ("gms-rt-devices-list", "--help", "--json"),
])
def test_help_and_discovery_work_with_distinct_tokens(environment, args):
    result = run_cli(environment, *args)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()
    assert "拒绝自动选择" not in result.stderr


def test_bare_agent_prints_help_and_current_terminal_recipe(environment):
    result = subprocess.run([sys.executable, str(RUNTIME / "gms-agent")], env=environment,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'eval "$(gms-agent profile env)"' in result.stdout
    assert "gms-agent update" in result.stdout
    assert "gms-agent --help" in result.stdout
    assert "GMS Agent " in result.stdout
    assert len(result.stdout.splitlines()) <= 20
    assert len(result.stdout) < 1000
    assert "Package layout contract" not in result.stdout


def test_agent_explicit_help_keeps_command_list_without_internal_documentation(environment):
    result = subprocess.run([sys.executable, str(RUNTIME / "gms-agent"), "--help"],
                            env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "rollback" in result.stdout
    assert "auto-update" in result.stdout
    assert "Package layout contract" not in result.stdout
    assert "~/.codex/config.toml" not in result.stdout
    assert len(result.stdout) < 2500


@pytest.mark.parametrize("entrypoint", ["gms-agent", "gms-remote-test.sh"])
def test_quickstart_also_works_before_enrollment(environment, tmp_path, entrypoint):
    environment["XDG_CONFIG_HOME"] = str(tmp_path / "unconfigured")
    interpreter = sys.executable if entrypoint == "gms-agent" else "bash"
    result = subprocess.run([interpreter, str(RUNTIME / entrypoint)], env=environment,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "gms-agent update" in result.stdout
    assert len(result.stdout.splitlines()) <= 20


@pytest.mark.parametrize("output", ["human", "json"])
def test_bare_cli_is_a_short_local_quickstart(environment, output):
    environment["GMS_RT_OUTPUT"] = output
    result = run_cli(environment)
    assert result.returncode == 0, result.stderr
    if output == "json":
        envelope = json.loads(result.stdout)
        assert envelope["ok"] is True
        assert envelope["command"] == "gms-rt-system-help"
        content = envelope["output"]
    else:
        content = result.stdout
    assert 'eval "$(gms-agent profile env)"' in content
    assert "gms-rt --help" in content
    assert "GMS Remote Test " in content
    assert len(content.splitlines()) <= 20
    assert len(content) < 1000
    assert "Server:" not in content


@pytest.mark.parametrize("command", [("--help",), ("gms-rt-system-help",)])
@pytest.mark.parametrize("server", ["", "https://docs.example:5001/"])
def test_full_help_retains_commands_and_only_prints_complete_document_urls(environment, command, server):
    result = run_cli(environment, *command, *(["--server", server] if server else []))
    assert result.returncode == 0, result.stderr
    assert "gms-rt-jobs-follow" in result.stdout
    assert "gms-rt-sdk-search" in result.stdout
    if server:
        assert "Docs:   https://docs.example:5001/docs" in result.stdout
        assert "Help:   https://docs.example:5001/api/system/help" in result.stdout
    else:
        assert "Controller：尚未选择" in result.stdout
        assert "Docs:" not in result.stdout
        assert "Help:" not in result.stdout


@pytest.mark.parametrize("args", [
    ("--profile", "kimi-two", "gms-rt-auth-credential-mode", "--json"),
    ("gms-rt-auth-credential-mode", "--profile=kimi-two", "--json"),
])
def test_global_profile_flag_selects_exact_identity(environment, args):
    result = run_cli(environment, *args)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["data"]["token_file"].endswith("/kimi-two.token")


def test_unattended_ambiguity_still_fails_closed(environment):
    result = run_cli(environment, "gms-rt-auth-credential-mode", "--non-interactive")
    assert result.returncode == 2
    assert "gms-agent profile env" in result.stderr
    assert result.stdout == ""


def test_profile_flag_does_not_send_identity_to_another_controller(environment):
    result = run_cli(environment, "gms-rt-auth-credential-mode", "--profile", "kimi-two",
                     "--server", "https://other.example:5001")
    assert result.returncode == 2
    assert "拒绝向" in result.stderr


@pytest.mark.parametrize("profile", ["", "kimi-two"])
def test_update_reaches_lifecycle_without_business_token_selection(environment, tmp_path, profile):
    runtime = tmp_path / "runtime"
    shutil.copytree(RUNTIME, runtime, ignore=shutil.ignore_patterns("__pycache__"))
    (runtime / "gms-agent").write_text(
        "import json, sys\nprint(json.dumps({'args': sys.argv[1:]}))\n"
    )
    arguments = ["gms-rt-system-update"] + (["--profile", profile] if profile else [])
    result = run_cli(environment, *arguments, runtime=runtime)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["args"] == ["update", *(["--profile", profile] if profile else [])]


def test_profile_env_emits_only_the_explicit_export(environment):
    result = subprocess.run([sys.executable, str(RUNTIME / "gms-agent"), "profile", "env", "kimi-two"],
                            env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "export GMS_RT_PROFILE=kimi-two\n"
    assert "synthetic-" not in result.stdout + result.stderr


@pytest.mark.parametrize("explicit,expected", [("", "kimi-two"), ("codex-one", "codex-one")])
def test_update_honors_terminal_choice_and_explicit_override(environment, monkeypatch, explicit, expected):
    profile_store.write_profile_toml("kimi-two", "kimi", "https://kimi.example:5001", "")
    monkeypatch.setenv("GMS_RT_PROFILE", "kimi-two")
    monkeypatch.setattr(package_manager, "installed_version", lambda: "9.9.9")
    fetch = Mock(return_value=(b'{"version":"9.9.9"}', {}))
    monkeypatch.setattr(package_manager, "http_get", fetch)
    assert package_manager._cmd_update_locked(SimpleNamespace(profile=explicit, server="", force=False)) == 0
    expected_server = "https://kimi.example:5001" if expected == "kimi-two" else "https://controller.example:5001"
    assert fetch.call_args.args[0].startswith(expected_server + "/")


@pytest.mark.parametrize("failed,section", [
    ("/auth/status", "auth"), ("/system/health", "server_health"),
    ("/devices/list", "devices"), ("/devices/console/ports", "serial_console"),
    ("unauthenticated", "auth"), ("none", ""), ("auth_disabled", ""),
])
def test_selfcheck_distinguishes_json_errors_from_success(environment, failed, section):
    script = '''source "$1"
api_call() {
    if [ "$1" = "$SELF_CHECK_FAILURE" ]; then
        printf '%s\\n' '{"error":"synthetic authentication or service failure"}'
        return 3
    fi
    case "$1" in
        /auth/status)
            case "$SELF_CHECK_FAILURE" in
                unauthenticated) printf '%s\\n' '{"authenticated":false,"auth_required":true}' ;;
                auth_disabled) printf '%s\\n' '{"authenticated":false,"auth_required":false}' ;;
                *) printf '%s\\n' '{"authenticated":true,"auth_required":true}' ;;
            esac ;;
        /devices/list) printf '%s\\n' '[]' ;;
        *) printf '%s\\n' '{"success":true}' ;;
    esac
}
_gms_rt_dispatch gms-rt-system-selfcheck --json --non-interactive
'''
    result = subprocess.run(["bash", "-c", script, "bash", str(RUNTIME / "gms-remote-test.sh")],
                            env=dict(environment, GMS_RT_PROFILE="kimi-two", SELF_CHECK_FAILURE=failed),
                            capture_output=True, text=True, timeout=10)
    envelope = json.loads(result.stdout)
    if section:
        assert result.returncode != 0
        assert envelope["ok"] is False
        assert envelope["data"][section]["ok"] is False
        assert envelope["data"]["hints"]
    else:
        assert result.returncode == 0, result.stderr
        assert envelope["ok"] is True
        assert envelope["data"]["hints"] == []


@pytest.mark.parametrize("argv", [["gms-rt-auth-credential-mode", "--json"],
                                  ["gms-rt-auth-credential-mode", "--non-interactive"]])
def test_automation_never_prompts_even_when_a_terminal_is_present(environment, monkeypatch, argv):
    monkeypatch.setattr(cli_context, "interactive_allowed", lambda: True)
    monkeypatch.setattr(cli_context, "choose_profile", lambda *_: pytest.fail("unexpected interactive prompt"))
    assert cli_context.resolve_cli_context("", "", argv)["mode"] == "error"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX terminal")
@pytest.mark.parametrize("entry,answer", [("cli", "2"), ("env", "2"), ("cli", "q"), ("env", "q")])
def test_terminal_selection_keeps_prompts_out_of_stdout(environment, entry, answer):
    import pty

    master, slave = pty.openpty()
    command = (["bash", str(RUNTIME / "gms-remote-test.sh"), "gms-rt-auth-credential-mode"]
               if entry == "cli" else [sys.executable, str(RUNTIME / "gms-agent"), "profile", "env"])
    process = subprocess.Popen(command, env=environment, stdin=slave, stderr=slave,
                               stdout=subprocess.PIPE, text=True)
    os.close(slave)
    try:
        deadline, prompts = time.monotonic() + 10, b""
        while "选择编号".encode() not in prompts and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                prompts += os.read(master, 65536)
        assert "选择编号".encode() in prompts, prompts.decode(errors="replace")
        assert b"codex-one" in prompts and b"kimi-two" in prompts
        os.write(master, (answer + "\n").encode())
        stdout, _ = process.communicate(timeout=10)
        if answer == "q":
            assert process.returncode == 2
            assert stdout == ""
            return
        assert process.returncode == 0, stdout
        if entry == "cli":
            assert json.loads(stdout)["token_file"].endswith("/kimi-two.token")
        else:
            assert stdout == "export GMS_RT_PROFILE=kimi-two\n"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
