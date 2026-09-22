from __future__ import annotations

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

from features.devices import bootloader_api
from foundation.command_result import CommandResult


class _RecordingSSHManager:
    """SSH manager fake: the uploaded script fails with a fixed result."""

    def __init__(
        self,
        script_result: CommandResult,
        fastboot_listing: str,
    ) -> None:
        self.script_result = script_result
        self.fastboot_listing = fastboot_listing
        self.commands: list[str] = []

    @contextlib.contextmanager
    def connection(self, _config):
        @contextlib.contextmanager
        def _sftp():
            yield SimpleNamespace(put=lambda local, remote: None)

        yield SimpleNamespace(open_sftp=lambda: _sftp())

    def execute_command(self, _ssh, cmd, timeout=0):
        self.commands.append(cmd)
        if cmd.startswith("bash "):
            return self.script_result
        if cmd == "fastboot devices":
            return CommandResult(stdout=self.fastboot_listing, code=0)
        return CommandResult(code=0)


def _patch_lock_runtime(monkeypatch, tmp_path: Path, manager) -> None:
    runtime_obj = bootloader_api.runtime.get_runtime()
    repo_root = Path(bootloader_api.__file__).resolve().parents[2]
    monkeypatch.setattr(runtime_obj, "ssh_manager", manager)
    monkeypatch.setattr(
        runtime_obj,
        "config_manager",
        SimpleNamespace(
            load_config=lambda: {},
            get_ubuntu_user=lambda config: "",
        ),
    )
    monkeypatch.setattr(runtime_obj, "project_root", repo_root)
    monkeypatch.setattr(
        bootloader_api,
        "default_suites_path",
        lambda config, user: str(tmp_path),
    )
    monkeypatch.setattr("time.sleep", lambda *_args: None)


def test_failed_lock_script_records_failure_and_recovers_device(
    monkeypatch, tmp_path,
) -> None:
    """脚本失败时必须产生失败记录（历史 bug：结果被静默丢弃、误报成功），
    并尽力把滞留在 fastboot 的设备重启回系统。"""
    manager = _RecordingSSHManager(
        script_result=CommandResult(
            stderr="设备 RK3562GMS1 未在 60s 内进入 fastbootd", code=1,
        ),
        fastboot_listing="RK3562GMS1\tfastboot\n",
    )
    _patch_lock_runtime(monkeypatch, tmp_path, manager)

    response = bootloader_api._run_bootloader_lock_block(
        {}, ["RK3562GMS1"], "lock",
    )
    payload = json.loads(response.body)

    assert payload["success"] is False
    results = payload["details"]["results"]
    assert [item["success"] for item in results] == [False]
    assert "RK3562GMS1" in results[0]["error"]
    # oem 命令在 Python 侧执行（版本未知走旧命令）。
    assert any(
        "oem at-lock-vboot" in cmd for cmd in manager.commands
    ), manager.commands
    # 失败后设备被尽力重启回系统，而不是留在 fastboot“无法开机”。
    assert "fastboot -s RK3562GMS1 reboot" in manager.commands


def test_operation_without_per_device_results_is_not_success() -> None:
    response = bootloader_api._bootloader_operation_response([], "lock")
    payload = json.loads(response.body)

    assert response.status_code == 502
    assert payload["success"] is False
    assert payload["code"] == "UPSTREAM_FAILURE"
    assert payload["details"]["summary"]["total"] == 0
    assert payload["next_actions"]


def test_adb_ready_requires_exact_successful_device_state() -> None:
    assert bootloader_api._adb_state_is_ready("device\n", 0)
    assert not bootloader_api._adb_state_is_ready("error: device not found\n", 0)
    assert not bootloader_api._adb_state_is_ready("device\n", 1)


def test_failed_bootloader_result_is_not_reported_as_success() -> None:
    response = bootloader_api._bootloader_operation_response(
        [
            {
                "device": "RK3572GMS1",
                "success": False,
                "error": "Bootloader remains locked",
            }
        ],
        "unlock",
    )
    payload = json.loads(response.body)

    assert response.status_code == 502
    assert payload["success"] is False
    assert payload["code"] == "UPSTREAM_FAILURE"
    assert payload["details"]["summary"]["failed"] == 1
    assert "Bootloader remains locked" in payload["error"]
