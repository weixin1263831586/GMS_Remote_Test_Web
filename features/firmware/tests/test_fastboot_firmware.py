from __future__ import annotations

import asyncio
import struct
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from features.firmware import fastboot_firmware
from features.firmware.partition_burn import (
    PartitionEntry,
    SfiEntry,
    parse_gpt_entries,
)
from worker_agent.fastboot_workflow import PreparedFastbootDevice


@pytest.fixture(autouse=True)
def _use_managed_fastboot_for_workflow_tests():
    with patch.object(
        fastboot_firmware,
        "_prepare_remote_fastboot",
        new=AsyncMock(return_value=(
            "/suite/.gms-platform-tools/fastboot",
            "fastboot version test",
        )),
    ):
        yield


def _partitions() -> list[PartitionEntry]:
    return [
        PartitionEntry("security", 0x2000, 0x2000),
        PartitionEntry("uboot_a", 0x4000, 0x4000),
        PartitionEntry("pvmfw_a", 0x8000, 0x800),
        PartitionEntry("dtbo_a", 0x8800, 0x2000),
        PartitionEntry("boot_a", 0xA800, 0x20000),
        PartitionEntry("vbmeta_a", 0x2A800, 0x800),
        PartitionEntry("super", 0x2B000, 0x10000),
        PartitionEntry("userdata", 0x3B000, 0, grow=True),
    ]


def _gpt_template(partitions: list[PartitionEntry]) -> bytes:
    sector_size = 512
    entry_count = 128
    entry_size = 128
    data = bytearray(34 * sector_size)
    table_offset = 2 * sector_size
    for index, partition in enumerate(partitions):
        offset = table_offset + index * entry_size
        data[offset:offset + 16] = bytes([index + 1]) * 16
        data[offset + 16:offset + 32] = bytes([0x80 + index]) * 16
        struct.pack_into("<Q", data, offset + 32, partition.offset_sec)
        last_lba = (
            0xFFFFFFDD
            if partition.grow
            else partition.offset_sec + partition.size_sec - 1
        )
        struct.pack_into("<Q", data, offset + 40, last_lba)
        name = partition.name.encode("utf-16-le")
        data[offset + 56:offset + 56 + len(name)] = name

    header = memoryview(data)[sector_size:2 * sector_size]
    header[:8] = b"EFI PART"
    struct.pack_into("<I", header, 8, 0x00010000)
    struct.pack_into("<I", header, 12, 92)
    struct.pack_into("<Q", header, 24, 1)
    struct.pack_into("<Q", header, 32, 0xFFFFFFFE)
    struct.pack_into("<Q", header, 40, 34)
    struct.pack_into("<Q", header, 48, 0xFFFFFFDD)
    header[56:72] = b"gms-gpt-test-id!"
    struct.pack_into("<Q", header, 72, 2)
    struct.pack_into("<I", header, 80, entry_count)
    struct.pack_into("<I", header, 84, entry_size)
    table_crc = zlib.crc32(data[table_offset:]) & 0xFFFFFFFF
    struct.pack_into("<I", header, 88, table_crc)
    struct.pack_into("<I", header, 16, 0)
    struct.pack_into("<I", header, 16, zlib.crc32(header[:92]) & 0xFFFFFFFF)
    return bytes(data)


def _firmware() -> fastboot_firmware.ExtractedFirmware:
    partitions = _partitions()
    plan = fastboot_firmware.FastbootWritePlan(
        bootloader=(
            fastboot_firmware.FastbootWriteStep(
                "dtbo_a", "dtbo.img", "image", 2048,
            ),
            fastboot_firmware.FastbootWriteStep(
                "boot_a", "boot.img", "image", 4096,
            ),
            fastboot_firmware.FastbootWriteStep(
                "vbmeta_a", "vbmeta.img", "image", 1024,
            ),
        ),
        fastbootd=(
            fastboot_firmware.FastbootWriteStep(
                "super", "super.img", "sparse image", 8192,
            ),
        ),
        skipped=("parameter.txt(非Fastboot分区条目: parameter)",),
    )
    return fastboot_firmware.ExtractedFirmware(
        extract_dir="/suite/fw_extract/update",
        plan=plan,
        partitions=tuple(partitions),
        gpt_template=_gpt_template(partitions),
    )


def test_build_plan_routes_all_payloads_without_silent_skip() -> None:
    partitions = _partitions()
    entries = [
        SfiEntry("MiniLoaderAll.bin", "", "", 1024),
        SfiEntry("parameter.txt", "parameter", "parameter", 512),
        SfiEntry("uboot.img", "uboot_a", "image", 4096),
        SfiEntry("dtbo.img", "dtbo_a", "image", 2048),
        SfiEntry("vbmeta.img", "vbmeta_a", "image", 1024),
        SfiEntry("boot.img", "boot_a", "image", 4096),
        SfiEntry("super.img", "super", "sparse image", 8192),
    ]

    plan = fastboot_firmware.build_fastboot_write_plan(entries, partitions)

    assert [step.partition for step in plan.bootloader] == [
        "uboot_a", "dtbo_a", "boot_a", "vbmeta_a",
    ]
    assert [step.partition for step in plan.fastbootd] == ["super"]
    assert all("dtbo" not in item and "vbmeta" not in item for item in plan.skipped)
    assert any("MiniLoaderAll.bin" in item for item in plan.skipped)


def test_fastboot_preflight_fails_before_firmware_extraction() -> None:
    extract = AsyncMock()
    with (
        patch.object(
            fastboot_firmware,
            "_prepare_remote_fastboot",
            new=AsyncMock(side_effect=fastboot_firmware.FastbootFirmwareError(
                "Fastboot preflight failed",
            )),
        ),
        patch.object(
            fastboot_firmware,
            "_extract_update_image",
            new=extract,
        ),
        pytest.raises(
            fastboot_firmware.FastbootFirmwareError,
            match="preflight failed",
        ),
    ):
        asyncio.run(fastboot_firmware.run_usbip_fastboot_firmware(
            object(),
            suite_dir="/suite",
            remote_tool="/suite/upgrade_tool",
            remote_firmware="/suite/update.img",
            devices=["D1"],
        ))

    extract.assert_not_awaited()


def test_build_plan_flashes_shared_dtbo_and_vbmeta_images_to_both_slots() -> None:
    partitions = [
        *_partitions(),
        PartitionEntry("dtbo_b", 0x40000, 0x2000),
        PartitionEntry("vbmeta_b", 0x42000, 0x800),
    ]
    entries = [
        SfiEntry("dtbo.img", "dtbo_a", "image", 0x400000),
        SfiEntry("dtbo.img", "dtbo_b", "image", 0x400000),
        SfiEntry("vbmeta.img", "vbmeta_a", "image", 0x2000),
        SfiEntry("vbmeta.img", "vbmeta_b", "image", 0x2000),
        SfiEntry("super.img", "super", "sparse image", 0x2000),
    ]

    plan = fastboot_firmware.build_fastboot_write_plan(entries, partitions)

    assert [step.partition for step in plan.bootloader] == [
        "dtbo_a", "dtbo_b", "vbmeta_a", "vbmeta_b",
    ]
    assert [step.partition for step in plan.fastbootd] == ["super"]
    assert plan.skipped == ()


def test_build_plan_rejects_unmapped_image() -> None:
    with pytest.raises(fastboot_firmware.FastbootFirmwareError, match="不在 parameter"):
        fastboot_firmware.build_fastboot_write_plan(
            [
                SfiEntry("vbmeta.img", "vbmeta_a", "image", 1024),
                SfiEntry("super.img", "super", "sparse image", 8192),
                SfiEntry("mystery.img", "mystery", "image", 1024),
            ],
            _partitions(),
        )


def test_build_plan_requires_super_and_vbmeta() -> None:
    with pytest.raises(fastboot_firmware.FastbootFirmwareError, match="包含super"):
        fastboot_firmware.build_fastboot_write_plan(
            [SfiEntry("boot.img", "boot_a", "image", 4096)],
            _partitions(),
        )
    with pytest.raises(fastboot_firmware.FastbootFirmwareError, match="没有vbmeta"):
        fastboot_firmware.build_fastboot_write_plan(
            [SfiEntry("super.img", "super", "sparse image", 8192)],
            _partitions(),
        )


def test_build_plan_rejects_path_traversal() -> None:
    with pytest.raises(fastboot_firmware.FastbootFirmwareError, match="不安全"):
        fastboot_firmware.build_fastboot_write_plan(
            [SfiEntry("../super.img", "super", "sparse image", 4096)],
            _partitions(),
        )


def test_extracted_size_check_allows_parameter_line_ending_normalization() -> None:
    entries = [
        SfiEntry("parameter.txt", "parameter", "parameter", 1017),
        SfiEntry("vbmeta.img", "vbmeta_a", "image", 8192),
        SfiEntry("super.img", "super", "sparse image", 4096),
    ]

    assert fastboot_firmware.extracted_file_size_mismatches(
        entries,
        {
            "parameter.txt": 1005,
            "vbmeta.img": 8192,
            "super.img": 4096,
        },
    ) == []
    assert fastboot_firmware.extracted_file_size_mismatches(
        entries,
        {
            "parameter.txt": 1005,
            "vbmeta.img": 8191,
            "super.img": 4096,
        },
    ) == ["vbmeta.img"]


def test_finalize_gpt_uses_real_disk_and_keeps_payloadless_pvmfw() -> None:
    partitions = _partitions()
    geometry = fastboot_firmware.StorageGeometry(0x1000000, 512)

    result = fastboot_firmware.finalize_gpt_image(
        _gpt_template(partitions), partitions, geometry,
    )

    header = result[512:1024]
    entries = {entry.name: entry for entry in parse_gpt_entries(result[512:])}
    entry_count = struct.unpack_from("<I", header, 80)[0]
    entry_size = struct.unpack_from("<I", header, 84)[0]
    table = result[1024:1024 + entry_count * entry_size]
    expected_last = geometry.total_sectors - 34
    assert entries["pvmfw_a"].first_lba == 0x8000
    assert entries["userdata"].last_lba == expected_last
    assert struct.unpack_from("<Q", header, 32)[0] == geometry.total_sectors - 1
    assert struct.unpack_from("<Q", header, 48)[0] == expected_last
    assert struct.unpack_from("<I", header, 88)[0] == zlib.crc32(table) & 0xFFFFFFFF
    header_copy = bytearray(header[:92])
    expected_header_crc = struct.unpack_from("<I", header_copy, 16)[0]
    struct.pack_into("<I", header_copy, 16, 0)
    assert expected_header_crc == zlib.crc32(header_copy) & 0xFFFFFFFF
    assert result[450] == 0xEE
    assert result[510:512] == b"\x55\xaa"


def test_finalize_gpt_converts_parameter_layout_for_4096_block_disk() -> None:
    partitions = _partitions()
    geometry = fastboot_firmware.StorageGeometry(0x1000000, 4096)

    result = fastboot_firmware.finalize_gpt_image(
        _gpt_template(partitions), partitions, geometry,
    )

    block_size = geometry.logical_block_size
    header = result[block_size:2 * block_size]
    entries = {
        name: (first_lba, last_lba)
        for name, first_lba, last_lba in
        fastboot_firmware._parse_target_gpt_entries(result, block_size)
    }
    total_blocks = geometry.total_sectors // (block_size // 512)
    expected_last = total_blocks - 6
    assert len(result) == 6 * block_size
    assert header[:8] == b"EFI PART"
    assert struct.unpack_from("<Q", header, 32)[0] == total_blocks - 1
    assert struct.unpack_from("<Q", header, 40)[0] == 6
    assert struct.unpack_from("<Q", header, 48)[0] == expected_last
    assert entries["security"] == (0x2000 // 8, 0x3FFF // 8)
    assert entries["pvmfw_a"] == (0x8000 // 8, 0x87FF // 8)
    assert entries["userdata"] == (0x3B000 // 8, expected_last)
    assert struct.unpack_from("<I", result, 458)[0] == total_blocks - 1
    assert result[450] == 0xEE
    assert result[510:512] == b"\x55\xaa"


def test_finalize_gpt_rejects_partition_not_aligned_to_logical_block() -> None:
    partitions = _partitions()
    partitions[0] = PartitionEntry("security", 0x2001, 0x2000)

    with pytest.raises(fastboot_firmware.FastbootFirmwareError, match="未按4096"):
        fastboot_firmware.finalize_gpt_image(
            _gpt_template(partitions),
            partitions,
            fastboot_firmware.StorageGeometry(0x1000000, 4096),
        )


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("GMS_GEOMETRY 249872384 512", (249872384, 512)),
        (
            "Capacity: 4 MiB (8192 x 512)\nCapacity: 119 GiB (249872384 x 512)",
            (249872384, 512),
        ),
        (
            "Capacity: 119 GiB (31234048 x 4096)",
            (249872384, 4096),
        ),
        ("no capacity", None),
    ],
)
def test_parse_storage_geometry(
    output: str, expected: tuple[int, int] | None,
) -> None:
    result = fastboot_firmware.parse_storage_geometry(output)
    actual = (
        (result.total_sectors, result.logical_block_size)
        if result is not None else None
    )
    assert actual == expected


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


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("(bootloader) max-download-size: 0x10000000", 0x10000000),
        ("max-download-size: 268435456", 268435456),
        ("FAILED (remote: unsupported command)", None),
    ],
)
def test_parse_fastboot_max_download(
    output: str,
    expected: int | None,
) -> None:
    assert fastboot_firmware.parse_fastboot_max_download(output) == expected


def test_segment_ranges_cover_image_once_and_stay_below_limit() -> None:
    block_size = 4096
    max_download = 256 * 1024
    total_size = 1024 * 1024

    ranges = fastboot_firmware.plan_segment_ranges(
        total_size,
        max_download,
        block_size=block_size,
        margin=4 * block_size,
    )

    assert ranges[0][0] == 0
    assert ranges[-1][1] == total_size
    assert [end for _start, end in ranges[:-1]] == [
        start for start, _end in ranges[1:]
    ]
    assert all(
        start % block_size == end % block_size == 0
        for start, end in ranges
    )
    assert all(
        end - start + 4 * block_size <= max_download
        for start, end in ranges
    )


def test_prepare_logical_partition_images_extracts_and_filters_nonempty() -> None:
    commands: list[str] = []

    class FakeSshManager:
        def execute_command(self, _ssh, command, timeout=None):
            commands.append(command)
            if command.startswith("find /suite"):
                return (
                    "/suite/android-cts/testcases/"
                    "MicrodroidHostTestCases.CTS/lpunpack\n",
                    "",
                    0,
                )
            if command.startswith("test -x"):
                return "", "", 0
            if command.startswith("test -f"):
                return "", "", 1
            if command.startswith("flock "):
                return (
                    "system_a.img 12582912\n"
                    "vendor_a.img 347709440\n"
                    "vendor_b.img 0\n"
                    "not-logical.img 4096\n",
                    "",
                    0,
                )
            raise AssertionError(command)

    with patch.object(
        fastboot_firmware.runtime, "ssh_manager", FakeSshManager(),
    ):
        images = asyncio.run(
            fastboot_firmware._prepare_logical_partition_images(
                object(),
                suite_dir="/suite",
                super_image_path="/suite/fw_extract/update/super.img",
            )
        )

    assert [(image.partition, image.size) for image in images] == [
        ("system_a", 12582912),
        ("vendor_a", 347709440),
    ]
    assert images[0].path == (
        "/suite/fw_extract/update/.gms-logical-images/system_a.img"
    )
    assert any("simg2img" in command and "lpunpack" in command for command in commands)


def test_full_flash_order_includes_gpt_physical_and_super() -> None:
    firmware = _firmware()
    events: list[str] = []

    class FakeSshManager:
        def execute_command(self, _ssh, command, timeout=None):
            events.append(command)
            if "current-slot" in command:
                return "", "current-slot: a", 0
            if "partition-size:super" in command:
                return "", "partition-size:super: 0x20000", 0
            if "partition-size:" in command:
                return "", "partition-size:physical: 0x40000", 0
            return "OKAY", "", 0

    preparer = SimpleNamespace(
        prepare_bootloader=lambda device: (
            events.append("PREPARE_BOOTLOADER")
            or PreparedFastbootDevice(device, "rk3572")
        ),
        unlock_bootloader=lambda _prepared: events.append("UNLOCK_BOOTLOADER"),
        enter_fastbootd=lambda _prepared: events.append("ENTER_FASTBOOTD"),
    )
    with (
        patch.object(
            fastboot_firmware,
            "_extract_update_image",
            new=AsyncMock(return_value=firmware),
        ),
        patch.object(
            fastboot_firmware,
            "_remote_image_expanded_size",
            new=AsyncMock(side_effect=[2048, 4096, 1024, 65536]),
        ),
        patch.object(
            fastboot_firmware,
            "_prepare_zero_filled_sparse_image",
            new=AsyncMock(
                return_value="/suite/fw_extract/update/super.img.gms-zero-filled"
            ),
        ),
        patch.object(
            fastboot_firmware,
            "_prepare_logical_partition_images",
            new=AsyncMock(return_value=(
                fastboot_firmware.LogicalPartitionImage(
                    "system_a", "/suite/logical/system_a.img", 4096,
                ),
                fastboot_firmware.LogicalPartitionImage(
                    "vendor_a", "/suite/logical/vendor_a.img", 8192,
                ),
            )),
        ),
        patch.object(
            fastboot_firmware,
            "_read_adb_storage_geometry",
            return_value=fastboot_firmware.StorageGeometry(0x1000000, 512),
        ),
        patch.object(
            fastboot_firmware, "_upload_remote_bytes", new=AsyncMock(),
        ),
        patch.object(
            fastboot_firmware,
            "_wait_for_android_boot",
            new=AsyncMock(return_value="D1"),
        ) as wait_boot,
        patch.object(fastboot_firmware.runtime, "ssh_manager", FakeSshManager()),
        patch.object(fastboot_firmware, "FastbootPreparer", return_value=preparer),
    ):
        result = asyncio.run(fastboot_firmware.run_usbip_fastboot_firmware(
            object(),
            suite_dir="/suite",
            remote_tool="/suite/upgrade_tool",
            remote_firmware="/suite/update.img",
            devices=["D1"],
        ))

    def index(fragment: str) -> int:
        return next(i for i, event in enumerate(events) if fragment in event)

    assert index(" flash gpt ") < index(" flash dtbo_a ")
    assert index(" flash dtbo_a ") < index(" flash vbmeta_a ")
    assert index(" flash vbmeta_a ") < index("ENTER_FASTBOOTD")
    assert index("ENTER_FASTBOOTD") < index("partition-size:super")
    assert index("partition-size:super") < index(" erase super")
    normalized_index = next(
        i for i, event in enumerate(events)
        if event.endswith("super.img.gms-zero-filled")
    )
    assert index(" erase super") < normalized_index < index(" -w")
    # -w 提前到 Fastbootd 逻辑分区写入之前：先擦后写，避免擦除与刚写入的
    # super 尾部产生竞争（RK3572 vendor_a 尾部 verity 元数据丢失根因）。
    assert index(" -w") < index(" flash system_a ")
    assert index(" flash system_a ") < index(" flash vendor_a ")
    assert not any(
        event.endswith("/suite/fw_extract/update/super.img") for event in events
    )
    assert index(" flash vendor_a ") < index(" reboot")
    assert not any("set_active" in event for event in events)
    wait_boot.assert_awaited_once()
    device_result = result["results"][0]
    assert device_result["gpt_updated"] is True
    assert device_result["userdata_wiped"] is True
    assert device_result["bootloader_partitions"] == [
        "dtbo_a", "boot_a", "vbmeta_a",
    ]
    assert device_result["fastbootd_partitions"] == ["super"]
    assert device_result["logical_partitions_rewritten"] == [
        "system_a", "vendor_a",
    ]


def test_unsupported_super_erase_falls_back_to_zero_filled_image() -> None:
    firmware = _firmware()
    commands: list[str] = []

    class FakeSshManager:
        def execute_command(self, _ssh, command, timeout=None):
            commands.append(command)
            if "current-slot" in command:
                return "", "current-slot: a", 0
            if "partition-size:super" in command:
                return "", "partition-size:super: 0x20000", 0
            if "partition-size:" in command:
                return "", "partition-size:physical: 0x40000", 0
            if " erase super" in command:
                return (
                    "",
                    "FAILED (remote: 'Erasing failed')\n"
                    "fastboot: error: Command failed",
                    1,
                )
            return "OKAY", "", 0

    preparer = SimpleNamespace(
        prepare_bootloader=lambda device: PreparedFastbootDevice(device, "rk3572"),
        unlock_bootloader=lambda _prepared: None,
        enter_fastbootd=lambda _prepared: None,
    )
    with (
        patch.object(
            fastboot_firmware,
            "_extract_update_image",
            new=AsyncMock(return_value=firmware),
        ),
        patch.object(
            fastboot_firmware,
            "_remote_image_expanded_size",
            new=AsyncMock(side_effect=[2048, 4096, 1024, 65536]),
        ),
        patch.object(
            fastboot_firmware,
            "_prepare_zero_filled_sparse_image",
            new=AsyncMock(return_value="/suite/super.img.gms-zero-filled"),
        ),
        patch.object(
            fastboot_firmware,
            "_prepare_logical_partition_images",
            new=AsyncMock(return_value=()),
        ),
        patch.object(
            fastboot_firmware,
            "_read_adb_storage_geometry",
            return_value=fastboot_firmware.StorageGeometry(0x1000000, 512),
        ),
        patch.object(fastboot_firmware, "_upload_remote_bytes", new=AsyncMock()),
        patch.object(
            fastboot_firmware,
            "_wait_for_android_boot",
            new=AsyncMock(return_value="D1"),
        ),
        patch.object(fastboot_firmware.runtime, "ssh_manager", FakeSshManager()),
        patch.object(fastboot_firmware, "FastbootPreparer", return_value=preparer),
    ):
        result = asyncio.run(fastboot_firmware.run_usbip_fastboot_firmware(
            object(),
            suite_dir="/suite",
            remote_tool="/suite/upgrade_tool",
            remote_firmware="/suite/update.img",
            devices=["D1"],
        ))

    assert any(" erase super" in command for command in commands)
    super_flashes = [command for command in commands if " flash super " in command]
    assert len(super_flashes) == 1
    assert super_flashes[0].endswith("super.img.gms-zero-filled")
    assert result["results"][0]["fastbootd_partitions"] == ["super"]


def test_super_erase_transport_failure_stops_before_sparse_flash() -> None:
    firmware = _firmware()
    commands: list[str] = []

    class FakeSshManager:
        def execute_command(self, _ssh, command, timeout=None):
            commands.append(command)
            if "current-slot" in command:
                return "", "current-slot: a", 0
            if "partition-size:super" in command:
                return "", "partition-size:super: 0x20000", 0
            if "partition-size:" in command:
                return "", "partition-size:physical: 0x40000", 0
            if " erase super" in command:
                return "", "fastboot: error: device disconnected", 1
            return "OKAY", "", 0

    preparer = SimpleNamespace(
        prepare_bootloader=lambda device: PreparedFastbootDevice(device, "rk3572"),
        unlock_bootloader=lambda _prepared: None,
        enter_fastbootd=lambda _prepared: None,
    )
    with (
        patch.object(
            fastboot_firmware,
            "_extract_update_image",
            new=AsyncMock(return_value=firmware),
        ),
        patch.object(
            fastboot_firmware,
            "_remote_image_expanded_size",
            new=AsyncMock(side_effect=[2048, 4096, 1024, 65536]),
        ),
        patch.object(
            fastboot_firmware,
            "_prepare_zero_filled_sparse_image",
            new=AsyncMock(return_value="/suite/super.img.gms-zero-filled"),
        ),
        patch.object(
            fastboot_firmware,
            "_prepare_logical_partition_images",
            new=AsyncMock(return_value=()),
        ),
        patch.object(
            fastboot_firmware,
            "_read_adb_storage_geometry",
            return_value=fastboot_firmware.StorageGeometry(0x1000000, 512),
        ),
        patch.object(fastboot_firmware, "_upload_remote_bytes", new=AsyncMock()),
        patch.object(fastboot_firmware.runtime, "ssh_manager", FakeSshManager()),
        patch.object(fastboot_firmware, "FastbootPreparer", return_value=preparer),
        pytest.raises(
            fastboot_firmware.FastbootFirmwareError,
            match=r"清空super失败.*未写入新super",
        ),
    ):
        asyncio.run(fastboot_firmware.run_usbip_fastboot_firmware(
            object(),
            suite_dir="/suite",
            remote_tool="/suite/upgrade_tool",
            remote_firmware="/suite/update.img",
            devices=["D1"],
        ))

    assert any(" erase super" in command for command in commands)
    assert not any(" flash super " in command for command in commands)


def test_missing_fastbootd_partition_aborts_without_silent_skip() -> None:
    firmware = _firmware()
    commands: list[str] = []

    class FakeSshManager:
        def execute_command(self, _ssh, command, timeout=None):
            commands.append(command)
            if "partition-size:super" in command:
                return "", "FAILED (remote: partition does not exist)", 1
            if "partition-size:" in command:
                return "", "partition-size:physical: 0x40000", 0
            return "OKAY", "", 0

    preparer = SimpleNamespace(
        prepare_bootloader=lambda device: PreparedFastbootDevice(device, "rk3572"),
        unlock_bootloader=lambda _prepared: None,
        enter_fastbootd=lambda _prepared: None,
    )
    with (
        patch.object(
            fastboot_firmware,
            "_extract_update_image",
            new=AsyncMock(return_value=firmware),
        ),
        patch.object(
            fastboot_firmware,
            "_remote_image_expanded_size",
            new=AsyncMock(side_effect=[2048, 4096, 1024, 65536]),
        ),
        patch.object(
            fastboot_firmware,
            "_prepare_zero_filled_sparse_image",
            new=AsyncMock(return_value="/suite/super.img.gms-zero-filled"),
        ),
        patch.object(
            fastboot_firmware,
            "_prepare_logical_partition_images",
            new=AsyncMock(return_value=()),
        ),
        patch.object(
            fastboot_firmware,
            "_read_adb_storage_geometry",
            return_value=fastboot_firmware.StorageGeometry(0x1000000, 512),
        ),
        patch.object(
            fastboot_firmware, "_upload_remote_bytes", new=AsyncMock(),
        ),
        patch.object(fastboot_firmware.runtime, "ssh_manager", FakeSshManager()),
        patch.object(fastboot_firmware, "FastbootPreparer", return_value=preparer),
        pytest.raises(
            fastboot_firmware.FastbootFirmwareError,
            match=r"未暴露动态分区 super.*未静默跳过",
        ),
    ):
        asyncio.run(fastboot_firmware.run_usbip_fastboot_firmware(
            object(),
            suite_dir="/suite",
            remote_tool="/suite/upgrade_tool",
            remote_firmware="/suite/update.img",
            devices=["D1"],
        ))

    assert not any(" flash super " in command for command in commands)


def test_read_adb_storage_geometry_retries_with_su_when_shell_denied() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: int):
        calls.append(argv)
        if len(calls) == 1:
            return fastboot_firmware.CommandResult(
                stderr="cat: /sys/class/block/mmcblk2/size: Permission denied",
                code=1,
            )
        return fastboot_firmware.CommandResult(
            stdout="GMS_GEOMETRY 61120512 512\n", code=0,
        )

    geometry = fastboot_firmware._read_adb_storage_geometry(runner, "D1")

    assert geometry is not None
    assert (geometry.total_sectors, geometry.logical_block_size) == (
        61120512, 512,
    )
    assert len(calls) == 2
    assert "su 0 sh -c " in calls[1][-1]


def test_read_adb_storage_geometry_retries_after_adb_root() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: int):
        calls.append(argv)
        if argv[-1] == "root":
            return fastboot_firmware.CommandResult(
                stdout="restarting adbd as root", code=0,
            )
        if argv[-1] == "wait-for-device":
            return fastboot_firmware.CommandResult(code=0)
        if len(calls) >= 5:
            return fastboot_firmware.CommandResult(
                stdout="GMS_GEOMETRY 249872384 512\n", code=0,
            )
        return fastboot_firmware.CommandResult(
            stderr="cat: /sys/class/block/sda/size: Permission denied", code=1,
        )

    geometry = fastboot_firmware._read_adb_storage_geometry(runner, "D1")

    assert geometry == fastboot_firmware.StorageGeometry(249872384, 512)
    assert ["adb", "-s", "D1", "root"] in calls
    assert ["adb", "-s", "D1", "wait-for-device"] in calls


def test_read_adb_storage_geometry_logs_when_both_attempts_fail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def runner(argv: list[str], timeout: int):
        return fastboot_firmware.CommandResult(
            stderr="cat: /sys/class/block/sda/size: Permission denied", code=1,
        )

    with caplog.at_level("WARNING", logger="features.firmware.fastboot_firmware"):
        geometry = fastboot_firmware._read_adb_storage_geometry(runner, "D1")

    assert geometry is None
    assert "Permission denied" in caplog.text
    assert "D1" in caplog.text


def test_read_uboot_storage_geometry_reports_unsupported_command() -> None:
    def runner(argv: list[str], timeout: int):
        return fastboot_firmware.CommandResult(
            stdout="FAILED (remote: 'Unsupported command')", code=1,
        )

    with pytest.raises(
        fastboot_firmware.FastbootFirmwareError,
        match=r"均未返回目标磁盘容量.*Unsupported command",
    ):
        fastboot_firmware._read_uboot_storage_geometry(runner, "D1")


def test_unsupported_uboot_capacity_round_trips_android_before_any_write() -> None:
    firmware = _firmware()
    commands: list[str] = []
    logs: list[str] = []
    prepare_count = 0
    unlock_count = 0

    class FakeSshManager:
        def execute_command(self, _ssh, command, timeout=None):
            commands.append(command)
            if "current-slot" in command:
                return "", "current-slot: a", 0
            if "partition-size:super" in command:
                return "", "partition-size:super: 0x20000", 0
            if "partition-size:" in command:
                return "", "partition-size:physical: 0x40000", 0
            return "OKAY", "", 0

    def prepare(device: str) -> PreparedFastbootDevice:
        nonlocal prepare_count
        prepare_count += 1
        return PreparedFastbootDevice(device, "rk3572")

    def unlock(_prepared: PreparedFastbootDevice) -> None:
        nonlocal unlock_count
        unlock_count += 1

    async def on_log(message: str) -> None:
        logs.append(message)

    preparer = SimpleNamespace(
        prepare_bootloader=prepare,
        unlock_bootloader=unlock,
        enter_fastbootd=lambda _prepared: None,
    )
    with (
        patch.object(
            fastboot_firmware,
            "_extract_update_image",
            new=AsyncMock(return_value=firmware),
        ),
        patch.object(
            fastboot_firmware,
            "_remote_image_expanded_size",
            new=AsyncMock(side_effect=[2048, 4096, 1024, 65536]),
        ),
        patch.object(
            fastboot_firmware,
            "_prepare_zero_filled_sparse_image",
            new=AsyncMock(return_value="/suite/super.img.gms-zero-filled"),
        ),
        patch.object(
            fastboot_firmware,
            "_prepare_logical_partition_images",
            new=AsyncMock(return_value=()),
        ),
        patch.object(
            fastboot_firmware,
            "_read_adb_storage_geometry",
            side_effect=[
                None,
                fastboot_firmware.StorageGeometry(0x1000000, 512),
            ],
        ),
        patch.object(
            fastboot_firmware,
            "_read_uboot_storage_geometry",
            side_effect=fastboot_firmware.FastbootFirmwareError(
                "Unsupported command",
            ),
        ),
        patch.object(
            fastboot_firmware, "_upload_remote_bytes", new=AsyncMock(),
        ),
        patch.object(
            fastboot_firmware,
            "_wait_for_android_boot",
            new=AsyncMock(return_value="D1"),
        ) as wait_boot,
        patch.object(fastboot_firmware.runtime, "ssh_manager", FakeSshManager()),
        patch.object(fastboot_firmware, "FastbootPreparer", return_value=preparer),
    ):
        result = asyncio.run(fastboot_firmware.run_usbip_fastboot_firmware(
            object(),
            suite_dir="/suite",
            remote_tool="/suite/upgrade_tool",
            remote_firmware="/suite/update.img",
            devices=["D1"],
            on_log=on_log,
        ))

    assert result["results"][0]["success"] is True
    assert prepare_count == 2
    assert unlock_count == 2
    wait_boot.assert_awaited()
    reboot_index = next(i for i, item in enumerate(commands) if " reboot" in item)
    gpt_index = next(i for i, item in enumerate(commands) if " flash gpt " in item)
    assert reboot_index < gpt_index
    assert any("尚未写盘" in message for message in logs)
    assert any("重新进入Bootloader" in message for message in logs)


def test_bootloader_size_probe_unsupported_still_flashes() -> None:
    firmware = _firmware()
    commands: list[str] = []
    logs: list[str] = []

    class FakeSshManager:
        def execute_command(self, _ssh, command, timeout=None):
            commands.append(command)
            if "partition-size:super" in command:
                return "", "partition-size:super: 0x20000", 0
            if "partition-size:" in command:
                return "", "FAILED (remote: 'invalid partition or device')", 1
            return "OKAY", "", 0

    async def on_log(message: str) -> None:
        logs.append(message)

    preparer = SimpleNamespace(
        prepare_bootloader=lambda device: PreparedFastbootDevice(device, "rk3572"),
        unlock_bootloader=lambda _prepared: None,
        enter_fastbootd=lambda _prepared: None,
    )
    with (
        patch.object(
            fastboot_firmware,
            "_extract_update_image",
            new=AsyncMock(return_value=firmware),
        ),
        patch.object(
            fastboot_firmware,
            "_remote_image_expanded_size",
            new=AsyncMock(side_effect=[2048, 4096, 1024, 65536]),
        ),
        patch.object(
            fastboot_firmware,
            "_prepare_zero_filled_sparse_image",
            new=AsyncMock(return_value="/suite/super.img.gms-zero-filled"),
        ),
        patch.object(
            fastboot_firmware,
            "_prepare_logical_partition_images",
            new=AsyncMock(return_value=()),
        ),
        patch.object(
            fastboot_firmware,
            "_read_adb_storage_geometry",
            return_value=fastboot_firmware.StorageGeometry(0x1000000, 512),
        ),
        patch.object(
            fastboot_firmware, "_upload_remote_bytes", new=AsyncMock(),
        ),
        patch.object(
            fastboot_firmware,
            "_wait_for_android_boot",
            new=AsyncMock(return_value="D1"),
        ),
        patch.object(fastboot_firmware.runtime, "ssh_manager", FakeSshManager()),
        patch.object(fastboot_firmware, "FastbootPreparer", return_value=preparer),
    ):
        result = asyncio.run(fastboot_firmware.run_usbip_fastboot_firmware(
            object(),
            suite_dir="/suite",
            remote_tool="/suite/upgrade_tool",
            remote_firmware="/suite/update.img",
            devices=["D1"],
            on_log=on_log,
        ))

    assert result["results"][0]["success"] is True
    assert any(" flash gpt " in command for command in commands)
    assert any(" flash dtbo_a " in command for command in commands)
    assert any(" flash vbmeta_a " in command for command in commands)
    assert any(" flash super " in command for command in commands)
    # 第一个分区探测被拒后停止继续探测，避免无意义的N次失败查询。
    assert any("partition-size:dtbo_a" in command for command in commands)
    assert not any("partition-size:boot_a" in command for command in commands)
    assert not any("partition-size:vbmeta_a" in command for command in commands)
    assert any("不返回partition-size" in message for message in logs)


def test_resolve_usbip_route_serial_uses_exact_source_and_busid() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], _timeout: int):
        calls.append(argv)
        if argv[-2:] == ["usbip", "port"]:
            return fastboot_firmware.CommandResult(
                stdout=(
                    "Port 00: <Port in Use>\n"
                    "    3-1 -> usbip://172.16.14.188:3240/1-1\n"
                    "Port 01: <Port in Use>\n"
                    "    3-2.4 -> usbip://172.16.14.66:3240/1-1\n"
                ),
            )
        if argv[-1] == "/sys/bus/usb/devices/3-2.4/serial":
            return fastboot_firmware.CommandResult(stdout="NEW-SERIAL\n")
        raise AssertionError(argv)

    serial = fastboot_firmware._resolve_usbip_route_serial(runner, {
        "device_host": "hcq@172.16.14.66",
        "busid": "1-1",
    })

    assert serial == "NEW-SERIAL"
    assert calls[-1][-1] == "/sys/bus/usb/devices/3-2.4/serial"


def test_wait_for_android_boot_accepts_serial_from_owned_usbip_route() -> None:
    def runner(argv: list[str], _timeout: int):
        if argv[:3] == ["adb", "-s", "OLD"]:
            return fastboot_firmware.CommandResult(stderr="device not found", code=1)
        if argv[:3] == ["adb", "-s", "NEW"] and argv[-1] == "get-state":
            return fastboot_firmware.CommandResult(stdout="device")
        if argv[:3] == ["adb", "-s", "NEW"]:
            return fastboot_firmware.CommandResult(stdout="1\n")
        raise AssertionError(argv)

    with patch.object(
        fastboot_firmware,
        "_resolve_usbip_route_serial",
        return_value="NEW",
    ):
        booted = asyncio.run(fastboot_firmware._wait_for_android_boot(
            runner,
            "OLD",
            usbip_route={"source_host": "172.16.14.66", "busid": "1-1"},
            timeout=1,
        ))

    assert booted == "NEW"


def test_logical_flash_segments_large_image_without_unsupported_fetch() -> None:
    firmware = _firmware()
    commands: list[str] = []
    logs: list[str] = []

    async def on_log(message: str) -> None:
        logs.append(message)

    class FakeSshManager:
        def execute_command(self, _ssh, command, timeout=None):
            commands.append(command)
            if "current-slot" in command:
                return "", "current-slot: a", 0
            if "max-download-size" in command:
                return "", "max-download-size: 262144", 0
            if "partition-size:super" in command:
                return "", "partition-size:super: 0x20000", 0
            if "partition-size:vendor_a" in command:
                return "", "partition-size:vendor_a: 0x200000", 0
            if "partition-size:" in command:
                return "", "partition-size:physical: 0x40000", 0
            if "--segment" in command:
                return "245772\n", "", 0
            return "OKAY", "", 0

    preparer = SimpleNamespace(
        prepare_bootloader=lambda device: PreparedFastbootDevice(device, "rk3572"),
        unlock_bootloader=lambda _prepared: None,
        enter_fastbootd=lambda _prepared: None,
    )
    vendor_image = fastboot_firmware.LogicalPartitionImage(
        "vendor_a", "/suite/logical/vendor_a.img", 0x100000,
    )
    system_image = fastboot_firmware.LogicalPartitionImage(
        "system_a", "/suite/logical/system_a.img", 4096,
    )
    with (
        patch.object(
            fastboot_firmware,
            "_extract_update_image",
            new=AsyncMock(return_value=firmware),
        ),
        patch.object(
            fastboot_firmware,
            "_remote_image_expanded_size",
            new=AsyncMock(side_effect=[2048, 4096, 1024, 65536]),
        ),
        patch.object(
            fastboot_firmware,
            "_prepare_zero_filled_sparse_image",
            new=AsyncMock(return_value="/suite/super.img.gms-zero-filled"),
        ),
        patch.object(
            fastboot_firmware,
            "_prepare_logical_partition_images",
            new=AsyncMock(return_value=(system_image, vendor_image)),
        ),
        patch.object(
            fastboot_firmware,
            "_read_adb_storage_geometry",
            return_value=fastboot_firmware.StorageGeometry(0x1000000, 512),
        ),
        patch.object(
            fastboot_firmware, "_upload_remote_bytes", new=AsyncMock(),
        ),
        patch.object(
            fastboot_firmware,
            "_wait_for_android_boot",
            new=AsyncMock(return_value="D1"),
        ),
        patch.object(fastboot_firmware.runtime, "ssh_manager", FakeSshManager()),
        patch.object(fastboot_firmware, "FastbootPreparer", return_value=preparer),
    ):
        result = asyncio.run(fastboot_firmware.run_usbip_fastboot_firmware(
            object(),
            suite_dir="/suite",
            remote_tool="/suite/upgrade_tool",
            remote_firmware="/suite/update.img",
            devices=["D1"],
            on_log=on_log,
        ))

    assert result["results"][0]["success"] is True
    vendor_segments = [
        command for command in commands
        if " flash vendor_a " in command
    ]
    assert len(vendor_segments) >= 2
    assert all(".gms-logical-segments/" in item for item in vendor_segments)
    system_segments = [
        command for command in commands
        if " flash system_a " in command
    ]
    assert len(system_segments) == 1
    assert ".gms-logical-segments/" in system_segments[0]
    assert any("RAW-only sparse分段写入" in message for message in logs)
    assert any("vendor_a 写入命令全部成功" in message for message in logs)
    assert not any(" fetch " in command for command in commands)
    assert not any("cmp -s" in command for command in commands)


def test_logical_segment_flash_failure_stops_before_reboot() -> None:
    firmware = _firmware()
    commands: list[str] = []

    class FakeSshManager:
        def execute_command(self, _ssh, command, timeout=None):
            commands.append(command)
            if "current-slot" in command:
                return "", "current-slot: a", 0
            if "max-download-size" in command:
                return "", "max-download-size: 262144", 0
            if "partition-size:super" in command:
                return "", "partition-size:super: 0x20000", 0
            if "partition-size:vendor_a" in command:
                return "", "partition-size:vendor_a: 0x200000", 0
            if "partition-size:" in command:
                return "", "partition-size:physical: 0x40000", 0
            if "--segment" in command:
                return "245772\n", "", 0
            if " flash vendor_a " in command:
                return "", "FAILED (remote: write failed)", 1
            return "OKAY", "", 0

    preparer = SimpleNamespace(
        prepare_bootloader=lambda device: PreparedFastbootDevice(device, "rk3572"),
        unlock_bootloader=lambda _prepared: None,
        enter_fastbootd=lambda _prepared: None,
    )
    vendor_image = fastboot_firmware.LogicalPartitionImage(
        "vendor_a", "/suite/logical/vendor_a.img", 0x100000,
    )
    with (
        patch.object(
            fastboot_firmware,
            "_extract_update_image",
            new=AsyncMock(return_value=firmware),
        ),
        patch.object(
            fastboot_firmware,
            "_remote_image_expanded_size",
            new=AsyncMock(side_effect=[2048, 4096, 1024, 65536]),
        ),
        patch.object(
            fastboot_firmware,
            "_prepare_zero_filled_sparse_image",
            new=AsyncMock(return_value="/suite/super.img.gms-zero-filled"),
        ),
        patch.object(
            fastboot_firmware,
            "_prepare_logical_partition_images",
            new=AsyncMock(return_value=(vendor_image,)),
        ),
        patch.object(
            fastboot_firmware,
            "_read_adb_storage_geometry",
            return_value=fastboot_firmware.StorageGeometry(0x1000000, 512),
        ),
        patch.object(
            fastboot_firmware, "_upload_remote_bytes", new=AsyncMock(),
        ),
        patch.object(fastboot_firmware.runtime, "ssh_manager", FakeSshManager()),
        patch.object(fastboot_firmware, "FastbootPreparer", return_value=preparer),
        pytest.raises(
            fastboot_firmware.FastbootFirmwareError,
            match="分段写入 vendor_a",
        ),
    ):
        asyncio.run(fastboot_firmware.run_usbip_fastboot_firmware(
            object(),
            suite_dir="/suite",
            remote_tool="/suite/upgrade_tool",
            remote_firmware="/suite/update.img",
            devices=["D1"],
        ))

    assert not any(" reboot" in command for command in commands)
