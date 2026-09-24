from __future__ import annotations

import pytest

from worker_agent.fastboot_workflow import (
    CommandResult,
    FastbootPreparationError,
    FastbootPreparer,
    vendor_partition,
)


class FakeRunner:
    def __init__(
        self,
        *,
        serial: str = "RK3572GMS1",
        mode: str = "",
        board: str = "rk3572",
        vendor: str = "",
    ):
        self.serial = serial
        self.mode = mode
        self.board = board
        self.vendor = vendor
        self.commands: list[list[str]] = []

    def __call__(self, argv: list[str], _timeout: int) -> CommandResult:
        self.commands.append(argv)
        if argv == ["fastboot", "devices"]:
            state = "fastbootd" if self.mode == "userspace" else "fastboot"
            output = f"{self.serial}\t{state}\n" if self.mode else ""
            return CommandResult(stdout=output)
        if argv[:3] == ["adb", "-s", self.serial]:
            if argv[3:] == ["shell", "getprop", "ro.board.platform"]:
                return CommandResult(stdout=f"{self.board}\n")
            if argv[3:] == ["shell", "getprop", "ro.vendor.api_level"]:
                return CommandResult(stdout=f"{self.vendor}\n")
            if argv[3:] == ["reboot", "bootloader"]:
                self.mode = "bootloader"
                return CommandResult()
        if argv[:3] != ["fastboot", "-s", self.serial]:
            return CommandResult(stderr=f"unexpected command: {argv}", code=1)
        command = argv[3:]
        if command == ["getvar", "is-userspace"]:
            value = "yes" if self.mode == "userspace" else "no"
            return CommandResult(stderr=f"is-userspace: {value}\n")
        if command == ["getvar", "product"]:
            return CommandResult(stderr=f"product: {self.board}\n")
        if command == ["reboot", "bootloader"]:
            self.mode = "bootloader"
            return CommandResult()
        if command == ["reboot", "fastboot"]:
            self.mode = "userspace"
            return CommandResult()
        return CommandResult()


@pytest.mark.parametrize(
    ("serial", "board", "vendor", "expected"),
    [
        # RK3572：全平台统一识别 board:<action>（与 vendor/uboot 版本无关）。
        ("RK3572GMS1", "", "", "board:unlock"),
        ("GENERIC-1", "rk3572", "", "board:unlock"),
        ("GENERIC-1", "rk3572", "34", "board:unlock"),
        # RK3588GMS7 纯 A17：vendor API level 为日期码 202604 → board:。
        ("RK3588GMS7", "rk3588", "202604", "board:unlock"),
        # A16 时代 vendor（202504）与更早 vendor 一律 at-*。
        ("RK3588GMS7", "rk3588", "202504", "at-unlock-vboot"),
        # GRF+SSI 回归（RK3562GMS1 事故）：system 底座报 ro.build.version.sdk=37，
        # 但 vendor 停在 A14（vendor.api_level=34）→ 按 vendor 判定为 at-*。
        ("RK3562GMS1", "rk3562", "34", "at-unlock-vboot"),
        # 版本未知（设备已在 fastboot，无 ADB 阶段）：非 RK3572 默认旧命令，
        # apply_oem_action 在明确 unrecognized 时以备选命令兜底重试。
        ("RK3588GMS7", "rk3588", "", "at-unlock-vboot"),
        ("GENERIC-1", "", "", "at-unlock-vboot"),
    ],
)
def test_python_preparation_selects_oem_argument(
    serial: str,
    board: str,
    vendor: str,
    expected: str,
) -> None:
    runner = FakeRunner(serial=serial, board=board, vendor=vendor)
    prepared = FastbootPreparer(
        runner,
        sleep=lambda _seconds: None,
    ).prepare_bootloader(serial)

    assert prepared.oem_argument("unlock") == expected
    assert ["adb", "-s", serial, "reboot", "bootloader"] in runner.commands


def test_apply_oem_action_falls_back_when_lock_command_unrecognized() -> None:
    """版本未知时 lock 默认旧命令；uboot（纯 A17）不识别 at-lock-vboot 时
    以 board:lock 兜底，而不是让锁定脚本 `set -e` 中途退出、设备滞留
    fastboot 无法开机（RK3562GMS1 事故的镜像场景）。"""
    runner = FakeRunner(serial="RK3562GMS1", board="rk3562", vendor="")
    prepared = FastbootPreparer(
        runner, sleep=lambda _seconds: None,
    ).prepare_bootloader(runner.serial)
    assert prepared.oem_argument("lock") == "at-lock-vboot"

    class UnrecognizedOnceRunner(FakeRunner):
        def __call__(self, argv: list[str], _timeout: int) -> CommandResult:
            self.commands.append(argv)
            if argv[:4] == ["fastboot", "-s", self.serial, "oem"]:
                if "at-lock-vboot" in argv:
                    return CommandResult(
                        stderr="FAILED (remote: 'unrecognized command')\n",
                        code=1,
                    )
                return CommandResult()
            return super().__call__(argv, _timeout)

    failing = UnrecognizedOnceRunner(serial=runner.serial)
    FastbootPreparer(failing, sleep=lambda _seconds: None).apply_oem_action(
        prepared, "lock",
    )
    board_attempt = [argv for argv in failing.commands if "board:lock" in argv]
    assert board_attempt, "unrecognized 后未以 board:lock 兜底"


def test_unlock_bootloader_falls_back_when_oem_command_unrecognized() -> None:
    """版本未知时默认旧命令；明确 unrecognized 后以 board:unlock 重试。"""
    runner = FakeRunner(serial="RK3588GMS7", board="rk3588", vendor="")
    runner_device = FastbootPreparer(runner, sleep=lambda _s: None)
    prepared = runner_device.prepare_bootloader(runner.serial)
    assert prepared.oem_argument("unlock") == "at-unlock-vboot"

    class UnrecognizedOnceRunner(FakeRunner):
        def __call__(self, argv: list[str], _timeout: int) -> CommandResult:
            self.commands.append(argv)
            if argv[:4] == ["fastboot", "-s", self.serial, "oem"]:
                if "at-unlock-vboot" in argv:
                    return CommandResult(
                        stderr="FAILED (remote: 'unrecognized command')\n",
                        code=1,
                    )
                return CommandResult()
            return super().__call__(argv, _timeout)

    failing = UnrecognizedOnceRunner(serial=runner.serial)
    FastbootPreparer(failing, sleep=lambda _s: None).unlock_bootloader(prepared)
    board_attempt = [
        argv for argv in failing.commands if "board:unlock" in argv
    ]
    assert board_attempt, "unrecognized 后未以 board:unlock 重试"


def test_python_preparation_moves_fastbootd_back_to_bootloader() -> None:
    runner = FakeRunner(mode="userspace")
    prepared = FastbootPreparer(
        runner,
        sleep=lambda _seconds: None,
    ).prepare_bootloader(runner.serial)

    assert prepared.oem_argument("lock") == "board:lock"
    assert [
        "fastboot",
        "-s",
        runner.serial,
        "reboot",
        "bootloader",
    ] in runner.commands


def test_fastboot_state_parser_accepts_android_fastboot_vendor_label() -> None:
    assert FastbootPreparer._parse_fastboot_state(
        "rk3572test\t Android Fastboot\n",
        "rk3572test",
    ) == "fastboot"


def test_python_preparation_notifies_usbip_transport_after_mode_switch() -> None:
    runner = FakeRunner()
    transitions: list[tuple[str, str]] = []

    FastbootPreparer(
        runner,
        sleep=lambda _seconds: None,
        on_transport_reset=lambda serial, mode: transitions.append((serial, mode)),
    ).prepare_bootloader(runner.serial)

    assert transitions == [(runner.serial, "fastboot")]


def test_gsi_preparation_transitions_bootloader_fastboot_to_fastbootd() -> None:
    runner = FakeRunner(mode="bootloader")
    transitions: list[tuple[str, str]] = []

    prepared = FastbootPreparer(
        runner,
        sleep=lambda _seconds: None,
        on_transport_reset=lambda serial, mode: transitions.append((serial, mode)),
    ).prepare_gsi_fastbootd(runner.serial)

    assert prepared.oem_argument("unlock") == "board:unlock"
    assert [
        "fastboot", "-s", runner.serial, "oem", "board:unlock",
    ] in runner.commands
    assert [
        "fastboot", "-s", runner.serial, "reboot", "fastboot",
    ] in runner.commands
    assert transitions == [(runner.serial, "fastbootd")]
    assert runner.mode == "userspace"


def test_python_preparation_times_out_when_device_never_reenumerates() -> None:
    runner = FakeRunner()

    def never_reenumerates(argv: list[str], timeout: int) -> CommandResult:
        result = runner(argv, timeout)
        if argv[0] == "adb" and argv[-2:] == ["reboot", "bootloader"]:
            runner.mode = ""
        return result

    # 假时钟：每次探测自推进 10s 模拟 fastboot_mode() 的真实阻塞，验证
    # deadline 按单调时钟收敛（循环次数语义下 120 次探测 ≈ 二十分钟）。
    now = [0.0]

    def fake_monotonic() -> float:
        return now[0]

    def probe_costly_sleep(seconds: float) -> None:
        now[0] += 10.0

    with pytest.raises(FastbootPreparationError, match="did not enter"):
        FastbootPreparer(
            never_reenumerates,
            sleep=probe_costly_sleep,
            monotonic=fake_monotonic,
        ).prepare_bootloader(runner.serial)


def test_mode_probe_shares_one_deadline_across_fastboot_commands() -> None:
    """devices 吃完预算后不得再给 getvar 一份完整超时。"""
    now = [0.0]
    calls: list[tuple[list[str], int]] = []

    def slow_runner(argv: list[str], timeout: int) -> CommandResult:
        calls.append((argv, timeout))
        now[0] += timeout
        if argv == ["fastboot", "devices"]:
            return CommandResult(stdout="SERIAL\tfastboot\n")
        return CommandResult(stderr="is-userspace: no\n")

    preparer = FastbootPreparer(
        slow_runner,
        bootloader_probe_timeout=8,
        monotonic=lambda: now[0],
    )

    assert preparer.fastboot_mode("SERIAL", timeout=5) == ""
    assert calls == [(["fastboot", "devices"], 5)]
    assert now[0] == 5


def test_vendor_partition_is_decided_in_python() -> None:
    assert vendor_partition("/images/vendor_boot-debug.img") == "vendor_boot"
    assert vendor_partition("/images/boot-debug.img") == "boot"
