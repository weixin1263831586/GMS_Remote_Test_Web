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
        sdk: str = "",
    ):
        self.serial = serial
        self.mode = mode
        self.board = board
        self.sdk = sdk
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
            if argv[3:] == ["shell", "getprop", "ro.build.version.sdk"]:
                return CommandResult(stdout=f"{self.sdk}\n")
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
    ("serial", "board", "sdk", "expected"),
    [
        # RK3572：全平台统一识别 board:<action>（与 Android 版本无关）。
        ("RK3572GMS1", "", "", "board:unlock"),
        ("GENERIC-1", "rk3572", "", "board:unlock"),
        ("GENERIC-1", "rk3572", "34", "board:unlock"),
        # RK3588GMS7 回归用例（GSI 烧写 unrecognized command 失败）：
        # Android 17（SDK 37）的 uboot 用 board:；更早版本用 at-unlock-vboot。
        ("RK3588GMS7", "rk3588", "37", "board:unlock"),
        ("RK3588GMS7", "rk3588", "36", "at-unlock-vboot"),
        ("RK3588GMS7", "", "35", "at-unlock-vboot"),
        # 版本未知（设备已在 fastboot，无 ADB 阶段）：非 RK3572 默认旧命令，
        # unlock_bootloader 在明确 unrecognized 时以备选命令兜底重试。
        ("RK3588GMS7", "", "", "at-unlock-vboot"),
        ("GENERIC-1", "", "", "at-unlock-vboot"),
    ],
)
def test_python_preparation_selects_oem_argument(
    serial: str,
    board: str,
    sdk: str,
    expected: str,
) -> None:
    runner = FakeRunner(serial=serial, board=board, sdk=sdk)
    prepared = FastbootPreparer(
        runner,
        sleep=lambda _seconds: None,
    ).prepare_bootloader(serial)

    assert prepared.oem_argument("unlock") == expected
    assert ["adb", "-s", serial, "reboot", "bootloader"] in runner.commands


def test_unlock_bootloader_falls_back_when_oem_command_unrecognized() -> None:
    """版本未知时默认旧命令；明确 unrecognized 后以 board:unlock 重试。"""
    runner = FakeRunner(serial="RK3588GMS7", board="rk3588", sdk="")
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

    with pytest.raises(FastbootPreparationError, match="did not enter"):
        FastbootPreparer(
            never_reenumerates,
            sleep=lambda _seconds: None,
        ).prepare_bootloader(runner.serial)


def test_vendor_partition_is_decided_in_python() -> None:
    assert vendor_partition("/images/vendor_boot-debug.img") == "vendor_boot"
    assert vendor_partition("/images/boot-debug.img") == "boot"
