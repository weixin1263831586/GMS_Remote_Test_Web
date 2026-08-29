from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from features.firmware import fastboot_firmware
from features.firmware.partition_burn import SfiEntry
from worker_agent.fastboot_workflow import PreparedFastbootDevice


def test_build_fastboot_plan_keeps_android_partitions_and_skips_rockchip() -> None:
    entries = [
        SfiEntry("MiniLoaderAll.bin", "", "", 1024),
        SfiEntry("parameter.txt", "parameter", "parameter", 512),
        SfiEntry("uboot.img", "uboot_a", "image", 4096),
        SfiEntry("misc.img", "misc", "image", 4096),
        SfiEntry("boot.img", "boot_a", "image", 8 * 1024 * 1024),
        SfiEntry("vbmeta.img", "vbmeta_a", "image", 4096),
        SfiEntry("super.img", "super", "sparse image", 1024 * 1024),
    ]

    steps, skipped = fastboot_firmware.build_fastboot_write_plan(entries)

    assert [step.partition for step in steps] == ["super", "boot_a", "vbmeta_a"]
    assert any("uboot_a" in item and "upgrade_tool" in item for item in skipped)
    assert any("misc" in item and "upgrade_tool" in item for item in skipped)


def test_build_fastboot_plan_requires_super() -> None:
    with pytest.raises(fastboot_firmware.FastbootFirmwareError, match="包含super"):
        fastboot_firmware.build_fastboot_write_plan([
            SfiEntry("boot.img", "boot_a", "image", 4096),
        ])


def test_build_fastboot_plan_rejects_path_traversal() -> None:
    with pytest.raises(fastboot_firmware.FastbootFirmwareError, match="不安全"):
        fastboot_firmware.build_fastboot_write_plan([
            SfiEntry("../super.img", "super", "sparse image", 4096),
        ])


def test_parse_android_sparse_expanded_size() -> None:
    header = struct.pack(
        "<IHHHHIIII",
        fastboot_firmware.ANDROID_SPARSE_MAGIC,
        1,
        0,
        28,
        12,
        4096,
        100,
        2,
        0,
    )

    assert fastboot_firmware.parse_android_sparse_expanded_size(header) == 409600
    assert fastboot_firmware.parse_android_sparse_expanded_size(b"not sparse") is None


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("(boot_a) partition-size: 0x4000000", 0x4000000),
        ("partition-size:super: 5872025600", 5872025600),
        ("FAILED (remote: partition does not exist)", None),
    ],
)
def test_parse_fastboot_partition_size(output: str, expected: int | None) -> None:
    assert fastboot_firmware.parse_fastboot_partition_size(output) == expected


def test_required_partition_policy_keeps_metadata_optional() -> None:
    assert fastboot_firmware.is_required_fastboot_partition("super")
    assert fastboot_firmware.is_required_fastboot_partition("boot_a")
    assert fastboot_firmware.is_required_fastboot_partition("vendor_boot_b")
    assert fastboot_firmware.is_required_fastboot_partition("init_boot_a")
    assert not fastboot_firmware.is_required_fastboot_partition("dtbo_a")
    assert not fastboot_firmware.is_required_fastboot_partition("vbmeta_b")


def test_preflight_checks_every_partition_before_first_flash() -> None:
    steps = [
        fastboot_firmware.FastbootWriteStep(
            "super", "super.img", "sparse image", 1024,
        ),
        fastboot_firmware.FastbootWriteStep(
            "boot_a", "boot.img", "image", 2048,
        ),
    ]
    commands: list[str] = []

    class FakeSshManager:
        def execute_command(self, _ssh, command, timeout=None):
            commands.append(command)
            if "partition-size:super" in command:
                return "", "(super) partition-size: 0x100000", 0
            if "partition-size:boot_a" in command:
                return "", "FAILED (remote: partition does not exist)", 1
            if " flash " in command:
                raise AssertionError("flash must not start before full preflight")
            return "", "", 0

    with patch.object(
        fastboot_firmware,
        "_extract_update_image",
        new=AsyncMock(return_value=("/suite/fw_extract/update", steps, [])),
    ), patch.object(
        fastboot_firmware,
        "_remote_image_expanded_size",
        new=AsyncMock(side_effect=[4096, 2048]),
    ), patch.object(
        fastboot_firmware.runtime,
        "ssh_manager",
        FakeSshManager(),
    ), patch.object(
        fastboot_firmware,
        "FastbootPreparer",
        return_value=SimpleNamespace(
            prepare_gsi_fastbootd=lambda _device: PreparedFastbootDevice(
                serial="D1", identity="rk3572",
            )
        ),
    ), pytest.raises(
        fastboot_firmware.FastbootFirmwareError,
        match="未暴露核心分区 boot_a",
    ):
        asyncio.run(fastboot_firmware.run_usbip_fastboot_firmware(
            object(),
            suite_dir="/suite",
            remote_tool="/suite/upgrade_tool",
            remote_firmware="/suite/update.img",
            devices=["D1"],
        ))

    assert not any(" flash " in command for command in commands)


def test_optional_unexposed_partition_is_skipped() -> None:
    steps = [
        fastboot_firmware.FastbootWriteStep(
            "super", "super.img", "sparse image", 1024,
        ),
        fastboot_firmware.FastbootWriteStep(
            "dtbo_a", "dtbo.img", "image", 2048,
        ),
    ]
    commands: list[str] = []

    class FakeSshManager:
        def execute_command(self, _ssh, command, timeout=None):
            commands.append(command)
            if "partition-size:super" in command:
                return "", "(super) partition-size: 0x100000", 0
            if "partition-size:dtbo_a" in command:
                return "", "FAILED (remote: partition does not exist)", 1
            return "OKAY", "", 0

    with patch.object(
        fastboot_firmware,
        "_extract_update_image",
        new=AsyncMock(return_value=("/suite/fw_extract/update", steps, [])),
    ), patch.object(
        fastboot_firmware,
        "_remote_image_expanded_size",
        new=AsyncMock(side_effect=[4096, 2048]),
    ), patch.object(
        fastboot_firmware.runtime,
        "ssh_manager",
        FakeSshManager(),
    ), patch.object(
        fastboot_firmware,
        "FastbootPreparer",
        return_value=SimpleNamespace(
            prepare_gsi_fastbootd=lambda _device: PreparedFastbootDevice(
                serial="D1", identity="rk3572",
            )
        ),
    ):
        result = asyncio.run(fastboot_firmware.run_usbip_fastboot_firmware(
            object(),
            suite_dir="/suite",
            remote_tool="/suite/upgrade_tool",
            remote_firmware="/suite/update.img",
            devices=["D1"],
        ))

    assert any(" flash super " in command for command in commands)
    assert not any(" flash dtbo_a " in command for command in commands)
    assert result["results"][0]["skipped_partitions"] == [
        "dtbo_a（设备Fastbootd未暴露，保留设备现有内容）"
    ]
