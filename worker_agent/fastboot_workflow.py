from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePath

# Canonical CommandResult lives in foundation; re-exported here so the
# existing `from worker_agent.fastboot_workflow import CommandResult`
# call sites keep working (features/firmware, features/devices).
from foundation.command_result import CommandResult


__all__ = [
    "ANDROID_17_VENDOR_API_LEVEL",
    "BootloaderOemProfile",
    "CommandResult",
    "FastbootPreparationError",
    "FastbootPreparer",
    "PreparedFastbootDevice",
    "Runner",
    "resolve_bootloader_oem_profile",
    "vendor_partition",
]


# Android 17 的 vendor API level（日期码格式）。oem lock/unlock 命令由
# uboot 决定，而 uboot 跟随 vendor 固件发布，不跟随 SSI 系统底座：
#   * 纯 Android 17 的 vendor（ro.vendor.api_level = 202604）识别
#     `oem board:<action>`；
#   * GRF+SSI 等旧 vendor 构建（如 A17 SSI 底座 + A14 vendor 的
#     RK3562GMS1，vendor.api_level = 34）只认 `oem at-<action>-vboot`。
# 注意不能用 ro.build.version.sdk 判定：GRF+SSI 构建的 system 侧 SDK
# 跟随 SSI 底座，同样会报 37。
ANDROID_17_VENDOR_API_LEVEL = 202604


@dataclass(frozen=True)
class BootloaderOemProfile:
    """Bootloader OEM lock/unlock 能力档案（平台 hardcode → capability 化）。

    不再在流程里散落 ``if "rk3572" ...`` 平台分支；每种 uboot 家族登记为
    一个不可变 profile，``commands`` 是按优先级排列的 oem 命令模板
    （``{action}`` 占位符替换为 lock/unlock）。首个命令是按已知信号选出
    的首选；后续条目是 ``apply_oem_action`` 在 uboot 明确 unrecognized
    时的兜底候选（传输类失败不做命令级重试）。
    """

    name: str
    commands: tuple[str, ...]

    def primary(self, action: str) -> str:
        """首选 oem 命令参数（resolve 时依据 identity/vendor_api_level 选定）。"""
        self._require_action(action)
        return self.commands[0].format(action=action)

    def fallback(self, action: str) -> str:
        """unrecognized 兜底命令；profile 只登记一条命令时无兜底。"""
        self._require_action(action)
        if len(self.commands) < 2:
            raise FastbootPreparationError(
                f"bootloader profile {self.name} has no fallback oem command"
            )
        return self.commands[1].format(action=action)

    @staticmethod
    def _require_action(action: str) -> None:
        if action not in {"lock", "unlock"}:
            raise ValueError("action must be lock or unlock")


#: Rockchip 新一代 uboot（RK3572 全平台 / vendor API level 达 A17）：
#: 统一识别 `oem board:<action>`；旧命令留作兜底。
_OEM_PROFILE_ROCKCHIP_BOARD = BootloaderOemProfile(
    "rockchip_board_command",
    ("board:{action}", "at-{action}-vboot"),
)
#: Rockchip 旧 vboot uboot（vendor < A17，含 GRF+SSI 组合）：
#: 只认 `oem at-<action>-vboot`；新命令留作兜底。
_OEM_PROFILE_ROCKCHIP_VBOOT_LEGACY = BootloaderOemProfile(
    "rockchip_vboot_legacy",
    ("at-{action}-vboot", "board:{action}"),
)

BOOTLOADER_OEM_PROFILES: dict[str, BootloaderOemProfile] = {
    profile.name: profile
    for profile in (_OEM_PROFILE_ROCKCHIP_BOARD, _OEM_PROFILE_ROCKCHIP_VBOOT_LEGACY)
}


def resolve_bootloader_oem_profile(
    identity: str = "", vendor_api_level: int = 0
) -> BootloaderOemProfile:
    """由设备信号解析 OEM 能力档案（ADR：capability 取代平台 hardcode）。

    判定信号依次为 ``identity``（serial + ro.board.platform + getvar
    product）与 ``vendor_api_level``；两者都未知时按旧 vboot 命令起步，
    由 ``apply_oem_action`` 的 unrecognized 兜底重试纠正误判。
    """
    if "rk3572" in (identity or "").lower():
        return _OEM_PROFILE_ROCKCHIP_BOARD
    level = int(vendor_api_level or 0)
    if level >= ANDROID_17_VENDOR_API_LEVEL:
        return _OEM_PROFILE_ROCKCHIP_BOARD
    return _OEM_PROFILE_ROCKCHIP_VBOOT_LEGACY


@dataclass(frozen=True)
class PreparedFastbootDevice:
    serial: str
    identity: str
    # ro.vendor.api_level，在 ADB 阶段读取；设备已在 fastboot（无 ADB
    # 阶段）时为 0（未知），oem 命令按旧 vendor 处理，并由 unrecognized
    # 兜底重试纠正误判。
    vendor_api_level: int = 0
    # 由 identity + vendor_api_level 解析出的 OEM 能力档案；缺省按旧
    # vboot 处理（与"版本未知"语义一致）。
    profile: BootloaderOemProfile = field(
        default_factory=lambda: _OEM_PROFILE_ROCKCHIP_VBOOT_LEGACY
    )

    def oem_argument(self, action: str) -> str:
        # 解锁/上锁命令与设备当前 uboot 版本绑定，而 uboot 随 vendor 固件
        # 发布；首选命令由 resolve_bootloader_oem_profile 按 identity +
        # vendor_api_level 决定，兜底顺序见 BootloaderOemProfile。
        return self.profile.primary(action)


class FastbootPreparationError(RuntimeError):
    pass


Runner = Callable[[list[str], int], CommandResult]
TransportResetCallback = Callable[[str, str], None]


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
        on_transport_reset: TransportResetCallback | None = None,
        bootloader_timeout: int = 120,
    ):
        self.runner = runner
        self.sleep = sleep
        self.on_transport_reset = on_transport_reset
        self.bootloader_timeout = max(1, int(bootloader_timeout))

    def _notify_transport_reset(
        self,
        serial: str,
        target_protocol: str = "fastboot",
    ) -> None:
        if self.on_transport_reset:
            self.on_transport_reset(serial, target_protocol)

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
                states = {part.lower() for part in parts[1:]}
                if "fastbootd" in states:
                    return "fastbootd"
                if "fastboot" in states:
                    return "fastboot"
        return ""

    @staticmethod
    def _parse_api_level(output: str) -> int:
        try:
            return max(0, int((output or "").strip().splitlines()[0].strip()))
        except (IndexError, ValueError):
            return 0

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

    def _wait_for_bootloader(self, serial: str, timeout: int | None = None) -> None:
        timeout = self.bootloader_timeout if timeout is None else max(1, int(timeout))
        for _attempt in range(timeout):
            if self.fastboot_mode(serial) == "bootloader":
                return
            self.sleep(1)
        raise FastbootPreparationError(
            f"device {serial} did not enter bootloader Fastboot within {timeout}s"
        )

    def _wait_for_fastbootd(self, serial: str, timeout: int | None = None) -> None:
        timeout = self.bootloader_timeout if timeout is None else max(1, int(timeout))
        for _attempt in range(timeout):
            if self.fastboot_mode(serial) == "userspace":
                return
            self.sleep(1)
        raise FastbootPreparationError(
            f"device {serial} did not enter Fastbootd within {timeout}s"
        )

    def prepare_bootloader(self, serial: str) -> PreparedFastbootDevice:
        mode = self.fastboot_mode(serial)
        board = ""
        vendor_api_level = 0
        if not mode:
            board_result = self._execute(
                ["adb", "-s", serial, "shell", "getprop", "ro.board.platform"],
                timeout=8,
                required=False,
            )
            board = board_result.output
            # uboot 随 vendor 固件发布，oem 命令选择依赖 vendor 侧版本；
            # 此刻设备还在 ADB 模式，是读取版本的唯一窗口。不能用
            # ro.build.version.sdk：GRF+SSI 构建跟随 SSI 系统底座同样报 37。
            vendor_result = self._execute(
                ["adb", "-s", serial, "shell", "getprop", "ro.vendor.api_level"],
                timeout=8,
                required=False,
            )
            vendor_api_level = self._parse_api_level(vendor_result.output)
            self._execute(["adb", "-s", serial, "reboot", "bootloader"])
            self._notify_transport_reset(serial)
            self._wait_for_bootloader(serial)
        elif mode == "userspace":
            self._execute(
                ["fastboot", "-s", serial, "reboot", "bootloader"],
                required=False,
            )
            self._notify_transport_reset(serial)
            self._wait_for_bootloader(serial)

        product = self._execute(
            ["fastboot", "-s", serial, "getvar", "product"],
            timeout=10,
            required=False,
        ).output
        return PreparedFastbootDevice(
            serial=serial,
            identity=f"{serial} {board} {product}",
            vendor_api_level=vendor_api_level,
            profile=resolve_bootloader_oem_profile(
                f"{serial} {board} {product}", vendor_api_level
            ),
        )

    def apply_oem_action(
        self, prepared: PreparedFastbootDevice, action: str,
    ) -> None:
        """Run the platform oem lock/unlock command in bootloader Fastboot.

        版本未知（设备此前已在 fastboot、无 ADB 阶段）时默认命令可能与
        设备 uboot 不匹配：仅在明确 unrecognized 时按 profile 的候选
        顺序用备选命令重试一次。lock 与 unlock 一样保留兜底，否则 uboot
        不识别首选命令时脚本以 `set -e` 中途退出，设备会被留在 fastboot
        无法开机（RK3562 Android 17 GSI 回归）。
        """
        command = prepared.oem_argument(action)
        result = self._execute(
            [
                "fastboot",
                "-s",
                prepared.serial,
                "oem",
                command,
            ],
            timeout=30,
            required=False,
        )
        if result.code == 0:
            return
        if "unrecognized" not in result.output.lower():
            # 传输类失败（设备可能已接受命令）按原样抛出，不盲目重试。
            detail = result.output or f"exit code {result.code}"
            raise FastbootPreparationError(
                f"fastboot -s {prepared.serial} failed: {detail}"
            )
        alternative = prepared.profile.fallback(action)
        self._execute(
            [
                "fastboot",
                "-s",
                prepared.serial,
                "oem",
                alternative,
            ],
            timeout=30,
        )

    def unlock_bootloader(
        self, prepared: PreparedFastbootDevice,
    ) -> None:
        """Unlock writes while the device is in bootloader Fastboot."""
        self.apply_oem_action(prepared, "unlock")

    def enter_fastbootd(
        self, prepared: PreparedFastbootDevice,
    ) -> None:
        """Switch an unlocked bootloader-Fastboot device to Fastbootd."""
        # Some fastboot builds report a transport error after the reboot was
        # already accepted.  The subsequent mode wait is the source of truth.
        self._execute(
            ["fastboot", "-s", prepared.serial, "reboot", "fastboot"],
            timeout=30,
            required=False,
        )
        self._notify_transport_reset(prepared.serial, "fastbootd")
        self._wait_for_fastbootd(prepared.serial)

    def prepare_gsi_fastbootd(self, serial: str) -> PreparedFastbootDevice:
        """Prepare a device for dynamic-partition flashing in Fastbootd.

        USB/IP devices re-enumerate when bootloader Fastboot switches to
        Fastbootd.  Keep that transition in Python so the controller can
        re-bind the new USB identity before the thin flashing script starts.
        """
        prepared = self.prepare_bootloader(serial)
        self.unlock_bootloader(prepared)
        self.enter_fastbootd(prepared)
        return prepared


def vendor_partition(image_path: str) -> str:
    name = PurePath(image_path).name.lower()
    return "boot" if name.startswith("boot") else "vendor_boot"
