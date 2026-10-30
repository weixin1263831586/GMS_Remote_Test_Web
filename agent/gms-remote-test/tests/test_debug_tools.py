from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ("runtime" if (ROOT / "runtime").is_dir() else "scripts")
spec = importlib.util.spec_from_file_location("debug_tools_test", RUNTIME / "mcp_debug_tools.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_session_request_uses_exact_target_and_no_copied_credential():
    run = Mock(return_value=("{}", False))
    module.handlers(run)["gms_rt_debug_session_request"]({
        "worker_id": "worker", "device": "SERIAL", "package": "com.example.app", "apk_file": "/tmp/a.apk",
        "duration_minutes": 45, "operations": ["install", "launch", "tap"],
    })
    run.assert_called_once_with("gms-rt-debug-session-request", [
        "worker", "SERIAL", "com.example.app", "/tmp/a.apk", "--minutes", "45", "--operations", "install,launch,tap",
    ], timeout=300)
    for schema in module.SCHEMAS:
        assert "approval_token" not in schema["inputSchema"]["properties"]
        assert "command" not in schema["inputSchema"]["properties"]


def test_action_preserves_zero_coordinates_and_text_as_argv():
    run = Mock(return_value=("{}", False))
    module.handlers(run)["gms_rt_debug_action"]({"session_id": "a" * 32, "action": "tap", "x": 0, "y": 20})
    run.assert_called_once_with("gms-rt-debug-action", ["a" * 32, "tap", "--x", "0", "--y", "20"], timeout=90)


def test_installed_session_needs_no_host_apk():
    run = Mock(return_value=("{}", False))
    module.handlers(run)["gms_rt_debug_session_request"]({
        "worker_id": "worker", "device": "SERIAL", "package": "com.android.vending", "installed": True,
        "operations": ["launch", "tap", "text"],
    })
    run.assert_called_once_with("gms-rt-debug-session-request", [
        "worker", "SERIAL", "com.android.vending", "--installed", "--operations", "launch,tap,text",
    ], timeout=300)


@pytest.mark.parametrize("values", [
    {}, {"installed": True, "apk_file": "/device.apk"}, {"installed": True, "operations": ["install"]},
    {"installed": "true"}, {"installed": True, "operations": None},
])
def test_invalid_identity_source_is_actionable_and_never_runs(values):
    run = Mock()
    message, failed = module.handlers(run)["gms_rt_debug_session_request"]({
        "worker_id": "worker", "device": "SERIAL", "package": "com.android.vending", **values,
    })
    assert failed and message
    run.assert_not_called()


@pytest.mark.parametrize("arguments, expected", [
    ("", "Usage:"),
    ("worker SERIAL com.app /missing.apk", "Agent host"),
    ("worker SERIAL com.app --installed --minutes 0", "15 to 120"),
    ("worker SERIAL com.app --installed --minutes abc", "15 to 120"),
    ("worker SERIAL com.app --installed --operations install", "cannot grant install"),
    ("worker SERIAL com.app --installed --operations launch,unknown", "Unsupported debug operation"),
    ("worker SERIAL com.app --installed --minutes", "Missing value"),
    ("worker SERIAL com.app --installed --unknown value", "Unknown debug-session option"),
])
def test_cli_request_reports_validation_errors_without_http(tmp_path, arguments, expected):
    result = subprocess.run(["bash", "-c", '''source "$GMS_TEST_RUNTIME/cli/debug.sh"
error() { printf '%s\\n' "$1" >&2; }
api_call() { touch unexpected-http; }
gms-rt-debug-session-request ''' + arguments], cwd=tmp_path, capture_output=True, text=True, check=False,
        env={**os.environ, "GMS_TEST_RUNTIME": str(RUNTIME), "GMS_RT_EXIT_USAGE": "2"})
    assert result.returncode == 2
    assert expected in result.stderr
    assert not (tmp_path / "unexpected-http").exists()


def test_cli_installed_request_does_not_upload_or_install(tmp_path):
    result = subprocess.run(["bash", "-c", '''source "$GMS_TEST_RUNTIME/cli/debug.sh"
api_call() { printf '%s\\n' "$@"; }
gms-rt-debug-session-request worker SERIAL com.android.vending --installed
'''], cwd=tmp_path, capture_output=True, text=True, check=False,
        env={**os.environ, "GMS_TEST_RUNTIME": str(RUNTIME), "GMS_RT_EXIT_USAGE": "2"})
    assert result.returncode == 0, result.stderr
    assert "installed=true" in result.stdout
    assert "operations=launch,stop,tap,swipe,text,keyevent" in result.stdout
    assert "apk_file=" not in result.stdout


def test_repeated_install_passes_session_reference():
    spec = importlib.util.spec_from_file_location("deploy_tools_debug_test", RUNTIME / "mcp_deployment_tools.py")
    deployment = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deployment)
    run = Mock(return_value=("{}", False))
    deployment.handlers(run)["gms_rt_apk_deploy"]({"worker_id": "worker", "device": "SERIAL", "apk_file": "/tmp/a.apk",
                                                "debug_session_id": "a" * 32})
    run.assert_called_once_with("gms-rt-apk-deploy", ["worker", "SERIAL", "/tmp/a.apk", "--debug-session", "a" * 32], timeout=300)


@pytest.mark.parametrize("arguments", [
    {"session_id": "a" * 32}, {},
])
def test_incomplete_actions_never_run(arguments):
    run = Mock()
    assert module.handlers(run)["gms_rt_debug_action"](arguments)[1]
    run.assert_not_called()


def test_cli_serializes_input_as_json_without_shell_interpretation(tmp_path):
    command = """source \"$GMS_TEST_RUNTIME/cli/debug.sh\"
api_call() { printf '%s\\n' \"$1\" \"$2\" \"$3\"; }
gms-rt-debug-action aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa text --text 'literal $(touch injected)'
"""
    result = subprocess.run(["bash", "-c", command], cwd=tmp_path, capture_output=True, text=True, check=False,
                            env={**os.environ, "GMS_TEST_RUNTIME": str(RUNTIME), "GMS_RT_EXIT_USAGE": "2"})
    assert result.returncode == 0
    assert '"text":"literal $(touch injected)"' in result.stdout
    assert not (tmp_path / "injected").exists()
