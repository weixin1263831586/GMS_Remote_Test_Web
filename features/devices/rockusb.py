"""Rockchip Loader/MaskROM 烧写模式设备的 USB 枚举。

烧写失败后设备常停留在 Loader/MaskROM 模式，对 adb/fastboot 不可见。
通过测试主机 sysfs 枚举 VID 2207 设备并读取 USB 序列号，
让设备列表与固件烧写链路都能继续定位这类设备。

判定必须同时满足 VID 与烧写模式 PID：Rockchip 健康设备会以多种
USB 功能枚举（如 2207:0006 ADB、2207:0007 其他功能），这些 PID
永远不会出现在 adb 枚举里，若只按 VID 判定会把健康设备永久误报
成 Loader。烧写模式 PID 以平台配置 usbip_vid_pids 中 VID 2207 的
条目为准（默认 2207:351a，见 features/devices/usb.py）。
"""

from __future__ import annotations

from collections.abc import Iterable

from .usb import configured_usbip_vid_pids


def rockusb_loader_vid_pids(config: dict | None = None) -> set[str]:
    """返回可判定为 Loader/MaskROM 的 ``pid`` 集合（4 位十六进制小写）。

    ``usbip_vid_pids`` 是 USB/IP 设备过滤用途（含 ADB/Fastboot 模式
    PID），不能整体推导 Loader PID——否则 2207:0006（ADB 模式）等
    健康枚举也会被当成烧写目标。因此只取其中 VID 2207 且不属于已知
    ADB/Fastboot 模式 PID 的条目；未配置时回退到内置 2207:351a。
    判定只认 PID，不带 VID 前缀。
    """
    from .usb import (
        DEFAULT_ANDROID_USBIP_VID_PID_ADB,
        DEFAULT_ANDROID_USBIP_VID_PID_FASTBOOT,
    )

    non_loader = {
        DEFAULT_ANDROID_USBIP_VID_PID_ADB,
        DEFAULT_ANDROID_USBIP_VID_PID_FASTBOOT,
    }
    vid_pids = configured_usbip_vid_pids(config or {})
    loader_pids: set[str] = set()
    for vid_pid in vid_pids:
        candidate = str(vid_pid).strip().lower()
        if candidate in non_loader:
            continue
        vid, _, pid = candidate.partition(":")
        if vid == "2207" and pid:
            loader_pids.add(pid)
    return loader_pids


ROCKCHIP_USB_VENDOR_ID = "2207"

ROCKUSB_SYSFS_PROBE_COMMAND = (
    "for d in /sys/bus/usb/devices/*; do "
    "[ -f \"$d/idVendor\" ] || continue; "
    f"[ \"$(cat \"$d/idVendor\" 2>/dev/null)\" = \"{ROCKCHIP_USB_VENDOR_ID}\" ] || continue; "
    "printf '%s\\t%s\\t%s\\n' "
    "\"$(cat \"$d/idProduct\" 2>/dev/null)\" "
    "\"$(cat \"$d/serial\" 2>/dev/null)\" "
    "\"$(cat \"$d/product\" 2>/dev/null)\"; "
    "done"
)


def parse_rockusb_probe_output(output: str) -> list[dict[str, str]]:
    """解析 sysfs 探测输出，返回 [{'pid','serial','product'}, ...]。"""
    entries: list[dict[str, str]] = []
    for line in (output or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pid, serial, product = (part.strip() for part in parts)
        if not pid or not serial:
            continue
        entries.append({"pid": pid, "serial": serial, "product": product})
    return entries


def rockusb_loader_serials(
    probe_output: str,
    exclude_serials: Iterable[str] = (),
    loader_pids: Iterable[str] | None = None,
) -> list[str]:
    """提取处于 Loader/MaskROM 烧写模式的设备序列号。

    两层判定：sysfs 条目 PID 必须属于烧写模式 PID 集合（loader_pids，
    缺省为平台配置中 VID 2207 的 PID），且序列号不在 adb（含
    unauthorized/offline 等任意状态）或 fastboot 枚举中。调用方必须先
    合并这些序列号到 exclude_serials，避免把健康的 adb 设备误判为
    可烧写。
    """
    excluded = {str(serial) for serial in (exclude_serials or ()) if str(serial).strip()}
    pids = (
        {str(pid).strip().lower() for pid in loader_pids if str(pid).strip()}
        if loader_pids is not None
        else rockusb_loader_vid_pids()
    )
    serials: list[str] = []
    for entry in parse_rockusb_probe_output(probe_output):
        serial = entry["serial"]
        if entry["pid"].lower() not in pids:
            continue
        if serial in excluded or serial in serials:
            continue
        serials.append(serial)
    return serials
