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
    ):
        self.serial = serial
        self.mode = mode
        self.board = board
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
        return CommandResult()


@pytest.mark.parametrize(
    ("serial", "board", "expected"),
    [
        ("RK3572GMS1", "", "board:unlock"),
        ("GENERIC-1", "rk3572", "board:unlock"),
        ("RK3576GMS1", "rk3576", "at-unlock-vboot"),
    ],
)
def test_python_preparation_selects_oem_argument(
    serial: str,
    board: str,
    expected: str,
) -> None:
    runner = FakeRunner(serial=serial, board=board)
    prepared = FastbootPreparer(
        runner,
        sleep=lambda _seconds: None,
    ).prepare_bootloader(serial)

    assert prepared.oem_argument("unlock") == expected
    assert ["adb", "-s", serial, "reboot", "bootloader"] in runner.commands


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
