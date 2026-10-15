"""Human approval follows command intent; Agent credentials never become cookies."""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
from pathlib import Path

import pytest
from test_cli_entrypoints import RUNTIME, cli_context, profile_store, run_cli


ARGS = ["gms-rt-approval-create", "--tool", "gms_rt_shell_exec", "--device", "SERIAL",
        "--command", "pm clear com.android.launcher3"]


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_store, "PROFILE_ROOT", tmp_path / "config/gms-agent/profiles")
    monkeypatch.setattr(profile_store, "STATE_DIR", tmp_path / "state/gms-remote-test")
    for name, client in (("codex-one", "codex"), ("kimi-two", "kimi")):
        profile_store.write_profile_toml(name, client, "https://controller.example:5001", "")
        token = profile_store.token_file(name)
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("fixture-" + name)
        token.chmod(0o600)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GMS_")}
    env.update({"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
                "XDG_STATE_HOME": str(tmp_path / "state"), "NO_COLOR": "1"})
    return env


@pytest.fixture
def transport(environment, tmp_path):
    stub = tmp_path / "bin/curl"
    stub.parent.mkdir()
    stub.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
from urllib.parse import urlsplit
args = sys.argv[1:]
url = next(a for a in args if a.startswith('https://'))
path = urlsplit(url).path
cookie = Path(args[args.index('-c') + 1]) if '-c' in args else None
with open(os.environ['FAKE_HTTP_LOG'], 'a') as log:
    log.write(json.dumps({'path': path, 'host': urlsplit(url).netloc,
                         'bearer': any(a.startswith('@') for a in args),
                         'cookie': str(cookie) if cookie else ''}) + '\\n')
if path == '/api/auth/status':
    result = {'authenticated': bool(cookie and cookie.exists()), 'credential_mode': 'session_cookie',
              'user': {'username': 'gms'}}
elif path == '/api/auth/login':
    cookie.write_text('fixture-human-session')
    result = {'success': True, 'authenticated': True, 'user': {'username': 'gms'}}
elif path == '/api/auth/approval-tokens':
    if cookie and cookie.exists():
        result = {'success': True, 'approval': {'token': 'fixture-approval', 'expires_in': 300}}
    else:
        print(json.dumps({'success': False, 'error': 'human login required'}))
        print('HTTP_STATUS:401')
        raise SystemExit()
else:
    raise AssertionError(path)
print(json.dumps(result))
print('HTTP_STATUS:200')
''')
    stub.chmod(0o755)
    return dict(environment, PATH=str(stub.parent) + os.pathsep + environment.get("PATH", os.defpath),
                FAKE_HTTP_LOG=str(tmp_path / "http.jsonl"))


def terminal(env, args, *, login=False, choose=False):
    import pty

    master, slave = pty.openpty()
    process = subprocess.Popen(["bash", str(RUNTIME / "gms-remote-test.sh"), *args],
                               stdin=slave, stderr=slave, stdout=subprocess.PIPE, env=env, text=True)
    os.close(slave)
    output = b""
    sent = set()
    try:
        deadline = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            if not select.select([master], [], [], .1)[0]:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            output += chunk
            for prompt, answer, allowed in (
                ("选择编号", "2\n", choose), ("Username [gms]:", "\n", login),
                ("Password:", "fixture-login-password\n", login),
            ):
                if prompt.encode() in output and prompt not in sent:
                    assert allowed, output.decode(errors="replace")
                    os.write(master, answer.encode())
                    sent.add(prompt)
        stdout, _ = process.communicate(timeout=2)
        assert process.returncode == 0, stdout + output.decode(errors="replace")
        assert "fixture-login-password" not in stdout + output.decode(errors="replace")
        return stdout, output.decode(errors="replace")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


def calls(env):
    path = Path(env["FAKE_HTTP_LOG"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_approval_logs_in_once_and_reuses_controller_cookie_across_profiles(transport):
    first, prompts = terminal(transport, [*ARGS, "--profile", "codex-one"], login=True)
    second, again = terminal(transport, [*ARGS, "--profile", "kimi-two"])
    assert "fixture-approval" in first and "fixture-approval" in second
    assert "Username [gms]" in prompts and "Username" not in again
    records = calls(transport)
    assert [row["path"] for row in records] == ["/api/auth/status", "/api/auth/login", "/api/auth/approval-tokens",
                                               "/api/auth/status", "/api/auth/approval-tokens"]
    assert not any(row["bearer"] for row in records)
    assert len({row["cookie"] for row in records}) == 1
    assert "/human-" in records[0]["cookie"]
    Path(records[0]["cookie"]).unlink()  # Simulate a lost/expired human session.
    terminal(transport, ARGS, login=True)
    assert sum(row["path"] == "/api/auth/login" for row in calls(transport)) == 2


def test_same_controller_distinct_tokens_do_not_require_agent_selection(transport):
    _, prompts = terminal(transport, ARGS, login=True)
    assert "选择编号" not in prompts
    assert not any(row["bearer"] for row in calls(transport))


def test_human_context_ignores_ambient_and_discovered_agent_tokens(transport):
    context = profile_store.resolve_direct_cli_context("kimi-two", human_session=True)
    token = profile_store.STATE_DIR / (context["profile"] + ".token")
    token.write_text("fixture-unsafe-token")
    token.chmod(0o644)
    env = dict(transport, GMS_AUTH_TOKEN_FILE=str(profile_store.token_file("kimi-two")))
    terminal(env, [*ARGS, "--profile", "kimi-two"], login=True)
    assert not any(row["bearer"] for row in calls(env))
    assert token.read_text() == "fixture-unsafe-token"


def test_multiple_controllers_require_a_choice_and_keep_cookies_separate(transport):
    profile_store.write_profile_toml("remote", "codex", "https://other.example:5001", "")
    terminal(transport, [*ARGS, "--profile", "codex-one"], login=True)
    _, prompts = terminal(transport, ARGS, login=True, choose=True)
    assert "Controller/TLS" in prompts
    records = calls(transport)
    assert records[-1]["host"] == "other.example:5001"
    assert records[0]["cookie"] != records[-1]["cookie"]


@pytest.mark.parametrize("args,agent", [([*ARGS, "--json"], False), ([*ARGS, "--non-interactive"], False), (ARGS, True)])
def test_automated_approval_never_switches_to_human(environment, monkeypatch, args, agent):
    monkeypatch.setattr(cli_context, "interactive_allowed", lambda: not agent)
    monkeypatch.setattr(cli_context, "choose_profile", lambda *_: pytest.fail("must not ask for credentials"))
    monkeypatch.delenv("GMS_RT_HUMAN_SESSION", raising=False)
    context = cli_context.resolve_cli_context("kimi-two", "", args)
    assert context["mode"] == "profile" and context["token_file"].endswith("kimi-two.token")


def test_explicit_human_noninteractive_call_does_not_prompt_or_use_bearer(transport):
    result = run_cli(dict(transport, GMS_RT_HUMAN_SESSION="1"), *ARGS, "--profile", "kimi-two", "--json")
    assert result.returncode == 3
    assert json.loads(result.stdout)["ok"] is False
    assert [row["path"] for row in calls(transport)] == ["/api/auth/approval-tokens"]
    assert calls(transport)[0]["bearer"] is False


def test_agent_process_never_inherits_parent_human_cookie_mode(environment, monkeypatch):
    monkeypatch.setenv("GMS_AGENT_PROCESS", "1")
    monkeypatch.setenv("GMS_RT_HUMAN_SESSION", "1")
    context = profile_store.resolve_direct_cli_context("kimi-two")
    assert context["mode"] == "profile" and context["token_file"].endswith("kimi-two.token")


def test_human_profile_cannot_cross_controller_binding(environment, monkeypatch):
    monkeypatch.setattr(cli_context, "interactive_allowed", lambda: True)
    context = cli_context.resolve_cli_context("kimi-two", "https://other.example:5001", ARGS)
    assert context["mode"] == "error" and "拒绝向" in context["error"]
