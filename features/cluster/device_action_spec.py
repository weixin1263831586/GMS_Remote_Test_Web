"""DeviceActionSpec：设备操作的单一真值（single source of truth）。

历史上同一个 action 的属性散落在多套集合里：

- Controller ``READ_ONLY_DEVICE_ACTIONS``（是否申请独占 claim）
- Controller ``ELEVATED_DEVICE_ACTIONS``（是否需要管理员提权）
- Controller ``NOTIFIED_DEVICE_ACTIONS``（是否发终态通知）
- Worker ``inspection_actions``（单设备 inspection 通道）
- Controller/Worker 各自的 ADB Proxy forbidden 集合
- Controller wait timeout 分档

每新增一个 action（wipe_data / reboot_recovery / install_apk …）都容易漏
其中一处。本模块把每个 action 的全部属性收敛到一条 ``DeviceActionSpec``
记录，Controller 与 Worker 各自按需读取派生集合；新增 action 只需在这里
登记一行。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceActionSpec:
    action: str
    # read_only: 不申请独占 claim、不做 exclusive fencing，
    # 测试占用中的设备仍可执行（Device Info / UI 操控等查看类操作）。
    read_only: bool = False
    # elevated: 需要管理员提权（bootloader 解锁、dm-verity override 等）。
    elevated: bool = False
    # terminal_notification: 完成后向通知中心发终态通知。
    terminal_notification: bool = False
    # inspection: Worker 端走 android_inspection 通道，且要求恰好一台设备。
    inspection: bool = False
    # forbidden_on_adb_proxy: ADB Proxy 远程设备没有本地 USB/Fastboot 通道。
    forbidden_on_adb_proxy: bool = False
    # wait_steps: Controller 同步等待 Worker ack 的轮询步数（0.1s/步）。
    # None = 默认 100 步（10s）。
    wait_steps: int | None = None


_DEVICE_ACTION_SPECS: tuple[DeviceActionSpec, ...] = (
    # ---- 状态改变 / 重启类（通知 + 独占 claim）----
    DeviceActionSpec(action="reboot", terminal_notification=True),
    DeviceActionSpec(action="reboot_bootloader", terminal_notification=True,
                     forbidden_on_adb_proxy=True),
    DeviceActionSpec(action="remount", terminal_notification=True),
    # ---- Bootloader / verity override（提权 + 通知）----
    DeviceActionSpec(action="bootloader_lock", elevated=True,
                     terminal_notification=True, forbidden_on_adb_proxy=True),
    DeviceActionSpec(action="bootloader_unlock", elevated=True,
                     terminal_notification=True, forbidden_on_adb_proxy=True),
    DeviceActionSpec(action="override_apply", elevated=True,
                     terminal_notification=True, inspection=True, wait_steps=1800),
    DeviceActionSpec(action="override_revert", elevated=True,
                     terminal_notification=True, inspection=True, wait_steps=1800),
    DeviceActionSpec(action="override_disable_verity", elevated=True,
                     terminal_notification=True, inspection=True),
    DeviceActionSpec(action="override_enable_verity", elevated=True,
                     terminal_notification=True, inspection=True),
    DeviceActionSpec(action="override_reboot", elevated=True,
                     terminal_notification=True, inspection=True),
    # ---- 网络等配置类 ----
    DeviceActionSpec(action="wifi"),
    # ---- UI 交互（即时返回，不通知）----
    DeviceActionSpec(action="tap"),
    DeviceActionSpec(action="scrcpy_start"),
    # ---- 只读 inspection（并发安全：不申请 claim）----
    DeviceActionSpec(action="screenshot", read_only=True, wait_steps=350),
    DeviceActionSpec(action="layout", read_only=True, wait_steps=350),
    DeviceActionSpec(action="get_properties", read_only=True, wait_steps=350),
    DeviceActionSpec(action="bootloader_status", read_only=True, wait_steps=350),
    DeviceActionSpec(action="packages_with_path", read_only=True, inspection=True,
                     wait_steps=350),
    DeviceActionSpec(action="packages_all", read_only=True, inspection=True,
                     wait_steps=350),
    DeviceActionSpec(action="features", read_only=True, inspection=True,
                     wait_steps=350),
    DeviceActionSpec(action="props", read_only=True, inspection=True,
                     wait_steps=350),
    DeviceActionSpec(action="config_explore", read_only=True, inspection=True,
                     wait_steps=1800),
    DeviceActionSpec(action="override_status", read_only=True, inspection=True,
                     wait_steps=350),
)


DEVICE_ACTION_SPECS: dict[str, DeviceActionSpec] = {
    spec.action: spec for spec in _DEVICE_ACTION_SPECS
}


def device_action_spec(action: str) -> DeviceActionSpec | None:
    return DEVICE_ACTION_SPECS.get(action)


def read_only_device_actions() -> frozenset[str]:
    return frozenset(
        action for action, spec in DEVICE_ACTION_SPECS.items() if spec.read_only)


def elevated_device_actions() -> frozenset[str]:
    return frozenset(
        action for action, spec in DEVICE_ACTION_SPECS.items() if spec.elevated)


def notified_device_actions() -> frozenset[str]:
    return frozenset(
        action for action, spec in DEVICE_ACTION_SPECS.items()
        if spec.terminal_notification)


def inspection_device_actions() -> frozenset[str]:
    return frozenset(
        action for action, spec in DEVICE_ACTION_SPECS.items() if spec.inspection)


def adb_proxy_forbidden_device_actions() -> frozenset[str]:
    return frozenset(
        action for action, spec in DEVICE_ACTION_SPECS.items()
        if spec.forbidden_on_adb_proxy)


def device_action_wait_steps(action: str, default: int = 100) -> int:
    spec = DEVICE_ACTION_SPECS.get(action)
    return spec.wait_steps if spec and spec.wait_steps is not None else default
