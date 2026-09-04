import subprocess
from foundation.command_result import CommandResult
from types import SimpleNamespace
from unittest.mock import patch

from features.firmware.firmware_api import _normalize_firmware_devices
from features.firmware.firmware_validation import (
    validate_local_update_image,
    validate_remote_update_image,
)


def test_firmware_device_ids_are_deduplicated_and_shell_safe():
    devices, invalid = _normalize_firmware_devices([
        " RK3576-1 ",
        "RK3576-1",
        "worker:SERIAL_2",
    ])

    assert devices == ["RK3576-1", "worker:SERIAL_2"]
    assert invalid == []


def test_firmware_device_ids_reject_shell_metacharacters():
    devices, invalid = _normalize_firmware_devices([
        "RK3576-1;touch /tmp/pwned",
    ])

    assert devices == []
    assert invalid == ["RK3576-1;touch /tmp/pwned"]


def test_local_preflight_reports_loader_hash_failure():
    completed = SimpleNamespace(
        returncode=255,
        stdout="Loading firmware failed!\nNote:wrong hash of loader,please check loader\n",
    )
    with patch("subprocess.run", return_value=completed) as run:
        result = validate_local_update_image("/tools/upgrade_tool", "/tmp/update.img")

    assert result.valid is False
    assert "Loader 哈希校验失败" in result.message
    assert "设备尚未重启" in result.message
    run.assert_called_once_with(
        ["/tools/upgrade_tool", "SFI", "/tmp/update.img"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=120,
        encoding="utf-8",
        errors="replace",
    )


def test_remote_preflight_quotes_paths_and_accepts_valid_image():
    class FakeSshManager:
        def __init__(self):
            self.command = ""

        def execute_command(self, _ssh, command, timeout=None):
            self.command = command
            assert timeout == 120
            return CommandResult(stdout="Type:Update Firmware\nEntry Count:20\n", stderr="", code=0)

    manager = FakeSshManager()
    result = validate_remote_update_image(
        manager,
        object(),
        "/home/hcq/GMS Suite/upgrade_tool",
        "/home/hcq/GMS Suite/update image.img",
    )

    assert result.valid is True
    assert manager.command == (
        "'/home/hcq/GMS Suite/upgrade_tool' SFI "
        "'/home/hcq/GMS Suite/update image.img'"
    )


def test_local_preflight_explains_unparseable_update_object():
    completed = SimpleNamespace(
        returncode=255,
        stdout="Type:Update Firmware\nFailed to Create update object\n",
    )
    with patch("subprocess.run", return_value=completed):
        result = validate_local_update_image("upgrade_tool", "update.img")

    assert result.valid is False
    assert "无法创建该 update.img 的解析对象" in result.message
    assert "按内容指纹建立新会话" in result.message
    assert "afptool/rkImageMaker" in result.message


def test_local_preflight_timeout_is_actionable():
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(["upgrade_tool", "SFI"], 120),
    ):
        result = validate_local_update_image("upgrade_tool", "update.img")

    assert result.valid is False
    assert "预检超时" in result.message
    assert "设备尚未重启" in result.message
