"""Fastboot-compatible Rockchip ``update.img`` burning for USB/IP devices.

The Windows USB/IP path must not enter RockUSB Loader/DB: that transition
creates a second Windows PnP instance and is not reliable with usbipd-win on
RK3572.  This module only extracts Android partitions from ``update.img`` and
writes partitions that the target explicitly exposes through Fastbootd.

Local Ubuntu USB firmware burning remains in ``firmware_api`` and continues to
use ``upgrade_tool uf``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

from worker_agent.fastboot_workflow import CommandResult, FastbootPreparer

from . import runtime
from .partition_burn import (
    ANDROID_SPARSE_MAGIC as ANDROID_SPARSE_MAGIC,  # re-exported for callers
)
from .partition_burn import (
    BYTES_PER_SECOND_FLOOR,
    PartitionBurnError,
    SfiEntry,
    _command_timeout,
    _extract_dir,
    _last_percent,
    _stream_tool_command,
    parse_android_sparse_expanded_size,
    parse_sfi_entries,
    strip_ansi_codes,
)


# Only Android boot/system partitions are accepted.  Rockchip boot chain,
# parameter/GPT, calibration and persistent-data partitions deliberately stay
# on the local ``upgrade_tool`` backend.
_FASTBOOT_PARTITION_RE = re.compile(
    r"^(?:"
    r"super|boot|vendor_boot|init_boot|vendor_kernel_boot|dtbo|recovery|"
    r"vbmeta(?:_system|_vendor)?|"
    r"system|system_ext|vendor|product|odm|"
    r"system_dlkm|vendor_dlkm|odm_dlkm"
    r")(?:_[ab])?$",
    re.IGNORECASE,
)
_PARTITION_SIZE_RE = re.compile(
    r"partition-size(?::[^:\s]+)?\s*:\s*(0x[0-9a-fA-F]+|[0-9]+)",
    re.IGNORECASE,
)


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


def _safe_entry_file(name: str) -> bool:
    path = PurePosixPath(str(name or ""))
    return bool(name) and path.name == name and name not in {".", ".."}


def build_fastboot_write_plan(
    entries: list[SfiEntry],
) -> tuple[list[FastbootWriteStep], list[str]]:
    """Select Fastboot-safe Android partitions from an SFI manifest."""
    steps: list[FastbootWriteStep] = []
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
        if not _FASTBOOT_PARTITION_RE.fullmatch(partition):
            skipped.append(f"{entry.file}({partition} 仅支持本地upgrade_tool烧写)")
            continue
        if partition.lower() in seen:
            raise FastbootFirmwareError(f"固件重复声明Fastboot分区: {partition}")
        if int(entry.size or 0) <= 0:
            raise FastbootFirmwareError(
                f"分区 {partition} 的镜像 {entry.file} 大小无效"
            )
        seen.add(partition.lower())
        steps.append(FastbootWriteStep(
            partition=partition,
            image=entry.file,
            entry_type=entry_type,
            packed_size=int(entry.size),
        ))

    if not steps:
        raise FastbootFirmwareError(
            "update.img 中没有可由Fastboot安全烧写的Android分区"
        )
    if not any(step.partition.lower() == "super" for step in steps):
        raise FastbootFirmwareError(
            "USB/IP Fastboot固件模式要求update.img包含super分区；"
            "缺少super时不能保证系统分区完整"
        )

    # Write system contents first and AVB metadata last.  If an intermediate
    # transfer fails, old vbmeta remains in place instead of authenticating a
    # partially written new boot chain.
    def priority(step: FastbootWriteStep) -> tuple[int, str]:
        name = step.partition.lower()
        if name == "super":
            return 0, name
        if name.startswith("vbmeta"):
            return 2, name
        return 1, name

    steps.sort(key=priority)
    return steps, skipped


def parse_fastboot_partition_size(output: str) -> int | None:
    match = _PARTITION_SIZE_RE.search(strip_ansi_codes(output or ""))
    if not match:
        return None
    with contextlib.suppress(ValueError):
        return int(match.group(1), 0)
    return None


def is_required_fastboot_partition(partition: str) -> bool:
    """Return whether omitting this image would make the system unusable.

    RK3572 Fastbootd exposes the dynamic super partition and the boot image
    family, but its HAL does not expose dtbo and can expose only one vbmeta
    alias.  Those metadata images remain on their existing on-device version
    in compatibility mode.  Missing system/boot contents is never tolerated.
    """
    name = str(partition or "").lower()
    if name == "super":
        return True
    base = re.sub(r"_[ab]$", "", name)
    return base in {
        "boot", "vendor_boot", "init_boot", "vendor_kernel_boot",
    }


def _remote_runner(ssh, argv: list[str], timeout: int) -> CommandResult:
    stdout, stderr, code = runtime.ssh_manager.execute_command(
        ssh, shlex.join(argv), timeout=timeout,
    )
    return CommandResult(stdout or "", stderr or "", code)


async def _extract_update_image(
    ssh,
    *,
    suite_dir: str,
    remote_tool: str,
    remote_firmware: str,
    on_log=None,
    on_progress=None,
) -> tuple[str, list[FastbootWriteStep], list[str]]:
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
    steps, skipped = build_fastboot_write_plan(entries)

    extract_dir = _extract_dir(suite_dir, os.path.basename(remote_firmware))
    expected_sizes: dict[str, int] = {}
    for entry in entries:
        if _safe_entry_file(entry.file):
            expected_sizes.setdefault(entry.file, int(entry.size or 0))
    needed = sorted(expected_sizes)
    quoted_dir = shlex.quote(extract_dir)
    stat_command = (
        f"cd {quoted_dir} && stat -c '%n %s' "
        + " ".join(shlex.quote(name) for name in needed)
        + " 2>/dev/null"
    )
    stat_out, _stat_err, stat_code = await asyncio.to_thread(
        runtime.ssh_manager.execute_command, ssh, stat_command, timeout=60,
    )
    actual_sizes: dict[str, int] = {}
    for line in (stat_out or "").splitlines():
        name, sep, size = line.rpartition(" ")
        if sep and size.isdigit():
            actual_sizes[name] = int(size)
    cache_hit = stat_code == 0 and all(
        actual_sizes.get(name) == expected_sizes[name] for name in needed
    )
    if not cache_hit:
        if on_log:
            await on_log("解包USB/IP Fastboot固件分区镜像（EXF）...")
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
    if on_progress:
        await on_progress(8.0)
    return extract_dir, steps, skipped


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


async def run_usbip_fastboot_firmware(
    ssh,
    *,
    suite_dir: str,
    remote_tool: str,
    remote_firmware: str,
    devices: list[str],
    on_transport_reset=None,
    on_log=None,
    on_progress=None,
) -> dict:
    """Burn the Android subset of ``update.img`` through Fastbootd."""
    extract_dir, steps, skipped = await _extract_update_image(
        ssh,
        suite_dir=suite_dir,
        remote_tool=remote_tool,
        remote_firmware=remote_firmware,
        on_log=on_log,
        on_progress=on_progress,
    )
    for item in skipped:
        if on_log:
            await on_log(f"跳过 {item}")

    image_sizes: dict[str, int] = {}
    for step in steps:
        if step.image not in image_sizes:
            image_sizes[step.image] = await _remote_image_expanded_size(
                ssh,
                os.path.join(extract_dir, step.image),
                step.entry_type,
                step.packed_size,
            )
    total_bytes = sum(image_sizes[step.image] for step in steps)
    completed_bytes = 0
    results: list[dict] = []

    for device in devices:
        if on_log:
            await on_log(f"设备 {device}：进入Fastbootd并校验分区")

        def runner(argv: list[str], timeout: int) -> CommandResult:
            return _remote_runner(ssh, argv, timeout)

        try:
            await asyncio.to_thread(
                FastbootPreparer(
                    runner,
                    on_transport_reset=on_transport_reset,
                ).prepare_gsi_fastbootd,
                device,
            )
            device_plan: list[tuple[FastbootWriteStep, int, int]] = []
            device_skipped: list[str] = []
            for step in steps:
                result = await asyncio.to_thread(
                    runner,
                    ["fastboot", "-s", device, "getvar", f"partition-size:{step.partition}"],
                    15,
                )
                partition_size = parse_fastboot_partition_size(result.output)
                if partition_size is None:
                    if is_required_fastboot_partition(step.partition):
                        raise FastbootFirmwareError(
                            f"设备 {device} 的Fastboot未暴露核心分区 "
                            f"{step.partition}；为避免不完整固件，本次未开始写入"
                        )
                    skipped_reason = (
                        f"{step.partition}（设备Fastbootd未暴露，保留设备现有内容）"
                    )
                    device_skipped.append(skipped_reason)
                    if on_log:
                        await on_log(f"设备 {device}：跳过 {skipped_reason}")
                    continue
                expanded_size = image_sizes[step.image]
                if expanded_size > partition_size:
                    raise FastbootFirmwareError(
                        f"分区 {step.partition} 容量不足：镜像展开后 "
                        f"{expanded_size} 字节，设备分区 {partition_size} 字节；"
                        "本次未开始写入"
                    )
                device_plan.append((step, expanded_size, partition_size))

            for step, expanded_size, _partition_size in device_plan:
                image_path = os.path.join(extract_dir, step.image)
                if on_log:
                    sparse = "（Android sparse，由Fastboot展开）" if step.entry_type == "sparse image" else ""
                    await on_log(
                        f"设备 {device}：烧写 {step.partition} <- {step.image}{sparse}"
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

            reboot = await asyncio.to_thread(
                runner, ["fastboot", "-s", device, "reboot"], 30,
            )
            if on_transport_reset:
                on_transport_reset(device, "adb")
            if reboot.code != 0:
                raise FastbootFirmwareError(
                    f"设备 {device} 分区已写入，但Fastboot重启失败: "
                    + (reboot.output or f"exit {reboot.code}")[-240:]
                )
            results.append({
                "device": device,
                "success": True,
                "partitions": [step.partition for step, _size, _capacity in device_plan],
                "skipped_partitions": device_skipped,
            })
        except Exception as exc:
            if isinstance(exc, FastbootFirmwareError):
                raise
            raise FastbootFirmwareError(
                f"设备 {device} Fastboot固件准备失败: {exc}"
            ) from exc

    if on_progress:
        await on_progress(100.0)
    return {
        "backend": "usbip-fastboot",
        "results": results,
        "skipped": skipped,
    }
