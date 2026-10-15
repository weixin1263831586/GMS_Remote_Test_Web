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
