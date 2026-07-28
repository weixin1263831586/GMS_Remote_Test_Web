from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePath


@dataclass(frozen=True)
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    code: int = 0

    @property
    def output(self) -> str:
        return "\n".join(
            value.strip() for value in (self.stdout, self.stderr) if value.strip()
        )


@dataclass(frozen=True)
class PreparedFastbootDevice:
    serial: str
    identity: str

    def oem_argument(self, action: str) -> str:
        if action not in {"lock", "unlock"}:
            raise ValueError("action must be lock or unlock")
        if "rk3572" in self.identity.lower():
            return f"board:{action}"
        return f"at-{action}-vboot"


class FastbootPreparationError(RuntimeError):
    pass


Runner = Callable[[list[str], int], CommandResult]


def subprocess_runner(argv: list[str], timeout: int) -> CommandResult:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return CommandResult(completed.stdout, completed.stderr, completed.returncode)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(stderr=str(exc), code=-1)


class FastbootPreparer:
    """Python-side state and platform decisions for the thin shell runners."""

    def __init__(
        self,
        runner: Runner,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.runner = runner
        self.sleep = sleep

    def _execute(
        self,
        argv: list[str],
        *,
        timeout: int = 30,
        required: bool = True,
    ) -> CommandResult:
        result = self.runner(argv, timeout)
        if required and result.code != 0:
            detail = result.output or f"exit code {result.code}"
            raise FastbootPreparationError(f"{' '.join(argv[:3])} failed: {detail}")
        return result

    @staticmethod
    def _parse_fastboot_state(output: str, serial: str) -> str:
        for line in (output or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == serial:
                state = parts[1].lower()
                if state in {"fastboot", "fastbootd"}:
                    return state
        return ""

    def fastboot_mode(self, serial: str) -> str:
        listed = self._execute(
            ["fastboot", "devices"],
            timeout=8,
            required=False,
        )
        state = self._parse_fastboot_state(listed.output, serial)
        if not state:
            return ""
        if state == "fastbootd":
            return "userspace"
        userspace = self._execute(
            ["fastboot", "-s", serial, "getvar", "is-userspace"],
            timeout=8,
            required=False,
        )
        return (
            "userspace"
            if "is-userspace: yes" in userspace.output.lower()
            else "bootloader"
        )

    def _wait_for_bootloader(self, serial: str, timeout: int = 45) -> None:
        for _attempt in range(timeout):
            if self.fastboot_mode(serial) == "bootloader":
                return
            self.sleep(1)
        raise FastbootPreparationError(
            f"device {serial} did not enter bootloader Fastboot within {timeout}s"
        )

    def prepare_bootloader(self, serial: str) -> PreparedFastbootDevice:
        mode = self.fastboot_mode(serial)
        board = ""
        if not mode:
            board_result = self._execute(
                ["adb", "-s", serial, "shell", "getprop", "ro.board.platform"],
                timeout=8,
                required=False,
            )
            board = board_result.output
            self._execute(["adb", "-s", serial, "reboot", "bootloader"])
            self._wait_for_bootloader(serial)
        elif mode == "userspace":
            self._execute(
                ["fastboot", "-s", serial, "reboot", "bootloader"],
                required=False,
            )
            self._wait_for_bootloader(serial)

        product = self._execute(
            ["fastboot", "-s", serial, "getvar", "product"],
            timeout=10,
            required=False,
        ).output
        return PreparedFastbootDevice(
            serial=serial,
            identity=f"{serial} {board} {product}",
        )


def vendor_partition(image_path: str) -> str:
    name = PurePath(image_path).name.lower()
    return "boot" if name.startswith("boot") else "vendor_boot"
