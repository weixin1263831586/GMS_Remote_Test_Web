"""Complete Rockchip ``update.img`` burning over USB/IP Fastboot.

RockUSB Loader/DB resets are not reliable through usbipd-win on RK3572.  The
reliable transport is Android Fastboot, but a complete firmware update needs
two Fastboot implementations:

* U-Boot Fastboot writes the GPT and every physical partition, including the
  ``dtbo``/``vbmeta`` partitions hidden by this device's Fastbootd SELinux
  policy.
* Fastbootd writes ``super`` and any individual dynamic partitions.

The GPT is generated from the firmware's ``parameter.txt`` and finalized for
the target disk geometry before the first write.  This is required when a new
firmware adds or moves partitions (the captured RK3572 failure added
``pvmfw_a``/``pvmfw_b``); copying only partition payloads cannot repair that
layout change.

Local Ubuntu USB firmware burning remains in ``firmware_api`` and continues to
use ``upgrade_tool uf``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import logging
import os
import re
import shlex
import struct
import time
import zipfile
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from worker_agent.fastboot_workflow import CommandResult, FastbootPreparer

from . import runtime
from .partition_burn import (
    ANDROID_SPARSE_MAGIC as ANDROID_SPARSE_MAGIC,  # re-exported for callers
)
from .partition_burn import (
    BYTES_PER_SECOND_FLOOR,
    SECTOR_BYTES,
    PartitionBurnError,
    PartitionEntry,
    SfiEntry,
    _command_timeout,
    _extract_dir,
    _last_percent,
    _stream_tool_command,
    parse_android_sparse_expanded_size,
    parse_gpt_entries,
    parse_parameter_partitions,
    parse_sfi_entries,
    strip_ansi_codes,
    tables_match,
)


logger = logging.getLogger(__name__)


# Dynamic partitions are written by Fastbootd.  Everything else declared as
# an image in parameter.txt is a physical GPT partition and is written by
# U-Boot Fastboot, whose partition lookup is not constrained by Android SELinux.
_FASTBOOTD_PARTITION_RE = re.compile(
    r"^(?:"
    r"super|"
    r"system|system_ext|vendor|product|odm|"
    r"system_dlkm|vendor_dlkm|odm_dlkm"
    r")(?:_[ab])?$",
    re.IGNORECASE,
)
_SAFE_PARTITION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,71}$")
_PARTITION_SIZE_RE = re.compile(
    r"partition-size(?::[^:\s]+)?\s*:\s*(0x[0-9a-fA-F]+|[0-9]+)",
    re.IGNORECASE,
)
_MAX_DOWNLOAD_RE = re.compile(
    r"max-download-size\s*:\s*(0x[0-9a-fA-F]+|[0-9]+)",
    re.IGNORECASE,
)
# Logical partitions are erased/written in 4KiB blocks by fastbootd.
_LOGICAL_BLOCK_SIZE = 4096
# Headroom that absorbs the sparse chunk headers plus fastboot framing so a
# segment's packed size always stays below max-download-size.
_SEGMENT_MARGIN_BYTES = 4 * _LOGICAL_BLOCK_SIZE
_GEOMETRY_RE = re.compile(r"GMS_GEOMETRY\s+(\d+)\s+(\d+)")
_UBOOT_CAPACITY_RE = re.compile(
    r"Capacity:.*?\((\d+)\s*x\s*(\d+)\)", re.IGNORECASE,
)

_GPT_HEADER_OFFSET = SECTOR_BYTES
_GPT_HEADER_MIN_SIZE = 92
_GPT_ENTRY_START_OFFSET = 32
_GPT_ENTRY_END_OFFSET = 40
_GPT_ENTRY_NAME_OFFSET = 56
_GPT_ENTRY_NAME_BYTES = 72
_GPT_TABLE_MIN_SECTORS = 34
_GPT_SPECIAL_TARGET = "gpt"
_CACHE_IDENTITY_FILE = ".gms-firmware-identity"
_GPT_TEMPLATE_FILE = ".gms-gpt-template.img"
_SPARSE_NORMALIZER_FILE = ".gms-sparse-image.py"
_SPARSE_ZERO_FILLED_SUFFIX = ".gms-zero-filled"
_SPARSE_TRUNCATED_SUFFIX = ".gms-truncated"
_LOGICAL_IMAGES_DIR = ".gms-logical-images"
_LOGICAL_SEGMENT_DIR = ".gms-logical-segments"
_MANAGED_PLATFORM_TOOLS_ARCHIVE = "platform-tools-gms-linux.zip"
_MANAGED_FASTBOOT_DIR = ".gms-platform-tools"
_MANAGED_FASTBOOT_ARCHIVE_PATH = "platform-tools/fastboot"
_MANAGED_LIBCXX_ARCHIVE_PATH = "platform-tools/lib64/libc++.so"
_USBIP_PORT_ROUTE_RE = re.compile(
    r"\b(?P<local_busid>\d+-\d+(?:\.\d+)*)\s*->\s*"
    r"usbip://(?P<host>\[[0-9A-Fa-f:]+\]|[^/:\s]+)(?::\d+)?/"
    r"(?P<remote_busid>\d+-\d+(?:\.\d+)*)\b",
    re.IGNORECASE,
)
_USB_SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
ANDROID_BOOT_TIMEOUT_SECONDS = 600
ANDROID_POLL_INTERVAL_SECONDS = 3


class FastbootFirmwareError(RuntimeError):
    """Safe, user-facing failure from the USB/IP Fastboot backend."""

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class FastbootWriteStep:
    partition: str
    image: str
    entry_type: str
    packed_size: int


@dataclass(frozen=True)
class FastbootWritePlan:
    bootloader: tuple[FastbootWriteStep, ...]
    fastbootd: tuple[FastbootWriteStep, ...]
    skipped: tuple[str, ...]

    @property
    def steps(self) -> tuple[FastbootWriteStep, ...]:
        return self.bootloader + self.fastbootd


@dataclass(frozen=True)
class ExtractedFirmware:
    extract_dir: str
    plan: FastbootWritePlan
    partitions: tuple[PartitionEntry, ...]
    gpt_template: bytes


@dataclass(frozen=True)
class StorageGeometry:
    total_sectors: int
    logical_block_size: int = SECTOR_BYTES


@dataclass(frozen=True)
class LogicalPartitionImage:
    partition: str
    path: str
    size: int


def _safe_entry_file(name: str) -> bool:
    path = PurePosixPath(str(name or ""))
    return bool(name) and path.name == name and name not in {".", ".."}


def extracted_file_size_mismatches(
    entries: list[SfiEntry],
    actual_sizes: dict[str, int],
) -> list[str]:
    """Return missing/truncated EXF outputs that make flashing unsafe.

    ``upgrade_tool EXF`` normalizes line endings in text metadata.  In this
    RK3572 package, SFI reports parameter.txt as 1017 bytes while EXF emits a
    valid 1005-byte LF file.  Partition payloads remain binary and must match
    SFI exactly; metadata only needs to exist and is validated by its parser.
    """
    mismatches: set[str] = set()
    for entry in entries:
        actual_size = actual_sizes.get(entry.file)
        if actual_size is None or actual_size <= 0:
            mismatches.add(entry.file)
            continue
        entry_type = " ".join((entry.entry_type or "").lower().split())
        if (
            entry_type in {"image", "sparse image"}
            and actual_size != int(entry.size or 0)
        ):
            mismatches.add(entry.file)
    return sorted(mismatches)


def build_fastboot_write_plan(
    entries: list[SfiEntry],
    partitions: list[PartitionEntry],
) -> FastbootWritePlan:
    """Build a no-silent-skip two-stage Fastboot plan.

    Metadata (package-file, Loader, parameter) is deliberately skipped because
    GPT is handled as a separately generated image.  Every SFI image entry
    must map to parameter.txt and must be assigned to U-Boot Fastboot or
    Fastbootd; an unknown image entry aborts instead of leaving an old-version
    partition behind.
    """
    by_partition = {entry.name.lower(): entry for entry in partitions}
    if len(by_partition) != len(partitions):
        raise FastbootFirmwareError("parameter.txt包含重复分区名")
    bootloader: list[FastbootWriteStep] = []
    fastbootd: list[FastbootWriteStep] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        partition = str(entry.partition or "").strip()
        entry_type = " ".join(str(entry.entry_type or "").lower().split())
        if not partition or entry_type not in {"image", "sparse image"}:
            skipped.append(
                f"{entry.file}(非Fastboot分区条目: {entry.entry_type or 'metadata'})"
            )
            continue
        if not _safe_entry_file(entry.file):
            raise FastbootFirmwareError(
                f"固件条目包含不安全的文件路径: {entry.file!r}"
            )
        if not _SAFE_PARTITION_RE.fullmatch(partition):
            raise FastbootFirmwareError(f"固件分区名不安全: {partition!r}")
        if partition.lower() in {_GPT_SPECIAL_TARGET, "mbr"}:
            raise FastbootFirmwareError(
                f"固件镜像条目不能占用Fastboot保留目标: {partition}"
            )
        if partition.lower() in seen:
            raise FastbootFirmwareError(f"固件重复声明Fastboot分区: {partition}")
        layout = by_partition.get(partition.lower())
        if layout is None:
            raise FastbootFirmwareError(
                f"固件声明的分区 {partition} 不在 parameter.txt 中；"
                "已在写入前拒绝不完整固件"
            )
        if int(entry.size or 0) <= 0:
            raise FastbootFirmwareError(
                f"分区 {partition} 的镜像 {entry.file} 大小无效"
            )
        if entry_type == "image" and not layout.grow:
            capacity = layout.size_sec * SECTOR_BYTES
            if int(entry.size) > capacity:
                raise FastbootFirmwareError(
                    f"分区 {partition} 镜像 {entry.file} 为 {entry.size} 字节，"
                    f"超过 parameter.txt 容量 {capacity} 字节"
                )
        seen.add(partition.lower())
        step = FastbootWriteStep(
            partition=partition,
            image=entry.file,
            entry_type=entry_type,
            packed_size=int(entry.size),
        )
        if _FASTBOOTD_PARTITION_RE.fullmatch(partition):
            fastbootd.append(step)
        else:
            bootloader.append(step)

    if not bootloader and not fastbootd:
        raise FastbootFirmwareError(
            "update.img 中没有可由Fastboot烧写的分区"
        )
    if not any(step.partition.lower() == "super" for step in fastbootd):
        raise FastbootFirmwareError(
            "USB/IP Fastboot固件模式要求update.img包含super分区；"
            "缺少super时不能保证系统分区完整"
        )
    if not any(step.partition.lower().startswith("vbmeta") for step in bootloader):
        raise FastbootFirmwareError(
            "update.img 中没有vbmeta分区；写入super后无法保证AVB一致性"
        )

    # Keep package order, except AVB metadata is the final bootloader write.
    # A GPT migration may move boot partitions, so all physical images must be
    # in place before rebooting the freshly-written layout into Fastbootd.
    def bootloader_priority(step: FastbootWriteStep) -> int:
        name = step.partition.lower()
        return 1 if name.startswith("vbmeta") else 0

    bootloader.sort(key=bootloader_priority)
    return FastbootWritePlan(
        bootloader=tuple(bootloader),
        fastbootd=tuple(fastbootd),
        skipped=tuple(skipped),
    )


def parse_fastboot_partition_size(output: str) -> int | None:
    match = _PARTITION_SIZE_RE.search(strip_ansi_codes(output or ""))
    if not match:
        return None
    with contextlib.suppress(ValueError):
        return int(match.group(1), 0)
    return None


def _partition_size_probe_unsupported(output: str) -> bool:
    """Detect Fastboot builds whose getvar has no partition-size handler.

    Rockchip U-Boot Fastboot builds without the handler answer every
    ``getvar partition-size:<name>`` with the same transport-level rejection
    (even for partitions that exist and flash fine by name); Fastbootd uses a
    distinct "partition does not exist" failure for real misses.
    """
    cleaned = strip_ansi_codes(output or "").lower()
    return (
        "invalid partition or device" in cleaned
        or "unsupported command" in cleaned
    )


def _super_erase_unsupported(output: str) -> bool:
    """Return True only for a Fastbootd-side rejection of ``erase super``.

    Some Android Fastbootd implementations expose and flash the physical
    ``super`` partition but reject erasing it.  Transport failures must remain
    fatal; the safe fallback is allowed only when the device itself returned a
    recognized remote-command rejection.
    """
    cleaned = strip_ansi_codes(output or "").lower()
    return "failed (remote:" in cleaned and any(
        marker in cleaned
        for marker in (
            "erasing failed",
            "erase is not allowed",
            "erase not allowed",
            "erase not supported",
            "unsupported command",
        )
    )


def _fastboot_command_failed(output: str) -> bool:
    """Detect device/host failures even when the client exits with status 0.

    Rockchip's U-Boot Fastboot returns a successful process status for some
    rejected ``getvar``/``flash`` requests. The textual protocol result is
    therefore part of the command status, not diagnostic decoration.
    """
    cleaned = strip_ansi_codes(output or "").lower()
    return any(
        marker in cleaned
        for marker in (
            "failed (remote:",
            "fastboot: error:",
            "command failed",
        )
    )


_CURRENT_SLOT_RE = re.compile(r"current-slot\s*:\s*([ab])", re.IGNORECASE)


def parse_fastboot_current_slot(output: str) -> str | None:
    """Return the device's active slot ('a'/'b') from getvar output."""
    match = _CURRENT_SLOT_RE.search(strip_ansi_codes(output or ""))
    return match.group(1).lower() if match else None


def parse_fastboot_max_download(output: str) -> int | None:
    """Parse ``getvar:max-download-size`` into a byte count."""
    match = _MAX_DOWNLOAD_RE.search(strip_ansi_codes(output or ""))
    if not match:
        return None
    text = match.group(1)
    try:
        return int(text, 16 if text.lower().startswith("0x") else 10)
    except ValueError:
        return None


def plan_segment_ranges(
    total_size: int,
    max_download: int,
    *,
    block_size: int = _LOGICAL_BLOCK_SIZE,
    margin: int = _SEGMENT_MARGIN_BYTES,
) -> tuple[tuple[int, int], ...]:
    """Split a logical partition image into single-download segment ranges.

    A segment's packed sparse size is its RAW payload plus two DONT_CARE chunk
    headers and one sparse header; the margin keeps that total safely below
    ``max_download`` even when the payload itself is aligned to block size.
    """
    if total_size <= 0 or max_download <= 0:
        return ((0, total_size),)
    budget = max_download - margin
    if budget <= 0:
        raise FastbootFirmwareError(
            f"max-download-size {max_download} 过小，无法规划分段写入"
        )
    aligned_budget = (budget // block_size) * block_size
    if aligned_budget <= 0:
        raise FastbootFirmwareError(
            f"max-download-size {max_download} 不足一个逻辑块 {block_size}"
        )
    if total_size <= aligned_budget:
        return ((0, total_size),)
    ranges: list[tuple[int, int]] = []
    offset = 0
    while offset < total_size:
        end = min(offset + aligned_budget, total_size)
        ranges.append((offset, end))
        offset = end
    return tuple(ranges)


def parse_storage_geometry(output: str) -> StorageGeometry | None:
    """Parse ADB sysfs output or U-Boot storage-capacity output."""
    cleaned = strip_ansi_codes(output or "")
    marker = _GEOMETRY_RE.search(cleaned)
    if marker:
        sectors, block_size = (int(value) for value in marker.groups())
        if sectors > 0 and block_size > 0:
            # Linux sysfs ``size`` is always counted in 512-byte sectors,
            # independently of queue/logical_block_size.
            return StorageGeometry(sectors, block_size)

    candidates: list[StorageGeometry] = []
    for blocks, block_size in _UBOOT_CAPACITY_RE.findall(cleaned):
        block_count = int(blocks)
        logical_block_size = int(block_size)
        total_bytes = block_count * logical_block_size
        if (
            block_count > 0
            and logical_block_size > 0
            and total_bytes % SECTOR_BYTES == 0
        ):
            # Normalize U-Boot's native block count to the same fixed
            # 512-byte unit returned by Linux sysfs.
            candidates.append(StorageGeometry(
                total_bytes // SECTOR_BYTES,
                logical_block_size,
            ))
    return max(
        candidates,
        key=lambda item: item.total_sectors,
        default=None,
    )


def _logical_block_ratio(geometry: StorageGeometry) -> int:
    block_size = geometry.logical_block_size
    if (
        block_size < SECTOR_BYTES
        or block_size % SECTOR_BYTES != 0
        or block_size & (block_size - 1)
    ):
        raise FastbootFirmwareError(
            f"目标存储逻辑块大小无效: {block_size} 字节"
        )
    ratio = block_size // SECTOR_BYTES
    if geometry.total_sectors % ratio:
        raise FastbootFirmwareError(
            "目标存储容量不能按逻辑块完整换算: "
            f"{geometry.total_sectors} 个512字节扇区 / {block_size} 字节"
        )
    return ratio


def _validate_geometry(geometry: StorageGeometry) -> None:
    ratio = _logical_block_ratio(geometry)
    if geometry.total_sectors // ratio < _GPT_TABLE_MIN_SECTORS * 2:
        raise FastbootFirmwareError("目标存储容量太小，无法容纳GPT")


def _target_partition_lbas(
    partition: PartitionEntry,
    ratio: int,
) -> tuple[int, int | None]:
    if partition.offset_sec % ratio:
        raise FastbootFirmwareError(
            f"parameter.txt分区 {partition.name} 起始位置未按"
            f"{ratio * SECTOR_BYTES}字节逻辑块对齐"
        )
    first_lba = partition.offset_sec // ratio
    if partition.grow:
        return first_lba, None
    if partition.size_sec <= 0 or partition.size_sec % ratio:
        raise FastbootFirmwareError(
            f"parameter.txt分区 {partition.name} 大小未按"
            f"{ratio * SECTOR_BYTES}字节逻辑块对齐"
        )
    return first_lba, first_lba + partition.size_sec // ratio - 1


def _parse_target_gpt_entries(data: bytes, block_size: int) -> list:
    """Parse entries from a finalized GPT using its native logical block."""
    header = data[block_size:2 * block_size]
    if header[:8] != b"EFI PART":
        raise FastbootFirmwareError("最终GPT缺少主GPT头")
    entry_lba = struct.unpack_from("<Q", header, 72)[0]
    entry_count = struct.unpack_from("<I", header, 80)[0]
    entry_size = struct.unpack_from("<I", header, 84)[0]
    entries = []
    table_offset = entry_lba * block_size
    for index in range(entry_count):
        offset = table_offset + index * entry_size
        raw = data[offset:offset + entry_size]
        if len(raw) < entry_size or not any(raw[:16]):
            continue
        name = raw[
            _GPT_ENTRY_NAME_OFFSET:
            _GPT_ENTRY_NAME_OFFSET + _GPT_ENTRY_NAME_BYTES
        ].decode("utf-16-le", errors="ignore").rstrip("\x00")
        entries.append((
            name,
            struct.unpack_from("<Q", raw, _GPT_ENTRY_START_OFFSET)[0],
            struct.unpack_from("<Q", raw, _GPT_ENTRY_END_OFFSET)[0],
        ))
    return entries


def finalize_gpt_image(
    template: bytes,
    partitions: list[PartitionEntry] | tuple[PartitionEntry, ...],
    geometry: StorageGeometry,
) -> bytes:
    """Finalize an ``upgrade_tool GPT`` template for one target disk.

    Rockchip's template contains the primary GPT with a sentinel 32-bit disk
    end.  U-Boot Fastboot validates the header against the real block device,
    so alternate/last-usable LBAs, the grow partition, PMBR and CRC fields must
    be replaced before ``fastboot flash gpt``.
    """
    _validate_geometry(geometry)
    if len(template) < _GPT_TABLE_MIN_SECTORS * SECTOR_BYTES:
        raise FastbootFirmwareError("upgrade_tool生成的GPT模板长度无效")

    template_data = bytearray(template)
    template_header = memoryview(template_data)[
        _GPT_HEADER_OFFSET:_GPT_HEADER_OFFSET + SECTOR_BYTES
    ]
    if bytes(template_header[:8]) != b"EFI PART":
        raise FastbootFirmwareError("upgrade_tool生成的文件缺少主GPT头")
    header_size = struct.unpack_from("<I", template_header, 12)[0]
    current_lba = struct.unpack_from("<Q", template_header, 24)[0]
    entry_lba = struct.unpack_from("<Q", template_header, 72)[0]
    entry_count = struct.unpack_from("<I", template_header, 80)[0]
    entry_size = struct.unpack_from("<I", template_header, 84)[0]
    if not (
        _GPT_HEADER_MIN_SIZE <= header_size <= SECTOR_BYTES
        and current_lba == 1
        and entry_lba >= 2
        and entry_count > 0
        and entry_size >= 128
        and entry_size % 8 == 0
    ):
        raise FastbootFirmwareError("upgrade_tool生成的GPT头字段无效")

    table_offset = entry_lba * SECTOR_BYTES
    table_size = entry_count * entry_size
    table_end = table_offset + table_size
    if table_end > len(template_data):
        raise FastbootFirmwareError("upgrade_tool生成的GPT分区表不完整")

    try:
        parsed = parse_gpt_entries(bytes(template_data[_GPT_HEADER_OFFSET:]))
    except PartitionBurnError as exc:
        raise FastbootFirmwareError(str(exc)) from exc
    if not tables_match(parsed, list(partitions)):
        raise FastbootFirmwareError(
            "upgrade_tool生成的GPT与parameter.txt不一致，已拒绝写盘"
        )

    ratio = _logical_block_ratio(geometry)
    block_size = geometry.logical_block_size
    total_logical_blocks = geometry.total_sectors // ratio
    table_blocks = (table_size + block_size - 1) // block_size
    target_entry_lba = 2
    first_usable_lba = target_entry_lba + table_blocks
    backup_header_lba = total_logical_blocks - 1
    last_usable_lba = backup_header_lba - table_blocks - 1
    if last_usable_lba < first_usable_lba:
        raise FastbootFirmwareError("目标存储没有可用GPT数据区域")

    grow_partitions = [partition for partition in partitions if partition.grow]
    if len(grow_partitions) != 1:
        raise FastbootFirmwareError("parameter.txt必须且只能包含一个grow分区")
    by_name = {partition.name: partition for partition in partitions}
    if len(by_name) != len(partitions):
        raise FastbootFirmwareError("parameter.txt包含重复分区名")
    ordered = sorted(partitions, key=lambda partition: partition.offset_sec)
    for index, partition in enumerate(ordered):
        first_lba, last_lba = _target_partition_lbas(partition, ratio)
        if first_lba < first_usable_lba:
            raise FastbootFirmwareError(
                f"分区 {partition.name} 位于GPT保留区域内"
            )
        if partition.grow and index != len(ordered) - 1:
            raise FastbootFirmwareError("grow分区不是parameter.txt中的最后一个分区")
        if index + 1 < len(ordered) and not partition.grow:
            next_first_lba, _ = _target_partition_lbas(ordered[index + 1], ratio)
            if last_lba is None or last_lba >= next_first_lba:
                raise FastbootFirmwareError(
                    f"parameter.txt分区 {partition.name} 与后续分区重叠"
                )

    # ``upgrade_tool GPT`` always emits a 512-byte-sector template.  Relocate
    # its header/table into the target's native logical-block layout before
    # converting each parameter.txt address from 512-byte units.
    data = bytearray((target_entry_lba + table_blocks) * block_size)
    header_offset = block_size
    table_offset = target_entry_lba * block_size
    table_end = table_offset + table_size
    data[header_offset:header_offset + header_size] = bytes(
        template_header[:header_size]
    )
    data[table_offset:table_end] = template_data[
        entry_lba * SECTOR_BYTES:
        entry_lba * SECTOR_BYTES + table_size
    ]
    header = memoryview(data)[header_offset:header_offset + block_size]
    struct.pack_into("<Q", header, 24, 1)
    struct.pack_into("<Q", header, 40, first_usable_lba)
    struct.pack_into("<Q", header, 72, target_entry_lba)

    found_names: set[str] = set()
    for index in range(entry_count):
        entry_offset = table_offset + index * entry_size
        raw = memoryview(data)[entry_offset:entry_offset + entry_size]
        if not any(raw[:16]):
            continue
        name = bytes(raw[
            _GPT_ENTRY_NAME_OFFSET:
            _GPT_ENTRY_NAME_OFFSET + _GPT_ENTRY_NAME_BYTES
        ]).decode("utf-16-le", errors="ignore").rstrip("\x00")
        partition = by_name.get(name)
        if partition is None:
            raise FastbootFirmwareError(
                f"GPT模板含parameter.txt之外的分区: {name or '<unnamed>'}"
            )
        found_names.add(name)
        template_first_lba = struct.unpack_from(
            "<Q", raw, _GPT_ENTRY_START_OFFSET,
        )[0]
        if template_first_lba != partition.offset_sec:
            raise FastbootFirmwareError(f"GPT分区 {name} 起始位置不匹配")
        first_lba, expected_end = _target_partition_lbas(partition, ratio)
        struct.pack_into("<Q", raw, _GPT_ENTRY_START_OFFSET, first_lba)
        if partition.grow:
            if first_lba > last_usable_lba:
                raise FastbootFirmwareError(
                    f"目标存储容量不足以容纳grow分区 {name}"
                )
            struct.pack_into("<Q", raw, _GPT_ENTRY_END_OFFSET, last_usable_lba)
        else:
            if expected_end is None or expected_end > last_usable_lba:
                raise FastbootFirmwareError(
                    f"目标存储容量不足以容纳分区 {name}"
                )
            struct.pack_into("<Q", raw, _GPT_ENTRY_END_OFFSET, expected_end)
    if found_names != set(by_name):
        missing = ", ".join(sorted(set(by_name) - found_names))
        raise FastbootFirmwareError(f"GPT模板缺少parameter.txt分区: {missing}")

    # Protective MBR.  RK's GPT template leaves LBA0 empty, while U-Boot's GPT
    # writer expects a complete primary image and creates the backup copy.
    data[:SECTOR_BYTES] = b"\0" * SECTOR_BYTES
    data[446:462] = struct.pack(
        "<B3sB3sII",
        0,
        b"\x00\x02\x00",
        0xEE,
        b"\xff\xff\xff",
        1,
        min(total_logical_blocks - 1, 0xFFFFFFFF),
    )
    data[510:512] = b"\x55\xaa"

    entries_crc = zlib.crc32(data[table_offset:table_end]) & 0xFFFFFFFF
    struct.pack_into("<Q", header, 32, backup_header_lba)
    struct.pack_into("<Q", header, 48, last_usable_lba)
    struct.pack_into("<I", header, 88, entries_crc)
    struct.pack_into("<I", header, 16, 0)
    header_crc = zlib.crc32(bytes(header[:header_size])) & 0xFFFFFFFF
    struct.pack_into("<I", header, 16, header_crc)

    finalized = bytes(data)
    final_entries = {
        name: (first_lba, last_lba)
        for name, first_lba, last_lba in _parse_target_gpt_entries(
            finalized, block_size,
        )
    }
    for partition in partitions:
        first_lba, last_lba = _target_partition_lbas(partition, ratio)
        actual = final_entries.get(partition.name)
        if actual is None or actual[0] != first_lba:
            raise FastbootFirmwareError("最终GPT自校验与parameter.txt不一致")
        if not partition.grow and actual[1] != last_lba:
            raise FastbootFirmwareError("最终GPT自校验与parameter.txt不一致")
    return finalized


def _build_backup_gpt_image(
    primary: bytes,
    geometry: StorageGeometry,
) -> tuple[bytes, int]:
    """Build the backup-table/header tail matching a finalized primary GPT."""
    _validate_geometry(geometry)
    block_size = geometry.logical_block_size
    total_blocks = geometry.total_sectors // (block_size // SECTOR_BYTES)
    if len(primary) < 3 * block_size:
        raise FastbootFirmwareError("最终GPT主表长度无效")
    header = primary[block_size:2 * block_size]
    if header[:8] != b"EFI PART":
        raise FastbootFirmwareError("最终GPT缺少主GPT头")
    header_size = struct.unpack_from("<I", header, 12)[0]
    entry_lba = struct.unpack_from("<Q", header, 72)[0]
    entry_count = struct.unpack_from("<I", header, 80)[0]
    entry_size = struct.unpack_from("<I", header, 84)[0]
    table_size = entry_count * entry_size
    table_blocks = (table_size + block_size - 1) // block_size
    table_offset = entry_lba * block_size
    table_end = table_offset + table_size
    if (
        not (_GPT_HEADER_MIN_SIZE <= header_size <= block_size)
        or entry_lba != 2
        or table_blocks <= 0
        or table_end > len(primary)
    ):
        raise FastbootFirmwareError("最终GPT主表字段无效")

    backup_header_lba = total_blocks - 1
    backup_table_lba = backup_header_lba - table_blocks
    backup = bytearray((table_blocks + 1) * block_size)
    backup[:table_size] = primary[table_offset:table_end]
    backup_header_offset = table_blocks * block_size
    backup[
        backup_header_offset:backup_header_offset + block_size
    ] = header
    backup_header = memoryview(backup)[
        backup_header_offset:backup_header_offset + block_size
    ]
    struct.pack_into("<Q", backup_header, 24, backup_header_lba)
    struct.pack_into("<Q", backup_header, 32, 1)
    struct.pack_into("<Q", backup_header, 72, backup_table_lba)
    struct.pack_into("<I", backup_header, 16, 0)
    struct.pack_into(
        "<I",
        backup_header,
        16,
        zlib.crc32(bytes(backup_header[:header_size])) & 0xFFFFFFFF,
    )
    return bytes(backup), backup_table_lba


def _remote_runner(
    ssh,
    argv: list[str],
    timeout: int,
    *,
    fastboot_path: str = "fastboot",
) -> CommandResult:
    resolved_argv = list(argv)
    is_fastboot = bool(resolved_argv and resolved_argv[0] == "fastboot")
    if is_fastboot:
        resolved_argv[0] = fastboot_path
    stdout, stderr, code = runtime.ssh_manager.execute_command(
        ssh, shlex.join(resolved_argv), timeout=timeout,
    )
    if is_fastboot and code == 0 and _fastboot_command_failed(
        "\n".join(part for part in (stdout, stderr) if part)
    ):
        code = 1
    return CommandResult(stdout or "", stderr or "", code)


def _upload_managed_fastboot_bundle(
    ssh,
    *,
    suite_dir: str,
) -> tuple[str, str]:
    """Upload and validate the controller-managed Fastboot client."""
    project_root = Path(runtime.project_root or Path(__file__).parents[2])
    archive_path = (
        project_root
        / "tools"
        / "GMS-Host-Tools"
        / _MANAGED_PLATFORM_TOOLS_ARCHIVE
    )
    if not archive_path.is_file():
        raise FastbootFirmwareError(
            "缺少受管Android platform-tools归档，已在写盘前停止: "
            f"{archive_path}"
        )
    try:
        with zipfile.ZipFile(archive_path) as archive:
            fastboot_data = archive.read(_MANAGED_FASTBOOT_ARCHIVE_PATH)
            libcxx_data = archive.read(_MANAGED_LIBCXX_ARCHIVE_PATH)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise FastbootFirmwareError(
            f"读取受管Fastboot归档失败，已在写盘前停止: {exc}"
        ) from exc
    if not fastboot_data or not libcxx_data:
        raise FastbootFirmwareError("受管Fastboot归档内容为空，已在写盘前停止")

    remote_dir = os.path.join(suite_dir, _MANAGED_FASTBOOT_DIR)
    remote_lib_dir = os.path.join(remote_dir, "lib64")
    remote_fastboot = os.path.join(remote_dir, "fastboot")
    remote_libcxx = os.path.join(remote_lib_dir, "libc++.so")
    upload_suffix = f".upload-{os.getpid()}-{time.time_ns()}"
    temporary_fastboot = remote_fastboot + upload_suffix
    temporary_libcxx = remote_libcxx + upload_suffix
    setup_out, setup_err, setup_code = runtime.ssh_manager.execute_command(
        ssh,
        f"mkdir -p {shlex.quote(remote_lib_dir)}",
        timeout=30,
    )
    if setup_code != 0:
        raise FastbootFirmwareError(
            "创建远端受管Fastboot目录失败: "
            + (setup_err or setup_out or f"exit {setup_code}")[-200:]
        )

    try:
        with ssh.open_sftp() as sftp:
            runtime.ssh_manager.optimize_sftp_performance(sftp)
            sftp.putfo(
                io.BytesIO(fastboot_data),
                temporary_fastboot,
                file_size=len(fastboot_data),
                confirm=True,
            )
            sftp.putfo(
                io.BytesIO(libcxx_data),
                temporary_libcxx,
                file_size=len(libcxx_data),
                confirm=True,
            )
            sftp.chmod(temporary_fastboot, 0o755)
            sftp.chmod(temporary_libcxx, 0o644)
    except Exception as exc:
        raise FastbootFirmwareError(
            f"上传受管Fastboot失败，已在写盘前停止: {exc}"
        ) from exc

    install_command = (
        f"mv -f -- {shlex.quote(temporary_fastboot)} "
        f"{shlex.quote(remote_fastboot)} && "
        f"mv -f -- {shlex.quote(temporary_libcxx)} "
        f"{shlex.quote(remote_libcxx)} && "
        f"chmod 755 {shlex.quote(remote_fastboot)} && "
        f"chmod 644 {shlex.quote(remote_libcxx)}"
    )
    install_out, install_err, install_code = runtime.ssh_manager.execute_command(
        ssh,
        install_command,
        timeout=30,
    )
    if install_code != 0:
        raise FastbootFirmwareError(
            "安装远端受管Fastboot失败，已在写盘前停止: "
            + (install_err or install_out or f"exit {install_code}")[-200:]
        )

    version_out, version_err, version_code = runtime.ssh_manager.execute_command(
        ssh,
        f"{shlex.quote(remote_fastboot)} --version",
        timeout=30,
    )
    version_text = "\n".join(
        part.strip() for part in (version_out, version_err) if part.strip()
    )
    if version_code != 0 or not version_text:
        raise FastbootFirmwareError(
            "受管Fastboot版本检查失败，已在写盘前停止: "
            + (version_text or f"exit {version_code}")[-200:]
        )
    return remote_fastboot, version_text.splitlines()[0]


async def _prepare_remote_fastboot(
    ssh,
    *,
    suite_dir: str,
) -> tuple[str, str]:
    return await asyncio.to_thread(
        _upload_managed_fastboot_bundle,
        ssh,
        suite_dir=suite_dir,
    )


async def _extract_update_image(
    ssh,
    *,
    suite_dir: str,
    remote_tool: str,
    remote_firmware: str,
    on_log=None,
    on_progress=None,
) -> ExtractedFirmware:
    quoted_tool = shlex.quote(remote_tool)
    quoted_firmware = shlex.quote(remote_firmware)
    stdout, stderr, code = await asyncio.to_thread(
        runtime.ssh_manager.execute_command,
        ssh,
        f"{quoted_tool} SFI {quoted_firmware}",
        timeout=120,
    )
    sfi_output = "\n".join(part for part in (stdout, stderr) if part)
    if code != 0:
        raise FastbootFirmwareError(f"解析固件信息失败（SFI退出码 {code}）")
    entries = parse_sfi_entries(sfi_output)
    if not entries:
        raise FastbootFirmwareError("update.img中没有可解析的SFI条目")
    for entry in entries:
        if not _safe_entry_file(entry.file):
            raise FastbootFirmwareError(
                f"固件条目包含不安全的文件路径: {entry.file!r}"
            )
    parameter_entries = [
        entry for entry in entries
        if " ".join((entry.entry_type or "").lower().split()) == "parameter"
    ]
    if len(parameter_entries) != 1:
        raise FastbootFirmwareError("update.img必须且只能包含一个parameter条目")
    parameter_file = parameter_entries[0].file

    stat_out, stat_err, stat_code = await asyncio.to_thread(
        runtime.ssh_manager.execute_command,
        ssh,
        f"stat -c %s -- {quoted_firmware}",
        timeout=30,
    )
    if stat_code != 0 or not (stat_out or "").strip().isdigit():
        raise FastbootFirmwareError(
            "读取固件大小失败: " + (stat_err or stat_out or "unknown")[-160:]
        )
    firmware_size = int((stat_out or "").strip())
    hash_out, hash_err, hash_code = await asyncio.to_thread(
        runtime.ssh_manager.execute_command,
        ssh,
        f"sha256sum -- {quoted_firmware}",
        timeout=_command_timeout(firmware_size),
    )
    hash_match = re.match(r"^([0-9a-fA-F]{64})\s", (hash_out or "").strip())
    if hash_code != 0 or not hash_match:
        raise FastbootFirmwareError(
            "计算固件SHA-256失败: " + (hash_err or hash_out or "unknown")[-160:]
        )
    firmware_identity = f"{firmware_size}:{hash_match.group(1).lower()}"

    extract_dir = _extract_dir(suite_dir, os.path.basename(remote_firmware))
    expected_sizes: dict[str, int] = {}
    for entry in entries:
        if _safe_entry_file(entry.file):
            expected_sizes.setdefault(entry.file, int(entry.size or 0))
    needed = sorted(expected_sizes)
    quoted_dir = shlex.quote(extract_dir)
    quoted_identity = shlex.quote(os.path.join(extract_dir, _CACHE_IDENTITY_FILE))
    stat_command = (
        f"cd {quoted_dir} && cat {quoted_identity} && printf '\n' && "
        "stat -c '%n %s' "
        + " ".join(shlex.quote(name) for name in needed)
        + " 2>/dev/null"
    )
    stat_out, _stat_err, stat_code = await asyncio.to_thread(
        runtime.ssh_manager.execute_command, ssh, stat_command, timeout=60,
    )
    stat_lines = (stat_out or "").splitlines()
    cached_identity = stat_lines[0].strip() if stat_lines else ""
    actual_sizes: dict[str, int] = {}
    for line in stat_lines[1:]:
        name, sep, size = line.rpartition(" ")
        if sep and size.isdigit():
            actual_sizes[name] = int(size)
    cache_hit = (
        stat_code == 0
        and cached_identity == firmware_identity
        and not extracted_file_size_mismatches(entries, actual_sizes)
    )
    if not cache_hit:
        if on_log:
            await on_log(
                "解包USB/IP Fastboot固件分区镜像"
                "（Rockchip EXF，等价RKImageMaker + AFPTool最终输出）..."
            )
        # ``extract_dir`` is derived from a basename and rooted below
        # ``suite_dir/fw_extract`` by _extract_dir.
        _out, _err, setup_code = await asyncio.to_thread(
            runtime.ssh_manager.execute_command,
            ssh,
            f"rm -rf {quoted_dir} && mkdir -p {quoted_dir}",
            timeout=120,
        )
        if setup_code != 0:
            raise FastbootFirmwareError("创建固件解包目录失败")

        async def extract_chunk(chunk: str) -> None:
            percent = _last_percent(chunk)
            if percent is not None and on_progress:
                await on_progress(8.0 * percent / 100.0)

        try:
            extract_output, extract_code = await _stream_tool_command(
                ssh,
                f"cd {quoted_dir} && {quoted_tool} EXF {quoted_firmware} {quoted_dir}",
                timeout=_command_timeout(4 * 1024 * 1024 * 1024),
                on_chunk=extract_chunk,
            )
        except PartitionBurnError as exc:
            raise FastbootFirmwareError(str(exc), exc.status_code) from exc
        if extract_code != 0 or "Extract ok" not in extract_output:
            raise FastbootFirmwareError(
                "固件解包失败（EXF）: " + strip_ansi_codes(extract_output)[-240:]
            )

    verify_command = (
        f"cd {quoted_dir} && stat -c '%n %s' "
        + " ".join(shlex.quote(name) for name in needed)
    )
    verify_out, verify_err, verify_code = await asyncio.to_thread(
        runtime.ssh_manager.execute_command, ssh, verify_command, timeout=120,
    )
    verified_sizes: dict[str, int] = {}
    for line in (verify_out or "").splitlines():
        name, sep, size = line.rpartition(" ")
        if sep and size.isdigit():
            verified_sizes[name] = int(size)
    mismatched = extracted_file_size_mismatches(entries, verified_sizes)
    if verify_code != 0 or mismatched:
        detail = ", ".join(mismatched[:6]) or (verify_err or "stat failed")[-160:]
        raise FastbootFirmwareError(f"固件解包文件缺失或大小不匹配: {detail}")

    parameter_path = os.path.join(extract_dir, parameter_file)
    parameter_out, parameter_err, parameter_code = await asyncio.to_thread(
        runtime.ssh_manager.execute_command,
        ssh,
        f"base64 -w0 -- {shlex.quote(parameter_path)}",
        timeout=30,
    )
    try:
        parameter_text = base64.b64decode(
            (parameter_out or "").strip(), validate=True,
        ).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise FastbootFirmwareError("parameter.txt内容编码无效") from exc
    if parameter_code != 0:
        raise FastbootFirmwareError(
            "读取parameter.txt失败: " + (parameter_err or "unknown")[-160:]
        )
    partitions = parse_parameter_partitions(parameter_text)
    if not partitions or not any(partition.grow for partition in partitions):
        raise FastbootFirmwareError("parameter.txt没有有效GPT分区或grow分区")
    plan = build_fastboot_write_plan(entries, partitions)

    gpt_path = os.path.join(extract_dir, _GPT_TEMPLATE_FILE)
    gpt_out, gpt_err, gpt_code = await asyncio.to_thread(
        runtime.ssh_manager.execute_command,
        ssh,
        f"rm -f -- {shlex.quote(gpt_path)} && {quoted_tool} GPT "
        f"{shlex.quote(parameter_path)} {shlex.quote(gpt_path)}",
        timeout=120,
    )
    if gpt_code != 0:
        raise FastbootFirmwareError(
            "生成GPT模板失败: " + (gpt_err or gpt_out or "unknown")[-240:]
        )
    gpt_data_out, gpt_data_err, gpt_data_code = await asyncio.to_thread(
        runtime.ssh_manager.execute_command,
        ssh,
        f"base64 -w0 -- {shlex.quote(gpt_path)}",
        timeout=30,
    )
    try:
        gpt_template = base64.b64decode(
            (gpt_data_out or "").strip(), validate=True,
        )
    except ValueError as exc:
        raise FastbootFirmwareError("GPT模板编码无效") from exc
    if gpt_data_code != 0:
        raise FastbootFirmwareError(
            "读取GPT模板失败: " + (gpt_data_err or "unknown")[-160:]
        )
    try:
        template_entries = parse_gpt_entries(gpt_template[_GPT_HEADER_OFFSET:])
    except PartitionBurnError as exc:
        raise FastbootFirmwareError(str(exc)) from exc
    if not tables_match(template_entries, partitions):
        raise FastbootFirmwareError("GPT模板与parameter.txt分区布局不一致")

    marker_out, marker_err, marker_code = await asyncio.to_thread(
        runtime.ssh_manager.execute_command,
        ssh,
        f"printf %s {shlex.quote(firmware_identity)} > {quoted_identity}",
        timeout=30,
    )
    if marker_code != 0:
        raise FastbootFirmwareError(
            "写入固件缓存标识失败: " + (marker_err or marker_out or "unknown")[-160:]
        )
    if on_progress:
        await on_progress(8.0)
    return ExtractedFirmware(
        extract_dir=extract_dir,
        plan=plan,
        partitions=tuple(partitions),
        gpt_template=gpt_template,
    )


async def _remote_image_expanded_size(
    ssh, image_path: str, entry_type: str, packed_size: int,
) -> int:
    if entry_type != "sparse image":
        return packed_size
    stdout, stderr, code = await asyncio.to_thread(
        runtime.ssh_manager.execute_command,
        ssh,
        f"od -An -tx1 -N28 -- {shlex.quote(image_path)}",
        timeout=30,
    )
    if code != 0:
        raise FastbootFirmwareError(
            f"读取sparse镜像头失败: {os.path.basename(image_path)}: "
            + (stderr or stdout or f"exit {code}").strip()[-160:]
        )
    try:
        header = bytes.fromhex("".join((stdout or "").split()))
    except ValueError as exc:
        raise FastbootFirmwareError(
            f"sparse镜像头格式无效: {os.path.basename(image_path)}"
        ) from exc
    expanded = parse_android_sparse_expanded_size(header)
    if expanded is None:
        raise FastbootFirmwareError(
            f"{os.path.basename(image_path)} 被声明为sparse image，"
            "但未检测到有效Android sparse头"
        )
    return expanded


async def _prepare_zero_filled_sparse_image(ssh, image_path: str) -> str:
    """Create one sparse image whose holes are explicit zero-fill chunks."""
    normalizer_path = os.path.join(
        os.path.dirname(image_path), _SPARSE_NORMALIZER_FILE,
    )
    normalized_path = image_path + _SPARSE_ZERO_FILLED_SUFFIX
    normalizer = Path(__file__).with_name("sparse_image.py").read_bytes()
    await _upload_remote_bytes(ssh, normalizer_path, normalizer)
    temporary_pattern = normalized_path + ".tmp.XXXXXX"
    command = (
        f"if test -s {shlex.quote(normalized_path)} "
        f"&& test {shlex.quote(normalized_path)} -nt {shlex.quote(image_path)}; then "
        f"stat -c %s -- {shlex.quote(normalized_path)}; "
        "else "
        f"tmp=$(mktemp {shlex.quote(temporary_pattern)}) || exit 1; "
        "trap 'rm -f -- \"$tmp\"' EXIT; "
        f"python3 {shlex.quote(normalizer_path)} --full "
        f"{shlex.quote(image_path)} \"$tmp\" "
        f"&& chmod 600 \"$tmp\" && mv -f -- \"$tmp\" "
        f"{shlex.quote(normalized_path)} "
        f"&& stat -c %s -- {shlex.quote(normalized_path)}; "
        "fi"
    )
    stdout, stderr, code = await asyncio.to_thread(
        runtime.ssh_manager.execute_command, ssh, command, timeout=900,
    )
    sizes = [line.strip() for line in (stdout or "").splitlines() if line.strip().isdigit()]
    if code != 0 or not sizes or int(sizes[-1]) <= 0:
        raise FastbootFirmwareError(
            f"生成完整零填充sparse镜像失败: {os.path.basename(image_path)}: "
            + (stderr or stdout or f"exit {code}")[-240:]
        )
    return normalized_path


async def _prepare_truncated_sparse_image(
    ssh,
    *,
    image_path: str,
    partition_size: int,
    block_size: int = _LOGICAL_BLOCK_SIZE,
) -> str:
    """Clamp a sparse super image's declared size to the device partition.

    Some Rockchip firmware packages ship a super image whose expanded size
    exceeds the ``parameter.txt`` super partition on smaller-storage variants.
    Flashing it verbatim is rejected by fastbootd ("resize failed"/"Not enough
    space").  The image tail past the partition is unallocated DONT_CARE
    space, so clamping the header's ``total_blocks`` preserves every populated
    filesystem block while fitting the physical partition.
    """
    truncated_path = image_path + _SPARSE_TRUNCATED_SUFFIX
    normalizer_path = os.path.join(
        os.path.dirname(image_path), _SPARSE_NORMALIZER_FILE,
    )
    normalizer = Path(__file__).with_name("sparse_image.py").read_bytes()
    await _upload_remote_bytes(ssh, normalizer_path, normalizer)
    max_blocks = partition_size // block_size
    temporary_pattern = truncated_path + ".tmp.XXXXXX"
    command = (
        f"tmp=$(mktemp {shlex.quote(temporary_pattern)}) || exit 1; "
        "trap 'rm -f -- \"$tmp\"' EXIT; "
        f"python3 {shlex.quote(normalizer_path)} --truncate "
        f"{shlex.quote(image_path)} {max_blocks} \"$tmp\" "
        f"&& chmod 600 \"$tmp\" && mv -f -- \"$tmp\" "
        f"{shlex.quote(truncated_path)} "
        f"&& stat -c %s -- {shlex.quote(truncated_path)}"
    )
    stdout, stderr, code = await asyncio.to_thread(
        runtime.ssh_manager.execute_command, ssh, command, timeout=900,
    )
    sizes = [
        line.strip() for line in (stdout or "").splitlines()
        if line.strip().isdigit()
    ]
    if code != 0 or not sizes or int(sizes[-1]) <= 0:
        raise FastbootFirmwareError(
            f"裁剪super镜像以适配分区容量失败: {os.path.basename(image_path)}: "
            + (stderr or stdout or f"exit {code}")[-240:]
        )
    return truncated_path


async def _generate_segment_sparse_image(
    ssh,
    *,
    raw_image_path: str,
    segment_path: str,
    total_size: int,
    start: int,
    end: int,
) -> None:
    """Build one single-download sparse segment on the test host."""
    normalizer_path = os.path.join(
        os.path.dirname(raw_image_path), _SPARSE_NORMALIZER_FILE,
    )
    normalizer = Path(__file__).with_name("sparse_image.py").read_bytes()
    await _upload_remote_bytes(ssh, normalizer_path, normalizer)
    temporary_pattern = segment_path + ".tmp.XXXXXX"
    command = (
        f"tmp=$(mktemp {shlex.quote(temporary_pattern)}) || exit 1; "
        "trap 'rm -f -- \"$tmp\"' EXIT; "
        f"python3 {shlex.quote(normalizer_path)} --segment "
        f"{shlex.quote(raw_image_path)} {total_size} "
        f"{_LOGICAL_BLOCK_SIZE} {start} {end} \"$tmp\" "
        f"&& chmod 600 \"$tmp\" && mv -f -- \"$tmp\" "
        f"{shlex.quote(segment_path)} "
        f"&& stat -c %s -- {shlex.quote(segment_path)}"
    )
    stdout, stderr, code = await asyncio.to_thread(
        runtime.ssh_manager.execute_command, ssh, command, timeout=600,
    )
    sizes = [
        line.strip() for line in (stdout or "").splitlines()
        if line.strip().isdigit()
    ]
    if code != 0 or not sizes or int(sizes[-1]) <= 0:
        raise FastbootFirmwareError(
            f"生成分段sparse镜像失败 ({start}-{end}): "
            + (stderr or stdout or f"exit {code}")[-240:]
        )


async def _flash_logical_image(
    ssh,
    runner,
    *,
    device: str,
    partition: str,
    image_path: str,
    image_size: int,
    max_download: int | None,
    on_log,
) -> None:
    """Flash one logical partition image in bounded sparse transfers.

    When ``max-download-size`` is available, every image is emitted as one or
    more RAW-only sparse transfers.  This prevents the host fastboot client
    from converting zero blocks to DONT_CARE chunks and from invisibly
    splitting one flash command into several writes.  Each segment declares
    the partition's full extent but covers only its own range with RAW data.

    Generic partition readback is deliberately not attempted. Android
    Fastbootd applies a device-side ``fetch`` allowlist; on RK3572 it permits
    vendor_boot but rejects logical partitions such as odm/system/vendor.
    Every transfer must instead return success, and Android boot/AVB remains
    the end-to-end validation performed after all writes complete.
    """
    def runner_read(argv: list[str], timeout: int) -> CommandResult:
        return runner(argv, timeout)

    if max_download is not None and max_download > 0:
        segment_dir = os.path.join(
            os.path.dirname(image_path), _LOGICAL_SEGMENT_DIR,
        )
        await asyncio.to_thread(
            runtime.ssh_manager.execute_command,
            ssh,
            f"mkdir -p {shlex.quote(segment_dir)}",
            timeout=30,
        )
        ranges = plan_segment_ranges(image_size, max_download)
        if on_log:
            await on_log(
                f"设备 {device}：{partition} 使用 {len(ranges)} 个"
                f"RAW-only sparse分段写入"
                f"（镜像 {image_size} 字节，"
                f"max-download-size {max_download}）"
            )
        for index, (start, end) in enumerate(ranges):
            segment_path = os.path.join(
                segment_dir,
                f"{partition}.{index:04d}-{start:X}-{end:X}.sparse",
            )
            await _generate_segment_sparse_image(
                ssh,
                raw_image_path=image_path,
                segment_path=segment_path,
                total_size=image_size,
                start=start,
                end=end,
            )
            result = await asyncio.to_thread(
                runner_read,
                [
                    "fastboot", "-s", device, "flash",
                    partition, segment_path,
                ],
                max(300, int((end - start) / BYTES_PER_SECOND_FLOOR)),
            )
            if result.code != 0:
                raise FastbootFirmwareError(
                    f"设备 {device} 分段写入 {partition} "
                    f"({start}-{end}) 失败: "
                    + (result.output or f"exit {result.code}")[-300:]
                )
    else:
        result = await asyncio.to_thread(
            runner_read,
            ["fastboot", "-s", device, "flash", partition, image_path],
            max(300, int(image_size / BYTES_PER_SECOND_FLOOR)),
        )
        if result.code != 0:
            raise FastbootFirmwareError(
                f"设备 {device} 烧写 {partition} 失败: "
                + (result.output or f"exit {result.code}")[-300:]
            )
    if on_log:
        await on_log(f"设备 {device}：{partition} 写入命令全部成功")


async def _prepare_logical_partition_images(
    ssh,
    *,
    suite_dir: str,
    super_image_path: str,
) -> tuple[LogicalPartitionImage, ...]:
    """Extract every populated logical partition from one sparse super image.

    RK3572 Fastbootd can report success while a multi-download physical
    ``super`` flash leaves individual logical extents from the previous
    firmware behind.  Reflashing the unpacked logical images avoids that
    device-specific whole-super transaction bug.
    """
    find_command = (
        f"find {shlex.quote(suite_dir)} -type f -name lpunpack "
        "-path '*MicrodroidHostTestCases*' -print -quit"
    )
    tool_out, tool_err, tool_code = await asyncio.to_thread(
        runtime.ssh_manager.execute_command,
        ssh,
        find_command,
        timeout=120,
    )
    lpunpack_path = (tool_out or "").strip().splitlines()
    lpunpack_path = lpunpack_path[0].strip() if lpunpack_path else ""
    if (
        tool_code != 0
        or not lpunpack_path.startswith(suite_dir.rstrip("/") + "/")
        or not PurePosixPath(lpunpack_path).is_absolute()
    ):
        raise FastbootFirmwareError(
            "未找到可信的lpunpack，无法逐分区校正super写入: "
            + (tool_err or tool_out or f"exit {tool_code}")[-200:]
        )
    simg2img_path = os.path.join(os.path.dirname(lpunpack_path), "simg2img")
    check_out, check_err, check_code = await asyncio.to_thread(
        runtime.ssh_manager.execute_command,
        ssh,
        f"test -x {shlex.quote(lpunpack_path)} "
        f"&& test -x {shlex.quote(simg2img_path)}",
        timeout=30,
    )
    if check_code != 0:
        raise FastbootFirmwareError(
            "lpunpack/simg2img工具不完整，无法逐分区校正super写入: "
            + (check_err or check_out or f"exit {check_code}")[-200:]
        )

    output_dir = os.path.join(os.path.dirname(super_image_path), _LOGICAL_IMAGES_DIR)
    complete_marker = os.path.join(output_dir, ".complete")
    lock_path = output_dir + ".lock"
    temporary_pattern = output_dir + ".tmp.XXXXXX"
    list_command = (
        f"test -f {shlex.quote(complete_marker)} && "
        f"find {shlex.quote(output_dir)} -maxdepth 1 -type f "
        "-name '*.img' -size +0c -printf '%f %s\\n'"
    )
    list_out, _list_err, list_code = await asyncio.to_thread(
        runtime.ssh_manager.execute_command,
        ssh,
        list_command,
        timeout=60,
    )
    if list_code != 0 or not (list_out or "").strip():
        build_inner = (
            f"if {list_command}; then :; else "
            f"tmp=$(mktemp -d {shlex.quote(temporary_pattern)}) || exit 1; "
            "trap 'rm -rf -- \"$tmp\"' EXIT; "
            "mkdir -p \"$tmp/parts\"; "
            f"{shlex.quote(simg2img_path)} {shlex.quote(super_image_path)} "
            "\"$tmp/super.raw\" && "
            f"{shlex.quote(lpunpack_path)} \"$tmp/super.raw\" \"$tmp/parts\" && "
            "rm -f -- \"$tmp/super.raw\" && "
            "printf 'complete\\n' > \"$tmp/parts/.complete\" && "
            f"rm -rf -- {shlex.quote(output_dir)} && "
            f"mv -- \"$tmp/parts\" {shlex.quote(output_dir)} && "
            "rmdir -- \"$tmp\" && trap - EXIT && "
            f"{list_command}; fi"
        )
        build_command = (
            f"flock -w 1800 {shlex.quote(lock_path)} sh -c "
            f"{shlex.quote(build_inner)}"
        )
        list_out, list_err, list_code = await asyncio.to_thread(
            runtime.ssh_manager.execute_command,
            ssh,
            build_command,
            timeout=1800,
        )
        if list_code != 0:
            raise FastbootFirmwareError(
                "从super解出逻辑分区失败: "
                + (list_err or list_out or f"exit {list_code}")[-300:]
            )

    images: list[LogicalPartitionImage] = []
    for line in (list_out or "").splitlines():
        filename, separator, size_text = line.rpartition(" ")
        if not separator or not size_text.isdigit() or not filename.endswith(".img"):
            continue
        partition = filename[:-4]
        size = int(size_text)
        if (
            size <= 0
            or not _SAFE_PARTITION_RE.fullmatch(partition)
            or not _FASTBOOTD_PARTITION_RE.fullmatch(partition)
            or partition.casefold() == "super"
        ):
            continue
        images.append(LogicalPartitionImage(
            partition=partition,
            path=os.path.join(output_dir, filename),
            size=size,
        ))
    images.sort(key=lambda item: item.partition.casefold())
    if not images:
        raise FastbootFirmwareError("super中没有可逐分区烧写的非空逻辑分区")
    return tuple(images)


def _validate_expanded_images(
    plan: FastbootWritePlan,
    partitions: tuple[PartitionEntry, ...],
    image_sizes: dict[str, int],
) -> None:
    by_name = {partition.name.lower(): partition for partition in partitions}
    for step in plan.steps:
        expanded_size = image_sizes.get(step.image, 0)
        partition = by_name.get(step.partition.lower())
        if partition is None or expanded_size <= 0:
            raise FastbootFirmwareError(
                f"分区 {step.partition} 缺少有效镜像容量信息"
            )
        if not partition.grow and expanded_size > partition.size_sec * SECTOR_BYTES:
            raise FastbootFirmwareError(
                f"分区 {step.partition} 镜像展开后为 {expanded_size} 字节，"
                f"超过parameter.txt容量 {partition.size_sec * SECTOR_BYTES} 字节"
            )


def _read_adb_storage_geometry(runner, device: str) -> StorageGeometry | None:
    # /sys/class/block/<partition> resolves below its parent disk directory.
    # Linux sysfs ``size`` is expressed in 512-byte sectors.
    script = (
        "node=$(basename \"$(readlink -f /dev/block/by-name/userdata)\") || exit 1; "
        "path=$(readlink -f \"/sys/class/block/$node\") || exit 1; "
        "if [ -f \"/sys/class/block/$node/partition\" ]; then "
        "disk=$(basename \"$(dirname \"$path\")\"); else disk=$node; fi; "
        "sectors=$(cat \"/sys/class/block/$disk/size\") || exit 1; "
        "logical=$(cat \"/sys/class/block/$disk/queue/logical_block_size\") || exit 1; "
        "printf 'GMS_GEOMETRY %s %s\\n' \"$sectors\" \"$logical\""
    )
    attempts = [
        ["adb", "-s", device, "shell", script],
        # Enforcing SELinux denies the plain ``shell`` user reads of
        # /sys/class/block/*/size; userdebug test devices allow them via su.
        ["adb", "-s", device, "shell", f"su 0 sh -c {shlex.quote(script)}"],
    ]
    details: list[str] = []
    for argv in attempts:
        result = runner(argv, 20)
        if result.code == 0:
            geometry = parse_storage_geometry(result.output)
            if geometry is not None:
                _validate_geometry(geometry)
                return geometry
        details.append((result.output or f"exit {result.code}")[-160:])

    # Rockchip userdebug builds commonly omit ``su`` while still supporting
    # ``adb root``.  Restarting adbd does not write storage; after the restart
    # wait for the same serial and retry the sysfs query as root.
    root = runner(["adb", "-s", device, "root"], 20)
    root_output = root.output or f"exit {root.code}"
    details.append(root_output[-160:])
    root_denied = any(
        marker in root_output.lower()
        for marker in ("cannot run as root", "production build")
    )
    if root.code == 0 and not root_denied:
        runner(["adb", "-s", device, "wait-for-device"], 30)
        for attempt in range(4):
            result = runner(["adb", "-s", device, "shell", script], 20)
            if result.code == 0:
                geometry = parse_storage_geometry(result.output)
                if geometry is not None:
                    _validate_geometry(geometry)
                    return geometry
            details.append((result.output or f"exit {result.code}")[-160:])
            if attempt < 3:
                time.sleep(1)

    detail = "; ".join(part for part in details if part)[-320:]
    logger.warning("ADB storage geometry unavailable for %s: %s", device, detail)
    return None


def _read_uboot_storage_geometry(runner, device: str) -> StorageGeometry:
    output: list[str] = []
    for command in ("scsi info", "mmc info"):
        output.append(runner(
            ["fastboot", "-s", device, "oem", f"run:{command}"], 20,
        ).output)
        output.append(runner(
            ["fastboot", "-s", device, "oem", "console"], 20,
        ).output)
        geometry = parse_storage_geometry("\n".join(output))
        if geometry is not None:
            _validate_geometry(geometry)
            return geometry
    detail = "; ".join(part for part in output if part.strip())[-240:]
    raise FastbootFirmwareError(
        f"设备 {device}：ADB与U-Boot均未返回目标磁盘容量，不能安全生成GPT"
        + (f"（U-Boot Fastboot返回: {detail}）" if detail else "")
    )


async def _upload_remote_bytes(ssh, path: str, data: bytes) -> None:
    encoded = base64.b64encode(data).decode("ascii")
    command = (
        f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)} "
        f"&& chmod 600 {shlex.quote(path)} && stat -c %s -- {shlex.quote(path)}"
    )
    stdout, stderr, code = await asyncio.to_thread(
        runtime.ssh_manager.execute_command, ssh, command, timeout=60,
    )
    if code != 0 or (stdout or "").strip() != str(len(data)):
        raise FastbootFirmwareError(
            "上传目标GPT失败: " + (stderr or stdout or f"exit {code}")[-200:]
        )


async def _write_gpt_from_android(
    ssh,
    runner,
    *,
    device: str,
    geometry: StorageGeometry,
    primary_gpt: bytes,
    remote_base_path: str,
) -> None:
    """Write and read back both GPT copies while Android adbd is available.

    RK3572 U-Boot advertises a generic Fastboot transport but rejects
    ``flash:gpt``. Cross-layout firmware therefore has to install the primary
    and backup GPT through the rooted Android block device before rebooting to
    Bootloader Fastboot. Geometry is rechecked immediately before the first
    write, and each copy is read back before continuing.
    """
    safe_device = re.sub(r"[^A-Za-z0-9._-]", "_", device)
    backup_gpt, backup_lba = _build_backup_gpt_image(primary_gpt, geometry)
    block_size = geometry.logical_block_size
    primary_blocks = len(primary_gpt) // block_size
    backup_blocks = len(backup_gpt) // block_size
    if (
        not primary_gpt
        or len(primary_gpt) % block_size
        or len(backup_gpt) % block_size
    ):
        raise FastbootFirmwareError("最终GPT写入块未对齐")

    remote_primary = remote_base_path + ".primary"
    remote_backup = remote_base_path + ".backup"
    device_prefix = f"/data/local/tmp/gms-gpt-{safe_device}"
    device_primary = device_prefix + ".primary"
    device_backup = device_prefix + ".backup"
    await _upload_remote_bytes(ssh, remote_primary, primary_gpt)
    await _upload_remote_bytes(ssh, remote_backup, backup_gpt)
    for source, target, expected_size in (
        (remote_primary, device_primary, len(primary_gpt)),
        (remote_backup, device_backup, len(backup_gpt)),
    ):
        pushed = await asyncio.to_thread(
            runner,
            ["adb", "-s", device, "push", source, target],
            60,
        )
        if pushed.code != 0:
            raise FastbootFirmwareError(
                f"设备 {device} 上传GPT写入块失败: "
                + (pushed.output or f"exit {pushed.code}")[-240:]
            )
        size_result = await asyncio.to_thread(
            runner,
            ["adb", "-s", device, "shell", "stat", "-c", "%s", target],
            15,
        )
        size_lines = [
            line.strip() for line in size_result.stdout.splitlines()
            if line.strip().isdigit()
        ]
        if (
            size_result.code != 0
            or not size_lines
            or int(size_lines[-1]) != expected_size
        ):
            raise FastbootFirmwareError(
                f"设备 {device} 的GPT写入块大小校验失败"
            )

    ratio = block_size // SECTOR_BYTES
    script = (
        "set -eu; "
        "node=$(basename \"$(readlink -f /dev/block/by-name/userdata)\"); "
        "path=$(readlink -f \"/sys/class/block/$node\"); "
        "if [ -f \"/sys/class/block/$node/partition\" ]; then "
        "disk=$(basename \"$(dirname \"$path\")\"); else disk=$node; fi; "
        "case \"$disk\" in *[!A-Za-z0-9._-]*|'') exit 31;; esac; "
        "sectors=$(cat \"/sys/class/block/$disk/size\"); "
        "logical=$(cat \"/sys/class/block/$disk/queue/logical_block_size\"); "
        f"[ \"$sectors\" = {geometry.total_sectors} ] || exit 32; "
        f"[ \"$logical\" = {block_size} ] || exit 33; "
        "target=/dev/block/$disk; [ -b \"$target\" ] || exit 34; "
        f"dd if={shlex.quote(device_backup)} of=\"$target\" "
        f"bs={block_size} seek={backup_lba} count={backup_blocks}; sync; "
        f"dd if=\"$target\" of={shlex.quote(device_backup)}.readback "
        f"bs={block_size} skip={backup_lba} count={backup_blocks}; "
        f"cmp -s {shlex.quote(device_backup)} "
        f"{shlex.quote(device_backup)}.readback || exit 35; "
        f"dd if={shlex.quote(device_primary)} of=\"$target\" "
        f"bs={block_size} seek=0 count={primary_blocks}; sync; "
        f"dd if=\"$target\" of={shlex.quote(device_primary)}.readback "
        f"bs={block_size} skip=0 count={primary_blocks}; "
        f"cmp -s {shlex.quote(device_primary)} "
        f"{shlex.quote(device_primary)}.readback || exit 36; "
        f"rm -f {shlex.quote(device_primary)} {shlex.quote(device_backup)} "
        f"{shlex.quote(device_primary)}.readback "
        f"{shlex.quote(device_backup)}.readback; "
        f"printf 'GMS_GPT_WRITTEN {geometry.total_sectors} {block_size} {ratio}\\n'"
    )

    attempts = [
        ["adb", "-s", device, "shell", script],
        ["adb", "-s", device, "shell", f"su 0 sh -c {shlex.quote(script)}"],
    ]
    details: list[str] = []
    for argv in attempts:
        result = await asyncio.to_thread(runner, argv, 120)
        if result.code == 0 and "GMS_GPT_WRITTEN" in result.output:
            return
        details.append((result.output or f"exit {result.code}")[-240:])

    root = await asyncio.to_thread(
        runner, ["adb", "-s", device, "root"], 30,
    )
    details.append((root.output or f"exit {root.code}")[-160:])
    root_denied = any(
        marker in root.output.lower()
        for marker in ("cannot run as root", "production build")
    )
    if not root_denied:
        await asyncio.to_thread(
            runner, ["adb", "-s", device, "wait-for-device"], 30,
        )
        result = await asyncio.to_thread(
            runner, ["adb", "-s", device, "shell", script], 120,
        )
        if result.code == 0 and "GMS_GPT_WRITTEN" in result.output:
            return
        details.append((result.output or f"exit {result.code}")[-240:])

    raise FastbootFirmwareError(
        f"设备 {device} 无法通过Android root写入并读回校验GPT："
        + "; ".join(part for part in details if part)[-500:]
    )


async def _wait_for_android_boot(
    runner,
    device: str,
    *,
    usbip_route: dict | None = None,
    timeout: int = ANDROID_BOOT_TIMEOUT_SECONDS,
) -> str:
    """Wait for Android and return its current ADB serial.

    A full firmware can legitimately change ``ro.serialno``/USB iSerial.  For
    USB/IP devices, resolve that replacement only through the already-owned
    physical ``(source_host, BUSID)`` route; never accept an arbitrary new ADB
    row from the shared test host.
    """
    deadline = time.monotonic() + max(1, timeout)
    last_output = ""
    while time.monotonic() < deadline:
        candidates = [device]
        routed_serial = await asyncio.to_thread(
            _resolve_usbip_route_serial, runner, usbip_route,
        )
        if routed_serial and routed_serial not in candidates:
            candidates.append(routed_serial)
        for candidate in candidates:
            state = await asyncio.to_thread(
                runner, ["adb", "-s", candidate, "get-state"], 10,
            )
            last_output = state.output or last_output
            if state.code == 0 and "device" in state.output.lower():
                completed = await asyncio.to_thread(
                    runner,
                    [
                        "adb", "-s", candidate, "shell", "getprop",
                        "sys.boot_completed",
                    ],
                    10,
                )
                last_output = completed.output or last_output
                if completed.code == 0 and completed.output.strip() == "1":
                    return candidate
        await asyncio.sleep(ANDROID_POLL_INTERVAL_SECONDS)
    raise FastbootFirmwareError(
        f"设备 {device} 已写入并重启，但 {timeout} 秒内未完成Android启动: "
        + (last_output or "ADB unavailable")[-200:]
    )


def _normalize_usbip_host(host: str) -> str:
    value = str(host or "").strip()
    if "@" in value:
        value = value.split("@", 1)[-1]
    return value.strip("[]").casefold()


def _resolve_usbip_route_serial(runner, route: dict | None) -> str:
    """Resolve the USB descriptor serial for one exact imported BUSID."""
    if not isinstance(route, dict):
        return ""
    source_host = _normalize_usbip_host(
        route.get("source_host") or route.get("device_host") or ""
    )
    remote_busid = str(route.get("busid") or "").strip()
    if not source_host or not re.fullmatch(r"\d+-\d+(?:\.\d+)*", remote_busid):
        return ""

    port_result = runner(["sudo", "-n", "usbip", "port"], 10)
    if port_result.code != 0:
        return ""
    local_busid = ""
    for match in _USBIP_PORT_ROUTE_RE.finditer(port_result.output or ""):
        if (
            _normalize_usbip_host(match.group("host")) == source_host
            and match.group("remote_busid") == remote_busid
        ):
            local_busid = match.group("local_busid")
            break
    if not local_busid:
        return ""

    serial_result = runner(
        ["cat", "--", f"/sys/bus/usb/devices/{local_busid}/serial"], 5,
    )
    serial = (serial_result.stdout or "").strip().splitlines()
    value = serial[0].strip() if serial else ""
    if serial_result.code != 0 or not _USB_SERIAL_RE.fullmatch(value):
        return ""
    return value


async def run_usbip_fastboot_firmware(
    ssh,
    *,
    suite_dir: str,
    remote_tool: str,
    remote_firmware: str,
    devices: list[str],
    usbip_device_routes: dict[str, dict] | None = None,
    on_transport_reset=None,
    on_android_serial_changed: Callable[[str, str, dict], Awaitable[None]] | None = None,
    on_log=None,
    on_progress=None,
) -> dict:
    """Burn a complete Android firmware layout via U-Boot/Fastbootd."""
    if not devices:
        raise FastbootFirmwareError("没有可烧写的USB/IP设备")
    managed_fastboot, fastboot_version = await _prepare_remote_fastboot(
        ssh,
        suite_dir=suite_dir,
    )
    if on_log:
        await on_log(f"使用受管Fastboot工具: {fastboot_version}")
    if on_log:
        await on_log(
            "USB/IP完整固件模式将更新GPT并清除metadata/cache/userdata中的用户数据"
        )
    extracted = await _extract_update_image(
        ssh,
        suite_dir=suite_dir,
        remote_tool=remote_tool,
        remote_firmware=remote_firmware,
        on_log=on_log,
        on_progress=on_progress,
    )
    for item in extracted.plan.skipped:
        if on_log:
            await on_log(f"跳过 {item}")

    image_sizes: dict[str, int] = {}
    for step in extracted.plan.steps:
        if step.image not in image_sizes:
            image_sizes[step.image] = await _remote_image_expanded_size(
                ssh,
                os.path.join(extracted.extract_dir, step.image),
                step.entry_type,
                step.packed_size,
            )
    _validate_expanded_images(extracted.plan, extracted.partitions, image_sizes)
    zero_filled_sparse_images: dict[str, str] = {}
    logical_partition_images: tuple[LogicalPartitionImage, ...] = ()
    for step in extracted.plan.fastbootd:
        if step.partition.lower() == "super" and step.entry_type == "sparse image":
            if on_log:
                await on_log(
                    "正在生成单文件完整零填充super sparse镜像，"
                    "防止分段写入丢失空洞清理结果"
                )
            zero_filled_sparse_images[
                step.image
            ] = await _prepare_zero_filled_sparse_image(
                ssh,
                os.path.join(extracted.extract_dir, step.image),
            )
            if on_log:
                await on_log(
                    "正在从super解出全部非空逻辑分区，"
                    "用于整块写入后的逐分区一致性校正"
                )
            logical_partition_images = await _prepare_logical_partition_images(
                ssh,
                suite_dir=suite_dir,
                super_image_path=os.path.join(extracted.extract_dir, step.image),
            )
    total_bytes = sum(
        image_sizes[step.image] for step in extracted.plan.steps
    ) + sum(image.size for image in logical_partition_images)
    completed_bytes = 0
    results: list[dict] = []
    route_by_device = usbip_device_routes or {}

    for device in devices:
        if on_log:
            await on_log(f"设备 {device}：读取存储容量并进入Bootloader Fastboot")

        def runner(argv: list[str], timeout: int) -> CommandResult:
            return _remote_runner(
                ssh,
                argv,
                timeout,
                fastboot_path=managed_fastboot,
            )

        try:
            geometry = await asyncio.to_thread(
                _read_adb_storage_geometry, runner, device,
            )
            if geometry is None and on_log:
                await on_log(
                    f"设备 {device}：ADB未返回磁盘容量，"
                    "进入Bootloader Fastboot后改用U-Boot读取"
                )
            preparer = FastbootPreparer(
                runner,
                on_transport_reset=on_transport_reset,
            )
            prepared = await asyncio.to_thread(preparer.prepare_bootloader, device)
            await asyncio.to_thread(preparer.unlock_bootloader, prepared)
            if geometry is None:
                try:
                    geometry = await asyncio.to_thread(
                        _read_uboot_storage_geometry, runner, device,
                    )
                except FastbootFirmwareError as uboot_error:
                    # Some RK3572 U-Boot builds expose neither ``oem run`` nor
                    # a capacity getvar.  No disk write has happened yet, so a
                    # temporary Android round-trip is the only safe way to read
                    # the real block-device size instead of guessing it.
                    if on_log:
                        await on_log(
                            f"设备 {device}：U-Boot Fastboot不支持容量查询，"
                            "尚未写盘；临时重启Android并通过adb root读取"
                        )
                    reboot = await asyncio.to_thread(
                        runner, ["fastboot", "-s", device, "reboot"], 30,
                    )
                    if on_transport_reset:
                        on_transport_reset(device, "adb")
                    try:
                        await _wait_for_android_boot(
                            runner, device,
                            usbip_route=route_by_device.get(device),
                        )
                    except FastbootFirmwareError as boot_error:
                        detail = (
                            reboot.output or f"exit {reboot.code}"
                        )[-160:]
                        raise FastbootFirmwareError(
                            f"{uboot_error}；为安全读取容量重启Android失败: "
                            f"{boot_error}；Fastboot重启返回: {detail}"
                        ) from boot_error
                    geometry = await asyncio.to_thread(
                        _read_adb_storage_geometry, runner, device,
                    )
                    if geometry is None:
                        raise FastbootFirmwareError(
                            f"{uboot_error}；Android启动后adb shell、su和adb root"
                            "仍无法读取目标磁盘容量，已停止且未写盘"
                        ) from uboot_error
                    if on_log:
                        await on_log(
                            f"设备 {device}：已读取容量 "
                            f"{geometry.total_sectors} 个512字节扇区，"
                            f"逻辑块 {geometry.logical_block_size} 字节，"
                            "重新进入Bootloader"
                        )
                    prepared = await asyncio.to_thread(
                        preparer.prepare_bootloader, device,
                    )
                    await asyncio.to_thread(
                        preparer.unlock_bootloader, prepared,
                    )
            finalized_gpt = finalize_gpt_image(
                extracted.gpt_template, extracted.partitions, geometry,
            )
            gpt_path = os.path.join(
                extracted.extract_dir,
                f".gms-gpt-{re.sub(r'[^A-Za-z0-9._-]', '_', device)}.img",
            )
            await _upload_remote_bytes(ssh, gpt_path, finalized_gpt)

            slot_result = await asyncio.to_thread(
                runner,
                ["fastboot", "-s", device, "getvar", "current-slot"],
                15,
            )
            current_slot = parse_fastboot_current_slot(slot_result.output)
            if current_slot:
                if on_log:
                    await on_log(f"设备 {device}：当前活动槽 {current_slot}")

            if on_log:
                await on_log(
                    f"设备 {device}：更新GPT（{geometry.total_sectors} 个512字节扇区，"
                    f"逻辑块 {geometry.logical_block_size} 字节），"
                    "同步完整parameter布局"
                )
            gpt_result = await asyncio.to_thread(
                runner,
                ["fastboot", "-s", device, "flash", _GPT_SPECIAL_TARGET, gpt_path],
                60,
            )
            if gpt_result.code != 0:
                raise FastbootFirmwareError(
                    f"设备 {device} 更新GPT失败，未继续写分区: "
                    + (gpt_result.output or f"exit {gpt_result.code}")[-300:]
                )

            bootloader_plan: list[tuple[FastbootWriteStep, int]] = []
            size_probe_supported = True
            for step in extracted.plan.bootloader:
                expanded_size = image_sizes[step.image]
                partition_size = None
                if size_probe_supported:
                    size_result = await asyncio.to_thread(
                        runner,
                        [
                            "fastboot", "-s", device, "getvar",
                            f"partition-size:{step.partition}",
                        ],
                        15,
                    )
                    partition_size = parse_fastboot_partition_size(
                        size_result.output,
                    )
                    if partition_size is None:
                        if _partition_size_probe_unsupported(size_result.output):
                            size_probe_supported = False
                            if on_log:
                                await on_log(
                                    f"设备 {device}：Bootloader Fastboot不返回"
                                    f"partition-size（{(size_result.output or '')[-120:]}），"
                                    "跳过逐分区容量预检"
                                    "（镜像容量已按parameter.txt校验，烧写由分区名寻址）"
                                )
                        else:
                            raise FastbootFirmwareError(
                                f"设备 {device} 更新GPT后，Bootloader Fastboot未暴露分区 "
                                f"{step.partition}；已停止，未静默跳过"
                            )
                if (
                    partition_size is not None
                    and expanded_size > partition_size
                ):
                    raise FastbootFirmwareError(
                        f"设备 {device} 分区 {step.partition} 容量不足：镜像 "
                        f"{expanded_size} 字节，分区 {partition_size} 字节"
                    )
                bootloader_plan.append((step, expanded_size))

            for step, expanded_size in bootloader_plan:
                image_path = os.path.join(extracted.extract_dir, step.image)
                if on_log:
                    await on_log(
                        f"设备 {device}：Bootloader Fastboot烧写 "
                        f"{step.partition} <- {step.image}"
                    )
                timeout = max(300, int(expanded_size / BYTES_PER_SECOND_FLOOR))
                result = await asyncio.to_thread(
                    runner,
                    ["fastboot", "-s", device, "flash", step.partition, image_path],
                    timeout,
                )
                if result.code != 0:
                    raise FastbootFirmwareError(
                        f"设备 {device} 烧写 {step.partition} 失败: "
                        + (result.output or f"exit {result.code}")[-300:]
                    )
                completed_bytes += expanded_size
                if on_progress and total_bytes > 0:
                    await on_progress(
                        8.0 + 90.0 * completed_bytes / (total_bytes * len(devices))
                    )

            if on_log:
                await on_log(f"设备 {device}：进入Fastbootd并校验动态分区")
            await asyncio.to_thread(preparer.enter_fastbootd, prepared)
            fastbootd_plan: list[tuple[FastbootWriteStep, int]] = []
            for step in extracted.plan.fastbootd:
                size_result = await asyncio.to_thread(
                    runner,
                    [
                        "fastboot", "-s", device, "getvar",
                        f"partition-size:{step.partition}",
                    ],
                    15,
                )
                partition_size = parse_fastboot_partition_size(size_result.output)
                if partition_size is None:
                    raise FastbootFirmwareError(
                        f"设备 {device} 的Fastbootd未暴露动态分区 "
                        f"{step.partition}；已停止，未静默跳过"
                    )
                expanded_size = image_sizes[step.image]
                if expanded_size > partition_size:
                    raise FastbootFirmwareError(
                        f"分区 {step.partition} 容量不足：镜像展开后 "
                        f"{expanded_size} 字节，设备分区 {partition_size} 字节"
                    )
                fastbootd_plan.append((step, expanded_size))

            for step, expanded_size in fastbootd_plan:
                image_path = os.path.join(extracted.extract_dir, step.image)
                if step.partition.lower() == "super":
                    if on_log:
                        await on_log(
                            f"设备 {device}：清空旧super，防止跨固件sparse空洞残留"
                        )
                    erase = await asyncio.to_thread(
                        runner,
                        ["fastboot", "-s", device, "erase", "super"],
                        300,
                    )
                    if erase.code != 0:
                        erase_detail = erase.output or f"exit {erase.code}"
                        if not _super_erase_unsupported(erase_detail):
                            raise FastbootFirmwareError(
                                f"设备 {device} 清空super失败，已停止且未写入新super: "
                                + erase_detail[-300:]
                            )
                        if on_log:
                            await on_log(
                                f"设备 {device}：Fastbootd不支持擦除super，"
                                "改用单文件完整零填充sparse镜像覆盖旧数据"
                            )
                    if step.entry_type == "sparse image":
                        zero_filled = zero_filled_sparse_images.get(step.image)
                        if not zero_filled:
                            raise FastbootFirmwareError(
                                f"设备 {device} 缺少完整零填充super sparse镜像"
                            )
                        image_path = zero_filled
                    elif expanded_size != step.packed_size:
                        raise FastbootFirmwareError(
                            f"设备 {device} 的super镜像格式与容量不一致，已停止"
                        )
                if expanded_size > partition_size:
                    # Some Rockchip packages ship a super image larger than
                    # the parameter.txt super partition of smaller-storage
                    # variants.  The image tail is unallocated sparse space,
                    # so clamp its declared block count to the partition
                    # instead of failing the whole burn.
                    if step.partition.lower() != "super" or (
                        step.entry_type != "sparse image"
                    ):
                        raise FastbootFirmwareError(
                            f"分区 {step.partition} 容量不足：镜像展开后 "
                            f"{expanded_size} 字节，设备分区 {partition_size} 字节"
                        )
                    if on_log:
                        await on_log(
                            f"设备 {device}：super镜像展开后 {expanded_size} 字节，"
                            f"超过设备分区 {partition_size} 字节；"
                            "镜像尾部为未分配空间，裁剪声明容量后写入"
                        )
                    image_path = await _prepare_truncated_sparse_image(
                        ssh,
                        image_path=os.path.join(
                            extracted.extract_dir, step.image,
                        ),
                        partition_size=partition_size,
                    )
                    if on_log:
                        await on_log(
                            f"设备 {device}：已生成裁剪版super镜像 "
                            f"{os.path.basename(image_path)}"
                        )
                if on_log:
                    sparse = (
                        "（Android sparse，单次写入且空洞显式零填充）"
                        if step.entry_type == "sparse image" else ""
                    )
                    await on_log(
                        f"设备 {device}：Fastbootd烧写 "
                        f"{step.partition} <- {step.image}{sparse}"
                    )
                result = await asyncio.to_thread(
                    runner,
                    ["fastboot", "-s", device, "flash", step.partition, image_path],
                    max(300, int(expanded_size / BYTES_PER_SECOND_FLOOR)),
                )
                if result.code != 0:
                    raise FastbootFirmwareError(
                        f"设备 {device} 烧写 {step.partition} 失败: "
                        + (result.output or f"exit {result.code}")[-300:]
                    )
                completed_bytes += expanded_size
                if on_progress and total_bytes > 0:
                    await on_progress(
                        8.0 + 90.0 * completed_bytes / (total_bytes * len(devices))
                    )

            # 先擦除再写动态分区：把 -w 放到 Fastbootd 逻辑分区写入之前，
            # 避免 erase metadata/userdata 与刚写完的 super 尾部在设备端
            # 产生写入竞争（RK3572 上曾导致最后写入的 vendor_a 尾部
            # verity 元数据丢失，启动即 dm-verity device corrupted）。
            if on_log:
                await on_log(
                    f"设备 {device}：重建metadata/cache/userdata（跨GPT布局烧写会清除用户数据）"
                )
            wipe = await asyncio.to_thread(
                runner, ["fastboot", "-s", device, "-w"], 600,
            )
            if wipe.code != 0:
                raise FastbootFirmwareError(
                    f"设备 {device} 重建用户数据分区失败，已停止: "
                    + (wipe.output or f"exit {wipe.code}")[-300:]
                )

            rewritten_logical_partitions: list[str] = []
            if logical_partition_images and on_log:
                await on_log(
                    f"设备 {device}：逐个校正 "
                    f"{len(logical_partition_images)} 个非空逻辑分区，"
                    "防止单次下载截断与Fastbootd多段写入残留旧固件"
                )
            max_download: int | None = None
            for logical_image in logical_partition_images:
                size_result = await asyncio.to_thread(
                    runner,
                    [
                        "fastboot", "-s", device, "getvar",
                        f"partition-size:{logical_image.partition}",
                    ],
                    15,
                )
                partition_size = parse_fastboot_partition_size(size_result.output)
                if partition_size is None:
                    raise FastbootFirmwareError(
                        f"设备 {device} 的Fastbootd未暴露逻辑分区 "
                        f"{logical_image.partition}；已停止，避免混合固件"
                    )
                if logical_image.size > partition_size:
                    raise FastbootFirmwareError(
                        f"设备 {device} 逻辑分区 {logical_image.partition} 容量不足："
                        f"镜像 {logical_image.size} 字节，分区 {partition_size} 字节"
                    )
                if max_download is None:
                    limit_result = await asyncio.to_thread(
                        runner,
                        ["fastboot", "-s", device, "getvar", "max-download-size"],
                        15,
                    )
                    max_download = parse_fastboot_max_download(limit_result.output)
                if on_log:
                    await on_log(
                        f"设备 {device}：Fastbootd校正 "
                        f"{logical_image.partition} <- "
                        f"{os.path.basename(logical_image.path)}"
                    )
                await _flash_logical_image(
                    ssh,
                    runner,
                    device=device,
                    partition=logical_image.partition,
                    image_path=logical_image.path,
                    image_size=logical_image.size,
                    max_download=max_download,
                    on_log=on_log,
                )
                rewritten_logical_partitions.append(logical_image.partition)
                completed_bytes += logical_image.size
                if on_progress and total_bytes > 0:
                    await on_progress(
                        8.0 + 90.0 * completed_bytes / (total_bytes * len(devices))
                    )

            # 给 fastbootd 留出把最后一段 flash 落盘的时间。RK3572 的
            # fastbootd 在 flash 完成返回 OKAY 后仍在后台刷 UFS 缓存，
            # 立即 reboot 曾造成最后写入分区（vendor_a）尾部 verity 元数据
            # 丢失。固定等待让设备端写入充分落盘。
            if rewritten_logical_partitions:
                if on_log:
                    await on_log(
                        f"设备 {device}：等待设备端写入落盘（8秒）后重启"
                    )
                await asyncio.sleep(8)

            reboot = await asyncio.to_thread(
                runner, ["fastboot", "-s", device, "reboot"], 30,
            )
            if on_transport_reset:
                on_transport_reset(device, "adb")
            if on_log:
                await on_log(f"设备 {device}：等待Android完成启动验证")
            try:
                booted_device = await _wait_for_android_boot(
                    runner, device,
                    usbip_route=route_by_device.get(device),
                )
            except FastbootFirmwareError as exc:
                if reboot.code != 0:
                    raise FastbootFirmwareError(
                        f"{exc}；Fastboot重启返回: "
                        + (reboot.output or f"exit {reboot.code}")[-160:]
                    ) from exc
                raise
            if booted_device != device:
                route = route_by_device.get(device) or {}
                if not route or on_android_serial_changed is None:
                    raise FastbootFirmwareError(
                        f"设备已通过原USB/IP物理端口启动，但序列号由 {device} "
                        f"变为 {booted_device}，平台缺少身份迁移上下文"
                    )
                if on_log:
                    await on_log(
                        f"设备 {device}：检测到固件更新序列号为 {booted_device}，"
                        "正在迁移USB/IP物理分配"
                    )
                await on_android_serial_changed(device, booted_device, route)
            results.append({
                "device": booted_device,
                "original_device": device,
                "serial_changed": booted_device != device,
                "success": True,
                "gpt_updated": True,
                "userdata_wiped": True,
                "geometry": {
                    "total_sectors": geometry.total_sectors,
                    "logical_block_size": geometry.logical_block_size,
                },
                "bootloader_partitions": [
                    step.partition for step in extracted.plan.bootloader
                ],
                "fastbootd_partitions": [
                    step.partition for step in extracted.plan.fastbootd
                ],
                "logical_partitions_rewritten": rewritten_logical_partitions,
                "partitions": [step.partition for step in extracted.plan.steps],
                "skipped_partitions": list(extracted.plan.skipped),
                "current_slot": current_slot,
            })
        except Exception as exc:
            if isinstance(exc, FastbootFirmwareError):
                raise
            raise FastbootFirmwareError(
                f"设备 {device} Fastboot固件烧写失败: {exc}"
            ) from exc

    if on_progress:
        await on_progress(100.0)
    return {
        "backend": "usbip-fastboot",
        "results": results,
        "skipped": list(extracted.plan.skipped),
    }
