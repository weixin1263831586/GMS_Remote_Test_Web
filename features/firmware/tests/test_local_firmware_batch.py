from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from features.firmware import firmware_api, runtime
from foundation.command_result import CommandResult


def test_local_batch_flashes_each_device_and_returns_per_device_results():
    commands: list[str] = []

    def execute(_ssh, command, timeout=None):
        commands.append(command)
        return CommandResult(code=0)

    async def run_stream(*_args, **_kwargs):
        return CommandResult(stdout="Upgrade firmware ok.", code=0)

    with patch.object(runtime, "ssh_manager", SimpleNamespace(execute_command=execute)), patch(
        "features.firmware.local_flash.wait_for_single_rockusb_loader",
        new=AsyncMock(return_value=(True, "")),
        ), patch(
        "features.firmware.local_flash.wait_for_rockusb_loader_exit",
        new=AsyncMock(return_value=(True, "")),
    ), patch(
        "foundation.ssh_executor.ssh_executor.run_stream",
        new=run_stream,
    ):
        results, error = asyncio.run(firmware_api._run_local_firmware_batch(
            ssh=object(), devices=["D1", "D2"],
            protocols={"D1": "adb", "D2": "adb"},
            suite_dir="/suite", remote_tool="/suite/.tool",
            remote_firmware="/suite/update.img", client_id="client",
        ))

    assert error is None
    assert [item["device"] for item in results] == ["D1", "D2"]
    assert all(item["success"] for item in results)
    assert commands == ["adb -s D1 reboot loader", "adb -s D2 reboot loader"]


def test_local_batch_marks_remaining_devices_skipped_after_failure():
    calls = 0

    async def run_stream(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return CommandResult(stderr="tool failed", code=7)

    with patch.object(runtime, "ssh_manager", SimpleNamespace(
        execute_command=lambda *_args, **_kwargs: CommandResult(code=0),
    )), patch(
        "features.firmware.local_flash.wait_for_single_rockusb_loader",
        new=AsyncMock(return_value=(True, "")),
        ), patch(
        "features.firmware.local_flash.wait_for_rockusb_loader_exit",
        new=AsyncMock(return_value=(True, "")),
    ), patch(
        "foundation.ssh_executor.ssh_executor.run_stream",
        new=run_stream,
    ):
        results, error = asyncio.run(firmware_api._run_local_firmware_batch(
            ssh=object(), devices=["D1", "D2"],
            protocols={"D1": "adb", "D2": "adb"},
            suite_dir="/suite", remote_tool="/suite/.tool",
            remote_firmware="/suite/update.img", client_id="client",
        ))

    assert calls == 1
    assert error == "tool failed"
    assert results[0]["stage"] == "FLASHING"
    assert results[1] == {
        "device": "D2", "success": False, "stage": "SKIPPED",
        "error": "previous device failed",
    }
