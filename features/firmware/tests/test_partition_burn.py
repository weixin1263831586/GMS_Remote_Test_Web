"""分区烧写（Loader 会话内 DI 按分区名写入）的单元测试。"""

import asyncio
import base64
import struct
import unittest
from unittest.mock import patch

from features.firmware import partition_burn, runtime


REAL_PARAMETER_TXT = """FIRMWARE_VER: 14.0
MACHINE_MODEL: rk3572_a16
MACHINE_ID: 007
MANUFACTURER: rockchip
MAGIC: 0x5041524B
ATAG: 0x00200800
MACHINE: rk3572_a16
CHECK_MASK: 0x80
PWR_HLD: 0,0,A,0,1
TYPE: GPT
CMDLINE:mtdparts=rk29xxnand:0x00002000@0x00002000(security),0x00004000@0x00004000(uboot_a),0x00004000@0x00008000(uboot_b),0x00002000@0x0000c000(misc),0x00008000@0x0000e000(resource_a),0x00008000@0x00016000(resource_b),0x00014000@0x0001e000(vendor_boot_a),0x00014000@0x00032000(vendor_boot_b),0x00004000@0x00046000(init_boot_a),0x00004000@0x0004a000(init_boot_b),0x00002000@0x0004e000(dtbo_a),0x00002000@0x00050000(dtbo_b),0x00000800@0x00052000(vbmeta_a),0x00000800@0x00052800(vbmeta_b),0x00020000@0x00053000(boot_a),0x00020000@0x00073000(boot_b),0x000c0000@0x00093000(backup),0x000c0000@0x00153000(cache),0x00020000@0x00213000(metadata),0x00000400@0x00233000(frp),0x00000800@0x00233400(baseparameter),0x00af0000@0x00233c00(super),-@0x00d23c00(userdata:grow)
"""

REAL_SFI_OUTPUT = """File:update.img
Type:Update Firmware
Chip Tag:351A\tVersion:e.0.00\tBuild Time:2026-08-24 23:22:50\tSign:false
Loader ver:1.00\tLoader Time:2026-08-24 21:30:30
Loader Sign:false
Entry Count:20
    EntryNo=0 ;    file=package-file;    offset=0xd6a98;    size=0x3a8
    EntryNo=1 ;    file=MiniLoaderAll.bin;    offset=0xd7298;    size=0xd5a32
    EntryNo=2 ;    file=parameter.txt;    partition=parameter;    type=parameter;    offset=0x1ad298;    size=0x3bb
    EntryNo=3 ;    file=uboot.img;    partition=uboot_a;    type=image;    offset=0x1ada98;    size=0x600000
    EntryNo=4 ;    file=uboot.img;    partition=uboot_b;    type=image;    offset=0x1ada98;    size=0x600000
    EntryNo=5 ;    file=misc.img;    partition=misc;    type=image;    offset=0x7ada98;    size=0xc000
    EntryNo=6 ;    file=super.img;    partition=super;    type=image;    offset=0xde2a98;    size=0x10b000000
"""


# 与 REAL_SFI_OUTPUT 声明的文件大小一一对应（缓存命中样本）。
CACHE_HIT_STAT_OUTPUT = (
    "MiniLoaderAll.bin 875058\n"
    "misc.img 49152\n"
    "package-file 936\n"
    "parameter.txt 955\n"
    "super.img 4479516672\n"
    "uboot.img 6291456\n"
)

# super 以 Android sparse 条目交付的镜像头样本。
SPARSE_SFI_OUTPUT = """File:update.img
Type:Update Firmware
Chip Tag:351A\tVersion:e.0.00\tBuild Time:2026-08-24 23:22:50\tSign:false
Loader ver:1.00\tLoader Time:2026-08-24 21:30:30
Loader Sign:false
Entry Count:3
    EntryNo=0 ;    file=MiniLoaderAll.bin;    offset=0xd7298;    size=0xd5a32
    EntryNo=1 ;    file=parameter.txt;    partition=parameter;    type=parameter;    offset=0x1ad298;    size=0x3bb
    EntryNo=2 ;    file=super.img;    partition=super;    type=sparse image;    offset=0x1ada98;    size=0x100000
"""

SPARSE_CACHE_HIT_STAT_OUTPUT = (
    "MiniLoaderAll.bin 875058\n"
    "parameter.txt 955\n"
    "super.img 1048576\n"
)


def sparse_header_hex(total_blocks: int) -> str:
    """od -An -tx1 -N28 风格的 Android sparse 头输出。"""
    header = struct.pack(
        "<IHHHHIIII",
        partition_burn.ANDROID_SPARSE_MAGIC, 1, 0, 28, 12,
        4096, total_blocks, 2, 0,
    )
    return header.hex(" ")


def build_gpt_dump(entries):
    """构造以 LBA1 起始的 GPT dump（33 扇区）。"""
    parameter_sizes = {
        entry.name: entry.size_sec
        for entry in partition_burn.parse_parameter_partitions(
            REAL_PARAMETER_TXT
        )
    }
    blob = bytearray(b"\x00" * 512 * 33)
    blob[0:8] = b"EFI PART"
    struct.pack_into("<Q", blob, 72, 2)           # partition_entry_lba
    struct.pack_into("<I", blob, 80, len(entries))
    struct.pack_into("<I", blob, 84, 128)         # entry_size
    for index, item in enumerate(entries):
        name, first_lba = item[:2]
        size_sec = (
            item[2] if len(item) > 2
            else parameter_sizes.get(name, 9)
        )
        base = 512 + index * 128
        blob[base:base + 16] = b"\x01" + b"\x00" * 15   # 非零类型 GUID
        struct.pack_into("<Q", blob, base + 32, first_lba)
        struct.pack_into(
            "<Q", blob, base + 40,
            first_lba + max(1, size_sec) - 1,
        )
        encoded = name.encode("utf-16-le")
        blob[base + 56:base + 56 + len(encoded)] = encoded
    return bytes(blob)


class ParserTests(unittest.TestCase):
    def test_parse_parameter_partitions(self):
        partitions = partition_burn.parse_parameter_partitions(
            REAL_PARAMETER_TXT
        )
        by_name = {entry.name: entry for entry in partitions}
        self.assertEqual(by_name["uboot_a"].offset_sec, 0x4000)
        self.assertEqual(by_name["uboot_a"].size_sec, 0x4000)
        self.assertEqual(by_name["super"].offset_sec, 0x233C00)
        self.assertTrue(by_name["userdata"].grow)
        self.assertEqual(by_name["userdata"].size_sec, 0)
        self.assertEqual(len(partitions), 23)

    def test_parse_sfi_entries(self):
        entries = partition_burn.parse_sfi_entries(REAL_SFI_OUTPUT)
        by_partition = {e.partition: e for e in entries if e.partition}
        self.assertEqual(by_partition["uboot_a"].file, "uboot.img")
        self.assertEqual(by_partition["uboot_a"].size, 0x600000)
        self.assertEqual(by_partition["super"].size, 0x10B000000)
        no_partition = [e for e in entries if not e.partition]
        self.assertEqual(
            {e.file for e in no_partition}, {"package-file", "MiniLoaderAll.bin"}
        )

    def test_parse_loader_time(self):
        self.assertEqual(
            partition_burn.parse_loader_time(REAL_SFI_OUTPUT),
            "2026-08-24 21:30:30",
        )
        self.assertEqual(partition_burn.parse_loader_time(""), "")

    def test_build_write_steps_maps_slots_and_skips_metadata(self):
        partitions = partition_burn.parse_parameter_partitions(
            REAL_PARAMETER_TXT
        )
        entries = partition_burn.parse_sfi_entries(REAL_SFI_OUTPUT)
        steps, skipped = partition_burn.build_write_steps(entries, partitions)
        self.assertEqual(
            [(step.partition, step.image) for step in steps],
            [
                ("uboot_a", "uboot.img"),
                ("uboot_b", "uboot.img"),
                ("misc", "misc.img"),
                ("super", "super.img"),
            ],
        )
        self.assertEqual(steps[0].offset_sec, 0x4000)
        self.assertEqual(steps[0].size_sec, 0x600000 // 512)
        # 0x10b000000 非 512 整数倍时向上取整。
        self.assertEqual(
            steps[-1].size_sec, (0x10B000000 + 511) // 512
        )
        skipped_text = " ".join(skipped)
        self.assertIn("package-file", skipped_text)
        self.assertIn("MiniLoaderAll.bin", skipped_text)
        self.assertIn("parameter.txt", skipped_text)

    def test_build_write_steps_rejects_unknown_partition(self):
        entries = [
            partition_burn.SfiEntry(
                file="x.img", partition="nonexistent", entry_type="image",
                size=512,
            )
        ]
        with self.assertRaises(partition_burn.PartitionBurnError):
            partition_burn.build_write_steps(entries, [])

    def test_build_write_steps_accepts_sparse_image(self):
        partitions = partition_burn.parse_parameter_partitions(
            REAL_PARAMETER_TXT
        )
        entries = [
            partition_burn.SfiEntry(
                file="super.img", partition="super",
                entry_type="sparse image", size=0x128B2B11C,
            )
        ]
        steps, skipped = partition_burn.build_write_steps(entries, partitions)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].partition, "super")
        self.assertTrue(steps[0].sparse)
        self.assertEqual(skipped, [])

    def test_build_write_steps_rejects_partition_overflow(self):
        partitions = [
            partition_burn.PartitionEntry(
                name="boot_a", offset_sec=0x1000, size_sec=8,
            )
        ]
        entries = [
            partition_burn.SfiEntry(
                file="boot.img", partition="boot_a",
                entry_type="image", size=9 * 512,
            )
        ]
        with self.assertRaises(partition_burn.PartitionBurnError) as ctx:
            partition_burn.build_write_steps(entries, partitions)
        self.assertIn("超过", str(ctx.exception))

    def test_gpt_parse_and_match(self):
        partitions = partition_burn.parse_parameter_partitions(
            REAL_PARAMETER_TXT
        )
        gpt_entries = [
            (entry.name, entry.offset_sec) for entry in partitions
        ]
        dump = build_gpt_dump(gpt_entries)
        parsed = partition_burn.parse_gpt_entries(dump)
        self.assertTrue(partition_burn.tables_match(parsed, partitions))

    def test_gpt_mismatch_detected(self):
        partitions = partition_burn.parse_parameter_partitions(
            REAL_PARAMETER_TXT
        )
        shifted = build_gpt_dump([("uboot_a", 0x5000), ("uboot_b", 0x8000)])
        parsed = partition_burn.parse_gpt_entries(shifted)
        self.assertFalse(partition_burn.tables_match(parsed, partitions))

    def test_gpt_size_mismatch_detected(self):
        partitions = partition_burn.parse_parameter_partitions(
            REAL_PARAMETER_TXT
        )
        wrong_size = build_gpt_dump([
            ("uboot_a", 0x4000, 0x3FFF),
        ])
        parsed = partition_burn.parse_gpt_entries(wrong_size)
        self.assertFalse(partition_burn.tables_match(parsed, partitions))

    def test_write_steps_must_fit_device_gpt(self):
        steps = [partition_burn.WriteStep(
            partition="boot_a", image="boot.img", offset_sec=0x1000,
            size_sec=9, size_bytes=9 * 512,
        )]
        gpt = [partition_burn.GptEntry(
            name="boot_a", first_lba=0x1000, last_lba=0x1007,
        )]
        self.assertFalse(partition_burn.write_steps_fit_gpt(steps, gpt))

    def test_sparse_header_expanded_size_parsing(self):
        header = struct.pack(
            "<IHHHHIIII",
            partition_burn.ANDROID_SPARSE_MAGIC, 1, 0, 28, 12,
            4096, 1000, 2, 0,
        )
        self.assertEqual(
            partition_burn.parse_android_sparse_expanded_size(header),
            4096000,
        )
        self.assertIsNone(
            partition_burn.parse_android_sparse_expanded_size(b"not sparse")
        )
        self.assertIsNone(
            partition_burn.parse_android_sparse_expanded_size(header[:20])
        )

    def test_sparse_fit_fails_closed_without_expanded_size(self):
        steps = [partition_burn.WriteStep(
            partition="super", image="super.img", offset_sec=0x233C00,
            size_sec=8, size_bytes=4096, sparse=True,
        )]
        gpt = [partition_burn.GptEntry(
            name="super", first_lba=0x233C00, last_lba=0xFFFFFF,
        )]
        # 未提供展开尺寸时必须拒绝，而不是按打包大小放行。
        self.assertFalse(partition_burn.write_steps_fit_gpt(steps, gpt))
        self.assertTrue(partition_burn.write_steps_fit_gpt(
            steps, gpt, {"super.img": 8 * 512},
        ))

    def test_gpt_parse_rejects_non_gpt(self):
        with self.assertRaises(partition_burn.PartitionBurnError):
            partition_burn.parse_gpt_entries(b"\x00" * 512)

    def test_last_percent_parses_ansi_progress(self):
        chunk = "\x1b[1A\x1b[2Kstart to extract super.img...(82%)\n"
        self.assertEqual(partition_burn._last_percent(chunk), 82)
        self.assertIsNone(partition_burn._last_percent("no percent here"))


class _FakeSshManager:
    def __init__(self, responses):
        self.responses = responses
        self.commands = []

    def execute_command(self, _ssh, cmd, timeout=None):
        self.commands.append(cmd)
        for needle, response in self.responses:
            if needle in cmd:
                if isinstance(response, list):
                    if len(response) > 1:
                        return response.pop(0)
                    return response[0]
                return response
        return "", "", 0


async def _fake_stream(outputs):
    """Return an async stand-in for _stream_tool_command keyed by command."""

    async def _stream(ssh, command, *, timeout, on_chunk=None):
        for needle, response in outputs:
            if needle in command:
                return response
        return "", 0

    return _stream


GPT_ENTRIES = [
    ("security", 0x2000), ("uboot_a", 0x4000), ("uboot_b", 0x8000),
    ("misc", 0xC000), ("resource_a", 0xE000), ("resource_b", 0x16000),
    ("vendor_boot_a", 0x1E000), ("vendor_boot_b", 0x32000),
    ("init_boot_a", 0x46000), ("init_boot_b", 0x4A000),
    ("dtbo_a", 0x4E000), ("dtbo_b", 0x50000), ("vbmeta_a", 0x52000),
    ("vbmeta_b", 0x52800), ("boot_a", 0x53000), ("boot_b", 0x73000),
    ("backup", 0x93000), ("cache", 0x153000), ("metadata", 0x213000),
    ("frp", 0x233000), ("baseparameter", 0x233400), ("super", 0x233C00),
    ("userdata", 0xD23C00),
]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.gpt_b64 = base64.b64encode(build_gpt_dump(GPT_ENTRIES)).decode()

    def _configure(self, responses):
        manager = _FakeSshManager(responses)
        runtime.configure_runtime(ssh_manager=manager)
        return manager

    def test_happy_path_runs_expected_command_sequence(self):
        responses = [
            ("SFI", (REAL_SFI_OUTPUT, "", 0)),
            (" RID", ("Read Flash ID OK\nFlash Info: EMMC", "", 0)),
            ("parameter.txt", (REAL_PARAMETER_TXT, "", 0)),
            ("base64 -w0", (self.gpt_b64, "", 0)),
            (" ld", ("List of rockusb connected(1)\nDevNo=1", "", 0)),
            (" RL 1 ", ("", "", 0)),
        ]
        manager = self._configure(responses)
        streamed: list[str] = []

        async def scenario():
            async def stream(ssh, command, *, timeout, on_chunk=None):
                streamed.append(command)
                if " EXF " in command:
                    return "Extract ok.", 0
                if " DB " in command:
                    return "Download Boot Start\nDownload Boot Success", 0
                if " UL " in command:
                    return "Upgrade loader ok", 0
                if " DI " in command:
                    return "Write image ok(100%)", 0
                return "", 0

            with patch.object(partition_burn, "_stream_tool_command", stream):
                return await partition_burn.run_partition_burn(
                    object(),
                    suite_dir="/suite",
                    remote_tool="/suite/.gms_upgrade_tool",
                    remote_firmware="/suite/update.img",
                )

        result = asyncio.run(scenario())
        self.assertEqual(len(result["written"]), 4)
        di_commands = [c for c in streamed if " DI " in c]
        self.assertEqual(len(di_commands), 4)
        self.assertIn("-uboot_a uboot.img", di_commands[0])
        self.assertTrue(
            any(" UL MiniLoaderAll.bin -noreset" in c for c in streamed)
        )
        # RID 已证明当前 Loader 可访问存储时，不能再用 DB 制造二次枚举。
        self.assertFalse(any(" DB " in c for c in streamed))
        self.assertTrue(any(" RID" in c for c in manager.commands))
        self.assertTrue(any(" RD" in c for c in manager.commands))

    def test_sparse_super_is_flashed_by_partition_name(self):
        # super 容量 0xaf0000 扇区 × 512 ≈ 5.4GiB；展开 4096000000 字节可容纳。
        responses = [
            ("SFI", (SPARSE_SFI_OUTPUT, "", 0)),
            ("stat -c", (SPARSE_CACHE_HIT_STAT_OUTPUT, "", 0)),
            (" RID", ("Read Flash ID OK", "", 0)),
            ("parameter.txt", (REAL_PARAMETER_TXT, "", 0)),
            ("base64 -w0", (self.gpt_b64, "", 0)),
            (" RL 1 ", ("", "", 0)),
            ("od -An", (sparse_header_hex(1_000_000), "", 0)),
            (" ld", ("List of rockusb connected(1)", "", 0)),
        ]
        manager = self._configure(responses)
        streamed: list[str] = []

        async def scenario():
            async def stream(ssh, command, *, timeout, on_chunk=None):
                streamed.append(command)
                if " UL " in command:
                    return "Upgrade loader ok", 0
                if " DI " in command:
                    return "Write image ok(100%)", 0
                return "", 0

            with patch.object(partition_burn, "_stream_tool_command", stream):
                return await partition_burn.run_partition_burn(
                    object(), suite_dir="/suite",
                    remote_tool="/suite/.gms_upgrade_tool",
                    remote_firmware="/suite/update.img",
                )

        result = asyncio.run(scenario())
        self.assertEqual(
            result["written"],
            [{"partition": "super", "image": "super.img"}],
        )
        di_commands = [c for c in streamed if " DI " in c]
        self.assertEqual(len(di_commands), 1)
        self.assertIn("DI -super super.img", di_commands[0])
        self.assertFalse(any(" WL " in c for c in streamed))
        self.assertTrue(any("od -An -tx1 -N28" in c for c in manager.commands))

    def test_sparse_expansion_overflow_aborts_before_any_write(self):
        # 展开 8192000000 字节 > super 分区容量，必须在 DI 前零写入终止。
        responses = [
            ("SFI", (SPARSE_SFI_OUTPUT, "", 0)),
            ("stat -c", (SPARSE_CACHE_HIT_STAT_OUTPUT, "", 0)),
            (" RID", ("Read Flash ID OK", "", 0)),
            ("parameter.txt", (REAL_PARAMETER_TXT, "", 0)),
            ("base64 -w0", (self.gpt_b64, "", 0)),
            (" RL 1 ", ("", "", 0)),
            ("od -An", (sparse_header_hex(2_000_000), "", 0)),
        ]
        self._configure(responses)
        streamed: list[str] = []

        async def scenario():
            async def stream(_ssh, command, *, timeout, on_chunk=None):
                streamed.append(command)
                if " UL " in command:
                    return "Upgrade loader ok", 0
                if " DI " in command:
                    return "Write image ok(100%)", 0
                return "", 0

            with patch.object(partition_burn, "_stream_tool_command", stream):
                return await partition_burn.run_partition_burn(
                    object(), suite_dir="/suite",
                    remote_tool="/suite/.gms_upgrade_tool",
                    remote_firmware="/suite/update.img",
                )

        with self.assertRaises(partition_burn.PartitionBurnError) as ctx:
            asyncio.run(scenario())
        self.assertIn("超过 parameter.txt 声明容量", str(ctx.exception))
        self.assertFalse(any(" DI " in c for c in streamed))
        self.assertFalse(any(" UL " in c for c in streamed))

    def test_forced_transport_probe_stops_before_any_write(self):
        responses = [
            ("SFI", (REAL_SFI_OUTPUT, "", 0)),
            ("stat -c", (CACHE_HIT_STAT_OUTPUT, "", 0)),
            ("parameter.txt", (REAL_PARAMETER_TXT, "", 0)),
            (" RID", [
                ("Read flash ID Fail!", "", 1),
                ("Read Flash ID OK", "", 0),
            ]),
            (" RD", ("Reset Device OK", "", 0)),
        ]
        manager = self._configure(responses)
        streamed: list[str] = []
        routes = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
        }]

        async def scenario():
            async def stream(_ssh, command, *, timeout, on_chunk=None):
                streamed.append(command)
                return "Download Boot Success", 0

            async def baseline(_routes):
                return {
                    ("172.16.14.66", "1-1"): {
                        "instance_id": "USB\\VID_2207&PID_351A\\OLD",
                        "vid_pid": "2207:351a",
                    }
                }, ""

            async def watcher(_ssh, _routes, **kwargs):
                kwargs["ready_event"].set()
                return {"success": True, "errors": {}}

            async def loader_ready(_ssh, _cmd, _count, **_kwargs):
                return True, "List of rockusb connected(1)"

            with patch.object(
                partition_burn, "_stream_tool_command", stream,
            ), patch.object(
                partition_burn, "_capture_route_baseline",
                side_effect=baseline,
            ), patch.object(
                partition_burn, "_reattach_routes", side_effect=watcher,
            ) as reattach, patch.object(
                partition_burn, "_wait_for_loaders",
                side_effect=loader_ready,
            ):
                result = await partition_burn.run_partition_burn(
                    object(), suite_dir="/suite",
                    remote_tool="/suite/.gms_upgrade_tool",
                    remote_firmware="/suite/update.img",
                    usbip_routes=routes,
                    transport_probe=True,
                    force_usbip_bind=True,
                )
            self.assertTrue(reattach.call_args.kwargs["require_forced"])
            return result

        result = asyncio.run(scenario())
        self.assertTrue(result["transport_probe"])
        self.assertTrue(result["db_required"])
        self.assertEqual(result["written"], [])
        self.assertTrue(any(" DB " in command for command in streamed))
        self.assertFalse(any(" UL " in command for command in streamed))
        self.assertFalse(any(" DI " in command for command in streamed))
        self.assertFalse(any(" RL " in command for command in manager.commands))
        self.assertTrue(any(" RD" in command for command in manager.commands))

    def test_normal_usbip_route_stops_before_unsafe_db(self):
        responses = [
            ("SFI", (REAL_SFI_OUTPUT, "", 0)),
            ("stat -c", (CACHE_HIT_STAT_OUTPUT, "", 0)),
            ("parameter.txt", (REAL_PARAMETER_TXT, "", 0)),
            (" RID", ("Read flash ID Fail!", "", 1)),
        ]
        self._configure(responses)
        streamed: list[str] = []

        async def scenario():
            async def stream(_ssh, command, *, timeout, on_chunk=None):
                streamed.append(command)
                return "", 0

            with patch.object(
                partition_burn, "_stream_tool_command", stream,
            ):
                return await partition_burn.run_partition_burn(
                    object(), suite_dir="/suite",
                    remote_tool="/suite/.gms_upgrade_tool",
                    remote_firmware="/suite/update.img",
                    usbip_routes=[{
                        "device_host": "hcq@172.16.14.66",
                        "source_host": "172.16.14.66",
                        "busids": ["1-1"],
                    }],
                    transport_probe=True,
                )

        with self.assertRaises(partition_burn.PartitionBurnError) as ctx:
            asyncio.run(scenario())
        self.assertIn("DB 前安全停止", str(ctx.exception))
        self.assertFalse(any(" DB " in command for command in streamed))

    def test_missing_extraction_triggers_exf(self):
        responses = [
            ("SFI", (REAL_SFI_OUTPUT, "", 0)),
            ("test -f", ("", "", 1)),
            (" RID", ("Read Flash ID OK", "", 0)),
            ("parameter.txt", (REAL_PARAMETER_TXT, "", 0)),
            ("base64 -w0", (self.gpt_b64, "", 0)),
            (" ld", ("List of rockusb connected(1)", "", 0)),
            (" RL 1 ", ("", "", 0)),
        ]
        self._configure(responses)
        streamed: list[str] = []

        async def scenario():
            async def stream(ssh, command, *, timeout, on_chunk=None):
                streamed.append(command)
                if " EXF " in command:
                    return "Extract ok.", 0
                if " DB " in command:
                    return "Download Boot Success", 0
                if " UL " in command:
                    return "ok", 0
                if " DI " in command:
                    return "ok(100%)", 0
                return "", 0

            with patch.object(partition_burn, "_stream_tool_command", stream):
                return await partition_burn.run_partition_burn(
                    object(), suite_dir="/suite",
                    remote_tool="/suite/.gms_upgrade_tool",
                    remote_firmware="/suite/update.img",
                )

        asyncio.run(scenario())
        self.assertTrue(any(" EXF " in c for c in streamed))

    def test_layout_mismatch_is_rejected(self):
        shifted = base64.b64encode(build_gpt_dump([("uboot_a", 0x9999)])).decode()
        responses = [
            ("SFI", (REAL_SFI_OUTPUT, "", 0)),
            ("stat -c", (CACHE_HIT_STAT_OUTPUT, "", 0)),
            (" RID", ("Read Flash ID OK", "", 0)),
            ("parameter.txt", (REAL_PARAMETER_TXT, "", 0)),
            ("base64 -w0", (shifted, "", 0)),
            (" RL 1 ", ("", "", 0)),
        ]
        self._configure(responses)

        async def scenario():
            async def stream(ssh, command, *, timeout, on_chunk=None):
                return "", 0

            with patch.object(partition_burn, "_stream_tool_command", stream):
                return await partition_burn.run_partition_burn(
                    object(), suite_dir="/suite",
                    remote_tool="/suite/.gms_upgrade_tool",
                    remote_firmware="/suite/update.img",
                )

        with self.assertRaises(partition_burn.PartitionBurnError) as ctx:
            asyncio.run(scenario())
        self.assertIn("不一致", str(ctx.exception))

    def test_loader_session_loss_after_ul_is_reported(self):
        responses = [
            ("SFI", (REAL_SFI_OUTPUT, "", 0)),
            (" RID", ("Read Flash ID OK", "", 0)),
            ("parameter.txt", (REAL_PARAMETER_TXT, "", 0)),
            ("base64 -w0", (self.gpt_b64, "", 0)),
            (" ld", ("List of rockusb connected(0)", "", 0)),
            (" RL 1 ", ("", "", 0)),
        ]
        self._configure(responses)

        async def scenario():
            async def stream(ssh, command, *, timeout, on_chunk=None):
                if " EXF " in command:
                    return "Extract ok.", 0
                if " DB " in command:
                    return "Download Boot Success", 0
                if " UL " in command:
                    return "ok", 0
                return "", 0

            with patch.object(partition_burn, "_stream_tool_command", stream):
                return await partition_burn.run_partition_burn(
                    object(), suite_dir="/suite",
                    remote_tool="/suite/.gms_upgrade_tool",
                    remote_firmware="/suite/update.img",
                )

        with self.assertRaises(partition_burn.PartitionBurnError) as ctx:
            asyncio.run(scenario())
        self.assertIn("RockUSB", str(ctx.exception))

    def test_extraction_cache_hit_skips_exf(self):
        responses = [
            ("SFI", (REAL_SFI_OUTPUT, "", 0)),
            ("stat -c", (CACHE_HIT_STAT_OUTPUT, "", 0)),
            (" RID", ("Read Flash ID OK", "", 0)),
            ("parameter.txt", (REAL_PARAMETER_TXT, "", 0)),
            ("base64 -w0", (self.gpt_b64, "", 0)),
            (" ld", ("List of rockusb connected(1)", "", 0)),
            (" RL 1 ", ("", "", 0)),
        ]
        self._configure(responses)
        streamed: list[str] = []

        async def scenario():
            async def stream(ssh, command, *, timeout, on_chunk=None):
                streamed.append(command)
                if " DB " in command:
                    return "Download Boot Success", 0
                if " UL " in command:
                    return "ok", 0
                if " DI " in command:
                    return "ok(100%)", 0
                return "", 0

            with patch.object(partition_burn, "_stream_tool_command", stream):
                return await partition_burn.run_partition_burn(
                    object(), suite_dir="/suite",
                    remote_tool="/suite/.gms_upgrade_tool",
                    remote_firmware="/suite/update.img",
                )

        result = asyncio.run(scenario())
        self.assertEqual(len(result["written"]), 4)
        self.assertFalse(any(" EXF " in c for c in streamed))

    def test_stale_extraction_with_wrong_sizes_is_repacked(self):
        # 同名但大小不符（换版本固件）：必须重新解包，不能复用旧内容。
        stale = CACHE_HIT_STAT_OUTPUT.replace("955", "111")
        responses = [
            ("SFI", (REAL_SFI_OUTPUT, "", 0)),
            ("stat -c", (stale, "", 0)),
            (" RID", ("Read Flash ID OK", "", 0)),
            ("parameter.txt", (REAL_PARAMETER_TXT, "", 0)),
            ("base64 -w0", (self.gpt_b64, "", 0)),
            (" ld", ("List of rockusb connected(1)", "", 0)),
            (" RL 1 ", ("", "", 0)),
        ]
        self._configure(responses)
        streamed: list[str] = []

        async def scenario():
            async def stream(ssh, command, *, timeout, on_chunk=None):
                streamed.append(command)
                if " EXF " in command:
                    return "Extract ok.", 0
                if " DB " in command:
                    return "Download Boot Success", 0
                if " UL " in command:
                    return "ok", 0
                if " DI " in command:
                    return "ok(100%)", 0
                return "", 0

            with patch.object(partition_burn, "_stream_tool_command", stream):
                return await partition_burn.run_partition_burn(
                    object(), suite_dir="/suite",
                    remote_tool="/suite/.gms_upgrade_tool",
                    remote_firmware="/suite/update.img",
                )

        asyncio.run(scenario())
        self.assertTrue(any(" EXF " in c for c in streamed))

    def test_db_reattach_watcher_failure_surfaces_diagnostics(self):
        responses = [
            ("SFI", (REAL_SFI_OUTPUT, "", 0)),
            ("stat -c", (CACHE_HIT_STAT_OUTPUT, "", 0)),
            ("parameter.txt", (REAL_PARAMETER_TXT, "", 0)),
        ]
        self._configure(responses)
        routes = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
        }]

        async def scenario():
            async def stream(ssh, command, *, timeout, on_chunk=None):
                if " DB " in command:
                    return "Download Boot Success", 0
                return "", 0

            async def capture_ok(_routes):
                return {
                    ("172.16.14.66", "1-1"): {
                        "instance_id": "USB\\VID_2207&PID_351A\\OLD",
                        "vid_pid": "2207:351a",
                    }
                }, ""

            async def watcher_fails(_ssh, _routes, **_kwargs):
                ready_event = _kwargs.get("ready_event")
                if ready_event is not None:
                    ready_event.set()
                return {
                    "success": False,
                    "errors": {"172.16.14.66/1-1": "attach timed out"},
                }

            async def loader_missing(_ssh, _cmd, _count, **_kwargs):
                return False, "List of rockusb connected(0)"

            with patch.object(partition_burn, "_stream_tool_command", stream), \
                    patch.object(
                        partition_burn, "_capture_route_baseline",
                        side_effect=capture_ok,
                    ), patch.object(
                        partition_burn, "_reattach_routes",
                        side_effect=watcher_fails,
                    ), patch.object(
                        partition_burn, "_wait_for_loaders",
                        side_effect=loader_missing,
                    ):
                return await partition_burn.run_partition_burn(
                    object(), suite_dir="/suite",
                    remote_tool="/suite/.gms_upgrade_tool",
                    remote_firmware="/suite/update.img",
                    usbip_routes=routes,
                    force_usbip_bind=True,
                )

        with self.assertRaises(partition_burn.PartitionBurnError) as ctx:
            asyncio.run(scenario())
        self.assertIn("upgrade_tool ld 未发现设备", str(ctx.exception))
        self.assertIn("attach timed out", str(ctx.exception))

    def test_db_recovery_uses_upgrade_tool_ld_as_authority(self):
        responses = [
            ("SFI", (REAL_SFI_OUTPUT, "", 0)),
            ("stat -c", (CACHE_HIT_STAT_OUTPUT, "", 0)),
            (" RID", [
                ("Read flash ID Fail!", "", 1),
                ("Read Flash ID OK", "", 0),
            ]),
            ("parameter.txt", (REAL_PARAMETER_TXT, "", 0)),
            ("base64 -w0", (self.gpt_b64, "", 0)),
            (" ld", ("List of rockusb connected(1)", "", 0)),
            (" RL 1 ", ("", "", 0)),
        ]
        self._configure(responses)
        routes = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
        }]
        streamed: list[str] = []

        async def scenario():
            async def stream(ssh, command, *, timeout, on_chunk=None):
                streamed.append(command)
                if " DB " in command:
                    # 生产所用 upgrade_tool 在 USB reset 后旧通道立即消失，
                    # 只返回 Start；这仍必须解锁有 PnP 基线保护的 watcher。
                    return "Download Boot Start", 0
                if " UL " in command:
                    return "ok", 0
                if " DI " in command:
                    return "ok(100%)", 0
                return "", 0

            async def capture_ok(_routes):
                return {
                    ("172.16.14.66", "1-1"): {
                        "instance_id": "USB\\VID_2207&PID_351A\\OLD",
                        "vid_pid": "2207:351a",
                    }
                }, ""

            async def watcher_is_uncertain(_ssh, _routes, **kwargs):
                kwargs["ready_event"].set()
                return {
                    "success": False,
                    "errors": {"172.16.14.66/1-1": "attach result uncertain"},
                }

            async def loader_ready(_ssh, _cmd, _count, **_kwargs):
                return True, "List of rockusb connected(1)"

            with patch.object(
                partition_burn, "_stream_tool_command", stream,
            ), patch.object(
                partition_burn, "_capture_route_baseline",
                side_effect=capture_ok,
            ), patch.object(
                partition_burn, "_reattach_routes",
                side_effect=watcher_is_uncertain,
            ), patch.object(
                partition_burn, "_wait_for_loaders",
                side_effect=loader_ready,
            ):
                return await partition_burn.run_partition_burn(
                    object(), suite_dir="/suite",
                    remote_tool="/suite/.gms_upgrade_tool",
                    remote_firmware="/suite/update.img",
                    usbip_routes=routes,
                    force_usbip_bind=True,
                )

        result = asyncio.run(scenario())
        self.assertEqual(len(result["written"]), 4)
        self.assertTrue(any(" DB " in command for command in streamed))

    def test_db_failure_aborts_before_any_flash_access(self):
        responses = [
            ("SFI", (REAL_SFI_OUTPUT, "", 0)),
            ("parameter.txt", (REAL_PARAMETER_TXT, "", 0)),
        ]
        self._configure(responses)

        async def scenario():
            async def stream(ssh, command, *, timeout, on_chunk=None):
                if " EXF " in command:
                    return "Extract ok.", 0
                if " DB " in command:
                    return "Download Boot Fail", 1
                return "", 0

            async def loader_ready(_ssh, _cmd, _count, **_kwargs):
                return True, "List of rockusb connected(1)"

            with patch.object(
                partition_burn, "_stream_tool_command", stream,
            ), patch.object(
                partition_burn, "_wait_for_loaders",
                side_effect=loader_ready,
            ):
                return await partition_burn.run_partition_burn(
                    object(), suite_dir="/suite",
                    remote_tool="/suite/.gms_upgrade_tool",
                    remote_firmware="/suite/update.img",
                )

        with self.assertRaises(partition_burn.PartitionBurnError) as ctx:
            asyncio.run(scenario())
        self.assertIn("DRAM Loader", str(ctx.exception))

    def test_storage_probe_failure_after_db_reports_recovery_hint(self):
        responses = [
            ("SFI", (REAL_SFI_OUTPUT, "", 0)),
            (" RID", ("Read flash ID Fail!", "", 1)),
            ("parameter.txt", (REAL_PARAMETER_TXT, "", 0)),
        ]
        self._configure(responses)

        async def scenario():
            async def stream(ssh, command, *, timeout, on_chunk=None):
                if " EXF " in command:
                    return "Extract ok.", 0
                if " DB " in command:
                    return "Download Boot Success", 0
                return "", 0

            async def loader_ready(_ssh, _cmd, _count, **_kwargs):
                return True, "List of rockusb connected(1)"

            with patch.object(
                partition_burn, "_stream_tool_command", stream,
            ), patch.object(
                partition_burn, "_wait_for_loaders",
                side_effect=loader_ready,
            ):
                return await partition_burn.run_partition_burn(
                    object(), suite_dir="/suite",
                    remote_tool="/suite/.gms_upgrade_tool",
                    remote_firmware="/suite/update.img",
                )

        with self.assertRaises(partition_burn.PartitionBurnError) as ctx:
            asyncio.run(scenario())
        self.assertIn("断电重上电", str(ctx.exception))

    def test_write_failure_aborts_with_partition_context(self):
        responses = [
            ("SFI", (REAL_SFI_OUTPUT, "", 0)),
            (" RID", ("Read Flash ID OK", "", 0)),
            ("parameter.txt", (REAL_PARAMETER_TXT, "", 0)),
            ("base64 -w0", (self.gpt_b64, "", 0)),
            (" ld", ("List of rockusb connected(1)", "", 0)),
            (" RL 1 ", ("", "", 0)),
        ]
        self._configure(responses)

        async def scenario():
            async def stream(ssh, command, *, timeout, on_chunk=None):
                if " EXF " in command:
                    return "Extract ok.", 0
                if " DB " in command:
                    return "Download Boot Success", 0
                if " UL " in command:
                    return "ok", 0
                if " DI " in command:
                    return "ERROR:Download image fail", 1
                return "", 0

            with patch.object(partition_burn, "_stream_tool_command", stream):
                return await partition_burn.run_partition_burn(
                    object(), suite_dir="/suite",
                    remote_tool="/suite/.gms_upgrade_tool",
                    remote_firmware="/suite/update.img",
                )

        with self.assertRaises(partition_burn.PartitionBurnError) as ctx:
            asyncio.run(scenario())
        self.assertIn("uboot_a", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
