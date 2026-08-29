"""Loader 会话内的按分区名分区烧写。

``upgrade_tool uf`` 在 Download Boot 后会把设备重置进 MaskROM，并要求
USB 来源主机在工具数秒的等待窗口内完成 Loader→MaskROM 的二次枚举。
usbipd-win 导出的物理端口在该窗口内实测反复出现 ``0000:0002`` 描述符
失败（Windows Problem Code 43），且在烧写独占保护生效后依旧复现，
说明该窗口在 USB/IP 链路上不可依赖——这是服务端（Windows）USB 枚举
层故障，发生在任何 USB/IP 导出动作之前，客户端侧重试无法修复。

本模块优先在当前 Loader 会话内完成全部写入::

    SFI  解析镜像头（分区→文件映射、文件大小、Loader 时间）
    EXF  解包镜像（带缓存；产出 parameter.txt 与分区镜像）
    RID  探测当前 RockUSB 会话是否已经具备存储访问能力；可访问时跳过 DB
    DB   仅在 RID 明确失败（MaskROM/最小下载会话）时下载 DRAM Loader。
         DB 本身也会触发 USB 重新枚举，因此 USB/IP 专属 watcher 在 DB
         命令启动前布防，并以烧写前 PnP/VID 基线和
         ``upgrade_tool ld`` 作为传输恢复的最终判据
    RL   读回设备 GPT，与 parameter.txt 比对布局，不一致即拒绝
    UL   -noreset 重写 idblock Loader（不触发设备复位）
    DI   逐分区按分区名写入（双槽 _a/_b 一并覆盖）。DI 原生解析
         Android sparse 镜像并校验展开后容量，super.img 无需本地展开
    RD   写完后复位重启，交由既有 ADB 重连机制恢复

绝大多数 ADB→Loader 后已可访问存储的设备不再承受 DB 二次复位；只有
确实需要初始化 DRAM Loader 的设备才进入受控重挂流程。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import re
import shlex
import struct
import time
from dataclasses import dataclass

from . import runtime
from .usbip_transport import (
    ROCKUSB_LOADER_COUNT_RE,
)
from .usbip_transport import (
    capture_rockusb_route_baseline as _capture_route_baseline,
)
from .usbip_transport import (
    reattach_usbip_after_rockusb_reset as _reattach_routes,
)
from .usbip_transport import wait_for_rockusb_loaders as _wait_for_loaders


logger = logging.getLogger(__name__)

SECTOR_BYTES = 512
GPT_DUMP_SECTORS = 33
# USB2 链路下大分区（super ~4.5GB）按不低于 ~1.5MB/s 预留写入时间。
MIN_COMMAND_SECONDS = 300
BYTES_PER_SECOND_FLOOR = 1.5 * 1024 * 1024

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_PERCENT_RE = re.compile(r"\((\d{1,3})%\)")
LOADER_TIME_RE = re.compile(r"Loader\s+Time:([^\n\r]+)")
DB_TRANSITION_MARKERS = (
    "Download Boot Start",
    "Download Boot Success",
    "Wait For Maskrom Start",
)


class PartitionBurnError(Exception):
    """分区烧写无法继续；message 面向用户，status_code 用于响应。"""

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class PartitionEntry:
    name: str
    offset_sec: int
    size_sec: int
    grow: bool = False


@dataclass(frozen=True)
class SfiEntry:
    file: str
    partition: str
    entry_type: str
    size: int


@dataclass(frozen=True)
class WriteStep:
    partition: str
    image: str
    offset_sec: int
    size_sec: int
    size_bytes: int
    sparse: bool = False


@dataclass(frozen=True)
class GptEntry:
    name: str
    first_lba: int
    last_lba: int


# Android sparse 镜像头（magic 0xED26FF3A，little-endian）。
ANDROID_SPARSE_MAGIC = 0xED26FF3A


def parse_android_sparse_expanded_size(header: bytes) -> int | None:
    """Return expanded bytes for an Android sparse header, or ``None``."""
    if len(header) < 28:
        return None
    try:
        magic, major, _minor, file_hdr_sz, chunk_hdr_sz, block_size, total_blocks, _chunks, _crc = struct.unpack(
            "<IHHHHIIII", header[:28]
        )
    except struct.error:
        return None
    if (
        magic != ANDROID_SPARSE_MAGIC
        or major != 1
        or file_hdr_sz < 28
        or chunk_hdr_sz < 12
        or block_size <= 0
        or total_blocks <= 0
    ):
        return None
    return block_size * total_blocks


def strip_ansi_codes(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text or "")


def parse_parameter_partitions(text: str) -> list[PartitionEntry]:
    """解析 parameter.txt CMDLINE 里的 ``0x大小@0x偏移(名字)`` 分区表。"""
    cmdline = ""
    for line in (text or "").splitlines():
        if line.startswith("CMDLINE:"):
            cmdline = line[len("CMDLINE:"):]
            break
    _, _, parts = cmdline.partition("mtdparts=")
    # 剥掉 mtdparts 的设备名前缀（rk29xxnand:）；修饰符冒号在其后，不受影响。
    if ":" in parts:
        parts = parts.split(":", 1)[1]
    entries: list[PartitionEntry] = []
    for token in parts.split(","):
        token = token.strip()
        if not token:
            continue
        size_text, sep, rest = token.partition("@")
        if not sep:
            continue
        offset_text, _, name_field = rest.partition("(")
        name_field = name_field.strip("()").strip()
        if not name_field:
            continue
        grow = False
        if ":" in name_field:
            name_field, _, modifier = name_field.partition(":")
            grow = modifier.strip() == "grow"
        with contextlib.suppress(ValueError):
            offset = int(offset_text, 0)
            size = 0 if size_text.strip() == "-" else int(size_text, 0)
            entries.append(PartitionEntry(
                name=name_field.strip(),
                offset_sec=offset,
                size_sec=size,
                grow=grow,
            ))
    return entries


def parse_sfi_entries(output: str) -> list[SfiEntry]:
    """解析 ``SFI`` 镜像头列表的 ``key=value;`` 条目行。"""
    entries: list[SfiEntry] = []
    for line in (output or "").splitlines():
        if "file=" not in line:
            continue
        fields: dict[str, str] = {}
        for part in line.split(";"):
            key, sep, value = part.partition("=")
            if sep:
                fields[key.strip()] = value.strip()
        file_name = fields.get("file") or ""
        if not file_name:
            continue
        with contextlib.suppress(ValueError):
            size = int(fields.get("size") or "0", 0)
            entries.append(SfiEntry(
                file=file_name,
                partition=fields.get("partition") or "",
                entry_type=fields.get("type") or "",
                size=size,
            ))
    return entries


def parse_loader_time(output: str) -> str:
    match = LOADER_TIME_RE.search(strip_ansi_codes(output or ""))
    return (match.group(1).strip() if match else "")


def build_write_steps(
    sfi_entries: list[SfiEntry],
    partitions: list[PartitionEntry],
) -> tuple[list[WriteStep], list[str]]:
    """由镜像头条目与 parameter 分区表生成 DI 按分区名写入计划。

    返回 (计划, 跳过说明)。parameter/loader 等非分区条目与无映射分区
    （backup/frp/cache/userdata 等）不写入，逐条记录在跳过说明里。DI 按
    分区名写入并原生解析 Android sparse 容器，因此 ``image`` 与
    ``sparse image`` 两种条目都进入计划；sparse 条目的展开容量稍后按
    镜像头预检，不能只看打包大小。
    """
    by_name = {entry.name: entry for entry in partitions}
    steps: list[WriteStep] = []
    skipped: list[str] = []
    for entry in sfi_entries:
        if not entry.partition or entry.entry_type == "parameter":
            skipped.append(
                f"{entry.file}(非分区条目: {entry.entry_type or 'metadata'})"
            )
            continue
        partition = by_name.get(entry.partition)
        if partition is None:
            raise PartitionBurnError(
                f"固件声明的分区 {entry.partition} 不在 parameter.txt 分区表中，"
                "镜像与设备布局不匹配，已拒绝写入。"
            )
        entry_type = " ".join((entry.entry_type or "").lower().split())
        sparse = entry_type == "sparse image"
        if entry_type not in {"image", "sparse image"}:
            raise PartitionBurnError(
                f"分区 {entry.partition} 的 {entry.file} 类型为"
                f" {entry.entry_type or '未知'}，DI 不支持该条目类型，"
                "已在任何设备写入前停止。"
            )
        size_bytes = max(0, entry.size)
        size_sec = (size_bytes + SECTOR_BYTES - 1) // SECTOR_BYTES
        if size_sec <= 0:
            raise PartitionBurnError(
                f"分区 {entry.partition} 的镜像 {entry.file} 大小无效，已拒绝写入。"
            )
        if (
            not sparse
            and not partition.grow
            and size_sec > partition.size_sec
        ):
            raise PartitionBurnError(
                f"分区 {entry.partition} 的镜像 {entry.file} 需要 {size_sec} 个"
                f"扇区，超过 parameter.txt 声明容量 {partition.size_sec}，"
                "为避免覆盖后续分区已拒绝写入。"
            )
        steps.append(WriteStep(
            partition=entry.partition,
            image=entry.file,
            offset_sec=partition.offset_sec,
            size_sec=size_sec,
            size_bytes=size_bytes,
            sparse=sparse,
        ))
    if not steps:
        raise PartitionBurnError("固件镜像中未找到可写入的分区条目")
    return steps, skipped


def parse_gpt_entries(data: bytes) -> list[GptEntry]:
    """解析 GPT 头（LBA1）与分区表项（默认 LBA2 起）。"""
    header = bytes(data[:SECTOR_BYTES])
    if header[:8] != b"EFI PART":
        raise PartitionBurnError("设备 LBA1 处未找到 GPT 头（非 GPT 布局）")
    entry_lba = struct.unpack_from("<Q", header, 72)[0]
    num_entries = struct.unpack_from("<I", header, 80)[0]
    entry_size = struct.unpack_from("<I", header, 84)[0]
    entries: list[GptEntry] = []
    for index in range(num_entries):
        start = (entry_lba - 1) * SECTOR_BYTES + index * entry_size
        raw = bytes(data[start:start + entry_size])
        if len(raw) < entry_size or not any(raw[:16]):
            continue
        first_lba = struct.unpack_from("<Q", raw, 32)[0]
        last_lba = struct.unpack_from("<Q", raw, 40)[0]
        name = raw[56:120].decode("utf-16-le", errors="ignore").rstrip("\x00")
        if name:
            entries.append(GptEntry(name=name, first_lba=first_lba, last_lba=last_lba))
    return entries


def tables_match(gpt: list[GptEntry], partitions: list[PartitionEntry]) -> bool:
    """parameter 固定分区必须与 GPT 同名、同起止 LBA。"""
    by_name = {entry.name: entry for entry in gpt}
    for partition in partitions:
        gpt_entry = by_name.get(partition.name)
        if gpt_entry is None or gpt_entry.first_lba != partition.offset_sec:
            return False
        if partition.grow:
            continue
        if partition.size_sec <= 0:
            return False
        expected_last = partition.offset_sec + partition.size_sec - 1
        if gpt_entry.last_lba != expected_last:
            return False
    return True


def write_steps_fit_gpt(
    steps: list[WriteStep],
    gpt: list[GptEntry],
    expanded_sizes: dict[str, int] | None = None,
) -> bool:
    """Every write must fit inside the corresponding on-device GPT entry.

    Sparse steps are measured by their expanded size (from the image
    header preflight); an unknown expanded size fails closed.
    """
    expanded_sizes = expanded_sizes or {}
    by_name = {entry.name: entry for entry in gpt}
    for step in steps:
        entry = by_name.get(step.partition)
        if entry is None or step.offset_sec != entry.first_lba:
            return False
        size_sec = step.size_sec
        if step.sparse:
            expanded = expanded_sizes.get(step.image)
            if expanded is None:
                return False
            size_sec = (expanded + SECTOR_BYTES - 1) // SECTOR_BYTES
        if size_sec <= 0:
            return False
        if step.offset_sec + size_sec - 1 > entry.last_lba:
            return False
    return True


def _command_timeout(size_bytes: int) -> int:
    return max(MIN_COMMAND_SECONDS, int(size_bytes / BYTES_PER_SECOND_FLOOR))


async def _stream_tool_command(
    ssh, command: str, *, timeout: float, on_chunk=None,
) -> tuple[str, int]:
    """流式执行工具命令，返回 (完整输出, 退出码)；超时主动断开通道。"""
    _stdin, stdout, _stderr = await asyncio.to_thread(
        lambda: ssh.exec_command(command, get_pty=True, timeout=int(timeout))
    )
    started = time.monotonic()
    buffer: list[str] = []
    channel = stdout.channel
    while True:
        if channel.recv_ready():
            chunk = (await asyncio.to_thread(channel.recv, 4096)).decode(
                "utf-8", errors="ignore",
            )
            buffer.append(chunk)
            if on_chunk:
                with contextlib.suppress(Exception):
                    await on_chunk(chunk)
        if channel.exit_status_ready() and not channel.recv_ready():
            break
        if time.monotonic() - started > timeout:
            with contextlib.suppress(Exception):
                channel.close()
            raise PartitionBurnError(
                f"烧写命令超时（{int(timeout)}s）: {command}", status_code=504,
            )
        await asyncio.sleep(0.1)
    while channel.recv_ready():
        chunk = (await asyncio.to_thread(channel.recv, 4096)).decode(
            "utf-8", errors="ignore",
        )
        buffer.append(chunk)
        if on_chunk:
            with contextlib.suppress(Exception):
                await on_chunk(chunk)
    return "".join(buffer), channel.recv_exit_status()


def _last_percent(text: str) -> int | None:
    percents = _PERCENT_RE.findall(strip_ansi_codes(text))
    return int(percents[-1]) if percents else None


def _tool_failed(output: str) -> bool:
    lowered = strip_ansi_codes(output or "").lower()
    return any(
        marker in lowered
        for marker in ("fail", "error", "no found")
    )


def _extract_dir(suite_dir: str, firmware_name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(firmware_name))
    if stem.lower().endswith(".img"):
        stem = stem[:-4]
    return os.path.join(suite_dir, "fw_extract", stem)


async def _remote_sparse_expanded_sizes(
    ssh, extract_dir: str, steps: list[WriteStep],
) -> dict[str, int]:
    """Read each sparse image header remotely; return file→expanded bytes.

    Runs before any device write: DI expands sparse images on its own, but a
    partition overflow must abort the whole burn with zero writes, not fail
    halfway through.
    """
    sizes: dict[str, int] = {}
    for step in steps:
        if not step.sparse or step.image in sizes:
            continue
        image_path = os.path.join(extract_dir, step.image)
        od_output, od_err, od_code = await asyncio.to_thread(
            runtime.ssh_manager.execute_command,
            ssh,
            f"od -An -tx1 -N28 -- {shlex.quote(image_path)}",
            timeout=30,
        )
        if od_code != 0:
            raise PartitionBurnError(
                f"读取sparse镜像头失败: {step.image}: "
                + (od_err or od_output or f"exit {od_code}").strip()[-160:]
            )
        try:
            header = bytes.fromhex("".join((od_output or "").split()))
        except ValueError as exc:
            raise PartitionBurnError(
                f"sparse镜像头格式无效: {step.image}"
            ) from exc
        expanded = parse_android_sparse_expanded_size(header)
        if expanded is None:
            raise PartitionBurnError(
                f"{step.image} 被声明为sparse image，但未检测到有效"
                "Android sparse头；为避免错误写入已在任何设备写入前停止。"
            )
        sizes[step.image] = expanded
    return sizes


def _sfi_image_loader_time(sfi_output: str) -> str:
    return parse_loader_time(sfi_output)


async def run_partition_burn(
    ssh,
    *,
    suite_dir: str,
    remote_tool: str,
    remote_firmware: str,
    usbip_routes: list[dict] | None = None,
    on_log=None,
    on_progress=None,
    transport_probe: bool = False,
    force_usbip_bind: bool = False,
) -> dict:
    """在同一 Loader 会话内按 LBA 完成整包固件写入。

    ``usbip_routes`` 为设备的 USB/IP 路由（含来源主机与 BUSID）；提供时
    会在 DB 重枚举后用固件专属 watcher 重新挂载传输。``on_log`` 与
    ``on_progress`` 均为可选异步回调，用于 WebSocket 日志与进度上报。
    """
    firmware_name = os.path.basename(remote_firmware)
    extract_dir = _extract_dir(suite_dir, firmware_name)
    quoted_dir = shlex.quote(extract_dir)
    quoted_tool = shlex.quote(remote_tool)
    quoted_firmware = shlex.quote(remote_firmware)

    async def log(message: str) -> None:
        if on_log:
            with contextlib.suppress(Exception):
                await on_log(message)

    async def progress(percentage: float) -> None:
        if on_progress:
            with contextlib.suppress(Exception):
                await on_progress(percentage)

    async def run_quiet(command: str, timeout: float = 60) -> tuple[str, int]:
        stdout, stderr, code = await asyncio.to_thread(
            runtime.ssh_manager.execute_command, ssh, command,
            timeout=int(timeout),
        )
        text = (stdout or "")
        if stderr:
            text = (text + "\n" + stderr) if text else stderr
        return text, code

    # 1. 镜像头：分区→文件映射与 Loader 时间。
    sfi_output, sfi_code = await run_quiet(
        f"{quoted_tool} SFI {quoted_firmware}", timeout=120,
    )
    if sfi_code != 0:
        raise PartitionBurnError(f"解析固件信息失败（SFI 退出码 {sfi_code}）")
    sfi_entries = parse_sfi_entries(sfi_output)
    image_loader_time = _sfi_image_loader_time(sfi_output)
    if image_loader_time:
        await log(f"固件内 Loader: {image_loader_time}")

    # 2. 解包缓存命中判定：所有镜像头声明的文件（含 parameter.txt）
    #    必须存在且大小逐一相符——同一文件名上传不同版本固件时
    #    （staged-update.img），仅按名字缓存会复用上一版解包内容，
    #    把错误数据写进设备。
    expected_sizes: dict[str, int] = {}
    for entry in sfi_entries:
        expected_sizes.setdefault(entry.file, entry.size)
    needed = sorted(expected_sizes)
    stat_output, stat_code = await run_quiet(
        f"cd {quoted_dir} && stat -c '%n %s' "
        + " ".join(shlex.quote(name) for name in needed)
        + " 2>/dev/null",
        timeout=60,
    )
    actual_sizes: dict[str, int] = {}
    for line in (stat_output or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            actual_sizes[parts[0]] = int(parts[1])
    cache_hit = stat_code == 0 and all(
        actual_sizes.get(name) == expected_sizes[name]
        for name in needed
    )
    if not cache_hit:
        await log("解包固件分区镜像（EXF）...")
        await run_quiet(f"rm -rf {quoted_dir} && mkdir -p {quoted_dir}", timeout=120)

        async def on_extract_chunk(chunk: str) -> None:
            percent = _last_percent(chunk)
            if percent is not None:
                await progress(6.0 * percent / 100.0)

        extract_output, extract_code = await _stream_tool_command(
            ssh,
            f"cd {quoted_dir} && {quoted_tool} EXF {quoted_firmware} {quoted_dir}",
            timeout=_command_timeout(4 * 1024 * 1024 * 1024),
            on_chunk=on_extract_chunk,
        )
        if extract_code != 0 or "Extract ok" not in extract_output:
            raise PartitionBurnError(
                "固件解包失败（EXF）: " + strip_ansi_codes(extract_output)[-200:]
            )
    await progress(6.0)

    parameter_output, parameter_code = await run_quiet(
        f"cat {quoted_dir}/parameter.txt", timeout=30,
    )
    if parameter_code != 0:
        raise PartitionBurnError("读取 parameter.txt 失败")
    partitions = parse_parameter_partitions(parameter_output)
    if not partitions:
        raise PartitionBurnError("parameter.txt 中未解析到分区定义")
    steps: list[WriteStep] = []
    skipped: list[str] = []
    if transport_probe:
        await log("USB/IP DB 传输探测模式：不会执行 GPT/UL/DI 写入")
    else:
        steps, skipped = build_write_steps(sfi_entries, partitions)
        for item in skipped:
            await log(f"跳过 {item}")

    # 3. 多数设备经 `adb reboot loader` 后已经可以 RID/RL/WL。先探测
    #    存储能力，避免无条件 DB 引入已知不稳定的 USB 二次复位。
    await progress(6.5)
    rid_output, rid_code = await run_quiet(
        f"cd {shlex.quote(suite_dir)} && {quoted_tool} RID", timeout=60,
    )
    storage_ready = bool(strip_ansi_codes(rid_output).strip()) and (
        rid_code == 0 and not _tool_failed(rid_output)
    )
    if storage_ready:
        await log("当前 Loader 已可访问存储，跳过 DB 二次复位")
    else:
        if usbip_routes and not force_usbip_bind:
            raise PartitionBurnError(
                "当前 USB/IP Loader 的 RID 失败，必须执行 DB 才能访问存储；"
                "但普通 usbipd Shared/attach 会在 DB 重枚举窗口切换 Windows "
                "USB 驱动，实机已重复触发 0000:0002 / Code 43。为避免再次锁死"
                "物理端口，本次在 DB 前安全停止。请改用 Linux/本地 USB 直连，"
                "或仅在目标 RockUSB PnP 实例已预绑定为 Shared (forced) 后运行"
                "管理员 transport-probe-force 验证；未通过探测前禁止分区写入。"
            )
        await log("当前会话不可访问存储，准备下载 DRAM Loader（DB）")
        db_baseline = {}
        if usbip_routes:
            db_baseline, baseline_error = await _capture_route_baseline(
                usbip_routes,
            )
            if baseline_error:
                raise PartitionBurnError(
                    f"无法在 DB 前锁定 Loader USB 实例: {baseline_error}"
                )

        db_transition_event = asyncio.Event()
        db_watcher_ready = asyncio.Event()
        db_reattach_task = None
        if usbip_routes:
            db_reattach_task = asyncio.create_task(
                _reattach_routes(
                    ssh,
                    usbip_routes,
                    # 复位后新实例约 2 秒出现 + 2 次稳定采样 + PnP 落定
                    # 等待；DRAM Loader 空闲窗口约 45 秒，30 秒预算内完成
                    # attach 仍有充足裕量。
                    timeout=30,
                    baseline=db_baseline,
                    transition_event=db_transition_event,
                    ready_event=db_watcher_ready,
                    require_forced=force_usbip_bind,
                )
            )
            try:
                await asyncio.wait_for(db_watcher_ready.wait(), timeout=15)
            except TimeoutError as exc:
                db_reattach_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await db_reattach_task
                raise PartitionBurnError(
                    "DB 前 USB/IP watcher 未能完成 Windows SSH 布防",
                    status_code=504,
                ) from exc

        db_phase_text = ""

        async def on_db_chunk(chunk: str) -> None:
            nonlocal db_phase_text
            db_phase_text = (db_phase_text + strip_ansi_codes(chunk))[-512:]
            # 部分 upgrade_tool 版本在 DB 下载完成触发 USB reset 后，通道
            # 会随旧 USB 实例消失而直接退出，只留下 Download Boot Start，
            # 不会再打印 Success。watcher 已由 PnP 基线保护，因此从 Start
            # 解锁不会误挂旧实例，且能覆盖这个真实的重枚举窗口。
            if any(marker in db_phase_text for marker in DB_TRANSITION_MARKERS):
                db_transition_event.set()

        # 不能依赖 DB 文本作为唯一阶段信号：部分 upgrade_tool 在旧 USB
        # 通道随 reset 消失时完全不回显 Start/Success。watcher 已有
        # 烧写前 PnP/VID 基线保护，因此在启动 DB 前直接解除等待即可；
        # 只有检测到真实身份变化并连续两次稳定后才会 attach。
        db_transition_event.set()
        await log("下载 DRAM Loader（DB）...")
        try:
            db_output, db_code = await _stream_tool_command(
                ssh,
                f"cd {quoted_dir} && {quoted_tool} DB MiniLoaderAll.bin",
                timeout=300,
                on_chunk=on_db_chunk,
            )
        except BaseException:
            if db_reattach_task is not None:
                db_reattach_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await db_reattach_task
            raise

        clean_db_output = strip_ansi_codes(db_output)
        if any(marker in clean_db_output for marker in DB_TRANSITION_MARKERS):
            db_transition_event.set()
        if db_code != 0 or _tool_failed(db_output):
            if db_reattach_task is not None:
                db_reattach_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await db_reattach_task
            raise PartitionBurnError(
                "下载 DRAM Loader 失败（DB）: " + clean_db_output[-200:]
            )
        db_reattach_result = {"success": not usbip_routes, "errors": {}}
        if db_reattach_task is not None:
            try:
                db_reattach_result = await db_reattach_task
            except Exception as exc:
                db_reattach_result = {
                    "success": False,
                    "errors": {"watcher": str(exc)},
                }

        # Attached 只是 USB/IP 传输层判断；Rockchip 工具能否看见设备才是
        # 后续命令可执行及自动重试的权威判据。
        loader_ready, loader_detail = await _wait_for_loaders(
            ssh,
            f"cd {shlex.quote(suite_dir)} && {quoted_tool} ld",
            1,
            timeout=15,
            interval=1,
        )
        if not loader_ready:
            reattach_errors = db_reattach_result.get("errors") or {}
            reattach_detail = "; ".join(
                f"{key}: {str(value).strip()[:160]}"
                for key, value in reattach_errors.items()
            )
            raise PartitionBurnError(
                "DB 后 RockUSB 未恢复（upgrade_tool ld 未发现设备"
                + (f"；{reattach_detail[:300]}" if reattach_detail else "")
                + "）。若 Windows 为 0000:0002，当前 bind/attach 无法修复，"
                "请先给设备断电恢复；并优先对 Shared (forced) 做 A/B 验证。"
                + (f" ld: {loader_detail[-120:]}" if loader_detail else "")
            )

        rid_output, rid_code = await run_quiet(
            f"cd {shlex.quote(suite_dir)} && {quoted_tool} RID", timeout=60,
        )
        if rid_code != 0 or _tool_failed(rid_output):
            raise PartitionBurnError(
                "DB 后存储仍不可访问（RID: "
                + strip_ansi_codes(rid_output)[-120:].strip()
                + "）。设备可能已离开 Loader 会话，请断电重上电后重试。"
            )
        await log("DRAM Loader 已就绪，存储访问正常")

    if transport_probe:
        await log("DB 传输探测成功；未写入任何分区，正在复位设备")
        await run_quiet(
            f"cd {shlex.quote(suite_dir)} && {quoted_tool} RD", timeout=60,
        )
        await progress(100.0)
        return {
            "transport_probe": True,
            "db_required": not storage_ready,
            "written": [],
            "skipped": [],
            "loader_time": image_loader_time,
            "total_bytes": 0,
        }

    # 4. 读回 GPT 并核对布局，防止盲写错位。
    await progress(7.0)
    rl_output, rl_code = await run_quiet(
        f"cd {shlex.quote(suite_dir)} && {quoted_tool} RL 1 "
        f"{GPT_DUMP_SECTORS} {quoted_dir}/gpt.dump",
        timeout=120,
    )
    gpt_b64, b64_code = await run_quiet(
        f"base64 -w0 {quoted_dir}/gpt.dump", timeout=60,
    )
    if rl_code != 0 or b64_code != 0:
        raise PartitionBurnError(
            f"读取设备 GPT 分区表失败（RL 退出码 {rl_code}，"
            f"base64 退出码 {b64_code}）: "
            + strip_ansi_codes(rl_output)[-160:].strip()
        )
    try:
        gpt = parse_gpt_entries(base64.b64decode(gpt_b64 or ""))
    except (ValueError, struct.error) as exc:
        raise PartitionBurnError(f"解析设备 GPT 失败: {exc}") from exc
    # sparse 条目按镜像头展开尺寸预检：DI 虽原生处理 sparse，但容量越界
    # 必须在任何写入前终止，而不是烧到一半失败。
    expanded_sizes: dict[str, int] = {}
    sparse_steps = [step for step in steps if step.sparse]
    if sparse_steps:
        expanded_sizes = await _remote_sparse_expanded_sizes(
            ssh, extract_dir, sparse_steps,
        )
        partition_by_name = {entry.name: entry for entry in partitions}
        for step in sparse_steps:
            partition = partition_by_name[step.partition]
            expanded_sec = (
                expanded_sizes[step.image] + SECTOR_BYTES - 1
            ) // SECTOR_BYTES
            if not partition.grow and expanded_sec > partition.size_sec:
                raise PartitionBurnError(
                    f"分区 {step.partition} 的sparse镜像 {step.image} 展开后需 "
                    f"{expanded_sec} 个扇区，超过 parameter.txt 声明容量 "
                    f"{partition.size_sec}，已拒绝写入。"
                )
    if not tables_match(gpt, partitions) or not write_steps_fit_gpt(
        steps, gpt, expanded_sizes,
    ):
        device_layout = ", ".join(
            f"{entry.name}@{entry.first_lba}-{entry.last_lba}"
            for entry in gpt[:8]
        )
        raise PartitionBurnError(
            "设备分区表与固件 parameter.txt 的起止 LBA 不一致，或镜像超过"
            "设备 GPT 分区容量；为避免错位/越界写入已拒绝。"
            f" 设备 GPT 前 8 项: {device_layout or '空'}。"
            "请先在 USB 直连/本地方式下用 uf 刷新一次分区表，或改用布局匹配的固件。"
        )
    await log("设备分区表与固件布局一致")

    # 5. Loader：始终用镜像内 Loader 重写 idblock（幂等），-noreset
    #    保持当前 USB 会话；随后以 ld 确认传输仍在。
    await progress(9.0)
    await log("更新 Loader（UL -noreset，不触发设备复位）...")
    ul_output, ul_code = await _stream_tool_command(
        ssh,
        f"cd {quoted_dir} && {quoted_tool} UL MiniLoaderAll.bin -noreset",
        timeout=300,
    )
    if ul_code != 0 or _tool_failed(ul_output):
        raise PartitionBurnError(
            "写入 Loader 失败（UL -noreset）: "
            + strip_ansi_codes(ul_output)[-200:]
        )
    ld_output, ld_code = await run_quiet(
        f"cd {shlex.quote(suite_dir)} && {quoted_tool} ld", timeout=60,
    )
    loader_count = 0
    if ld_code == 0:
        match = ROCKUSB_LOADER_COUNT_RE.search(ld_output or "")
        if match:
            loader_count = int(match.group(1))
    if loader_count < 1:
        raise PartitionBurnError(
            "写入 Loader 后设备不再应答（ld 未发现 RockUSB）。UL -noreset 在该"
            "固件/设备组合上可能仍触发了复位，请断电重上电后改用 uf 或本地烧写。"
        )
    await log("Loader 已更新，RockUSB 会话正常")

    # 6. 逐分区 DI（按分区名写入，原生展开 sparse）；按字节数加权推进进度。
    def step_bytes(step: WriteStep) -> int:
        return (
            expanded_sizes.get(step.image, step.size_bytes)
            if step.sparse else step.size_bytes
        )

    total_bytes = sum(step_bytes(step) for step in steps) or 1
    written_bytes = 0
    await log(
        f"开始写入 {len(steps)} 个分区，共 {total_bytes / (1024 ** 3):.2f} GiB"
    )
    for index, step in enumerate(steps, start=1):
        base = 10.0 + 88.0 * written_bytes / total_bytes
        span = 88.0 * step_bytes(step) / total_bytes
        sparse_note = (
            "，Android sparse 由 DI 原生展开" if step.sparse else ""
        )
        await log(
            f"[{index}/{len(steps)}] 写入 {step.partition} "
            f"<- {step.image}"
            f"（{step_bytes(step) / (1024 ** 2):.1f} MiB{sparse_note}）"
        )

        async def on_write_chunk(chunk: str, *, _base=base, _span=span) -> None:
            percent = _last_percent(chunk)
            if percent is not None:
                await progress(_base + _span * percent / 100.0)

        di_output, di_code = await _stream_tool_command(
            ssh,
            f"cd {quoted_dir} && {quoted_tool} DI "
            f"{shlex.quote('-' + step.partition)} {shlex.quote(step.image)}",
            timeout=_command_timeout(step_bytes(step)),
            on_chunk=on_write_chunk,
        )
        if di_code != 0 or "ERROR" in (di_output or "").upper():
            raise PartitionBurnError(
                f"写入分区 {step.partition} 失败（DI）: "
                + strip_ansi_codes(di_output)[-200:]
            )
        written_bytes += step_bytes(step)
        await progress(10.0 + 88.0 * written_bytes / total_bytes)

    # 7. 复位重启；该 Loader→ADB 切换由既有重连机制恢复。
    await log("全部分区写入完成，复位设备...")
    await run_quiet(
        f"cd {shlex.quote(suite_dir)} && {quoted_tool} RD", timeout=60,
    )
    await progress(100.0)
    return {
        "written": [
            {"partition": step.partition, "image": step.image}
            for step in steps
        ],
        "skipped": skipped,
        "loader_time": image_loader_time,
        "total_bytes": total_bytes,
    }
