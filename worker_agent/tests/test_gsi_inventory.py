from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from worker_agent.inventory import flash_gsi


def test_worker_gsi_uses_python_preparation_and_thin_script(tmp_path: Path) -> None:
    firmware_dir = tmp_path / "firmware"
    firmware_dir.mkdir()
    system_img = firmware_dir / "system.img"
    vendor_img = firmware_dir / "boot-debug.img"
    system_img.write_bytes(b"system")
    vendor_img.write_bytes(b"vendor")
    config = SimpleNamespace(data_root=tmp_path)
    prepared = SimpleNamespace(oem_argument=lambda action: "board:unlock")
    completed = subprocess.CompletedProcess(
        [],
        0,
        stdout="done",
        stderr="",
    )

    with (
        patch(
            "worker_agent.inventory.probe_devices",
            return_value=[{"serial": "RK3572GMS1"}],
        ),
        patch("worker_agent.inventory.FastbootPreparer") as preparer,
        patch(
            "worker_agent.inventory.subprocess.run",
            return_value=completed,
        ) as run,
    ):
        preparer.return_value.prepare_bootloader.return_value = prepared
        result = flash_gsi(
            config,
            system_img,
            vendor_img,
            ["RK3572GMS1"],
        )

    argv = run.call_args.args[0]
    assert argv[1:5] == [
        "RK3572GMS1",
        "board:unlock",
        str(system_img),
        str(Path("tools/misc.img").resolve()),
    ]
    assert argv[5:] == ["boot", str(vendor_img)]
    assert result["success"] is True


def test_worker_gsi_accepts_vendor_only_image(tmp_path: Path) -> None:
    firmware_dir = tmp_path / "firmware"
    firmware_dir.mkdir()
    vendor_img = firmware_dir / "vendor_boot.img"
    vendor_img.write_bytes(b"vendor")
    config = SimpleNamespace(data_root=tmp_path)
    prepared = SimpleNamespace(oem_argument=lambda action: "board:unlock")

    with (
        patch(
            "worker_agent.inventory.probe_devices",
            return_value=[{"serial": "RK3572GMS1"}],
        ),
        patch("worker_agent.inventory.FastbootPreparer") as preparer,
        patch(
            "worker_agent.inventory.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout="done", stderr=""),
        ) as run,
    ):
        preparer.return_value.prepare_bootloader.return_value = prepared
        result = flash_gsi(config, None, vendor_img, ["RK3572GMS1"])

    argv = run.call_args.args[0]
    assert argv[3] == ""
    assert argv[5:] == ["vendor_boot", str(vendor_img)]
    assert result["success"] is True
