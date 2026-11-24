"""Remote firmware probes must not look like a missing file."""

import asyncio
import os
from unittest.mock import patch

import pytest

from features.firmware import api_helpers, firmware_api
from features.firmware.upload_transport import controller_firmware_for_source_flash
from foundation.command_result import CommandResult
from foundation.error_model import ApiError


class _Ssh:
    def __init__(self, result: CommandResult):
        self.result = result
        self.commands = []

    def execute_command(self, _ssh, command, timeout=None):
        self.commands.append(command)
        return self.result


def test_missing_marker_means_the_file_is_absent(monkeypatch):
    manager = _Ssh(CommandResult(
        stdout="__GMS_REMOTE_FILE_MISSING__\n", stderr="", code=0,
    ))
    monkeypatch.setattr(api_helpers.runtime, "ssh_manager", manager)

    assert api_helpers.remote_file_exists(object(), "/suite/update.img") is False


def test_probe_timeout_is_not_a_missing_file(monkeypatch):
    manager = _Ssh(CommandResult(stdout="", stderr="slow", code=-1, timed_out=True))
    monkeypatch.setattr(api_helpers.runtime, "ssh_manager", manager)

    with pytest.raises(ApiError) as raised:
        api_helpers.remote_file_exists(object(), "/suite/update.img")

    assert raised.value.status_code == 504
    assert "slow" not in raised.value.message


def test_probe_transport_failure_is_not_a_missing_file(monkeypatch):
    manager = _Ssh(CommandResult(stdout="", stderr="reset", code=-1))
    monkeypatch.setattr(api_helpers.runtime, "ssh_manager", manager)

    with pytest.raises(ApiError) as raised:
        api_helpers.remote_file_exists(object(), "/suite/update.img")

    assert raised.value.status_code == 502
    assert "reset" not in raised.value.message


def test_digest_timeout_is_504(monkeypatch):
    manager = _Ssh(CommandResult(stdout="", stderr="", code=-1, timed_out=True))
    monkeypatch.setattr(firmware_api.runtime, "ssh_manager", manager)

    with pytest.raises(ApiError) as raised:
        asyncio.run(firmware_api._remote_sha256(object(), "/suite/update.img"))

    assert raised.value.code == "DEPENDENCY_TIMEOUT"


def test_digest_missing_file_is_404(monkeypatch):
    manager = _Ssh(CommandResult(
        stdout="", stderr="sha256sum: /suite/update.img: No such file\n", code=1,
    ))
    monkeypatch.setattr(firmware_api.runtime, "ssh_manager", manager)

    with pytest.raises(ApiError) as raised:
        asyncio.run(firmware_api._remote_sha256(object(), "/suite/update.img"))

    assert raised.value.code == "NOT_FOUND"


def test_digest_transport_failure_is_not_a_mismatch(monkeypatch):
    manager = _Ssh(CommandResult(stdout="", stderr="connection reset", code=-1))
    monkeypatch.setattr(firmware_api.runtime, "ssh_manager", manager)

    with pytest.raises(ApiError) as raised:
        asyncio.run(firmware_api._remote_sha256(object(), "/suite/update.img"))

    assert raised.value.code == "UPSTREAM_FAILURE"
    assert "connection reset" not in raised.value.message


def test_source_flash_uses_controller_file_not_test_host_path(tmp_path):
    local = tmp_path / "update.img"
    local.write_bytes(b"image")

    path, temporary = asyncio.run(controller_firmware_for_source_flash(
        object(), "/suite/update.img", str(local),
    ))

    assert path == str(local)
    assert temporary is None


def test_source_flash_downloads_when_only_the_test_host_has_the_image():
    downloaded = {}

    async def fake_download(_ssh, remote_path, local_path):
        downloaded["remote"] = remote_path
        downloaded["local"] = local_path

    with patch(
        "features.firmware.upload_transport.download_remote_firmware",
        new=fake_download,
    ):
        path, temporary = asyncio.run(controller_firmware_for_source_flash(
            object(), "/suite/update.img", None,
        ))
    try:
        assert path == temporary
        assert path != "/suite/update.img"
        assert downloaded == {"remote": "/suite/update.img", "local": path}
        assert path.startswith("/tmp/")
    finally:
        if path:
            os.remove(path)
