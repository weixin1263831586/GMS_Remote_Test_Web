"""Rockchip Loader/MaskROM 烧写模式设备的 USB 枚举。

烧写失败后设备常停留在 Loader/MaskROM 模式，对 adb/fastboot 不可见。
通过测试主机 sysfs 枚举 VID 2207 设备并读取 USB 序列号，
让设备列表与固件烧写链路都能继续定位这类设备。

判定必须同时满足 VID 与烧写模式身份。烧写模式身份有两层：
1. 烧写模式 PID 以平台配置 usbip_vid_pids 中 VID 2207 的条目为准
   （默认 2207:351a，见 features/devices/usb.py）；
2. ROM 级产品名标记（``USB download gadget`` / ``MaskROM`` /
   ``Rockusb Device``）随 Rockchip BootROM 固定，不随 SoC 改变。
PID 逐代不同（RK3572=351a、RK3576=350e，RK3562 又是新的值），新 SoC
仅靠 PID 清单会漏判；标记层保证未知 SoC 的 Loader 也能被识别。
标记层带健康功能 PID 否定清单（0006/0007 等）：健康设备的 iProduct
由厂商客制，可能恰含标记子串，PID 是硬约束，优先于标记。
Rockchip 健康设备会以多种 USB 功能枚举（如 2207:0006 ADB、
2207:0007 其他功能），这些 PID 永远不会出现在 adb 枚举里，且产品名
不带上述标记；若只按 VID 判定会把健康设备永久误报成 Loader。
注意：MaskROM 设备在 sysfs 中常无 iSerial，此类条目无法建立
序列号身份，会被解析层丢弃（可识别的 MaskROM 仅限带序列号的条目）；
USB/IP 重挂载用的产品名标记清单另见 features/devices/usb.py 的
ANDROID_USBIP_MARKERS（用途不同，保持独立）。
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

# Rockchip BootROM 烧写模式的 USB 产品名标记（iProduct）。这些字符串
# 属于 BootROM 本身，跨 SoC 世代稳定：RK3572 Loader 在 sysfs 中为
# "Rockusb Device"，RK3576 为 "USB download gadget"，MaskROM 为
# "MaskROM"（实机记录见 test_rockusb_enumeration 与
# docs/usbip/firmware-flashing.md）。健康 Android 枚举（如
# "SSI 17 on ARM64"）永远不会带这些标记。
ROCKUSB_LOADER_PRODUCT_MARKERS = (
    "usb download gadget",
    "maskrom",
    "rockusb device",
)

# Rockchip 健康运行态的 USB 功能 PID（VID 2207）。0006=ADB，0007=其他
# 功能枚举（现场案例 c3d9b8674f4b94f6 以 0007 健康枚举且永不出现于
# adb，见 test_rockusb_enumeration）。健康设备的 iProduct 由厂商客制，
# 可能恰好包含标记子串；PID 是 USB 功能身份的硬约束，因此标记层
# （以及任何显式误配置）都不得把这些 PID 判为烧写模式。
ROCKUSB_HEALTHY_FUNCTION_PIDS = frozenset({"0006", "0007"})


def is_rockusb_loader_product(product: str) -> bool:
    """判断 sysfs ``product`` 是否为 BootROM 烧写模式标记。"""
    normalized = str(product or "").strip().lower()
    return any(marker in normalized for marker in ROCKUSB_LOADER_PRODUCT_MARKERS)


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
    缺省为平台配置中 VID 2207 的 PID），或产品名命中 BootROM 烧写
    标记（见 ROCKUSB_LOADER_PRODUCT_MARKERS）；PID 随 SoC 逐代变化
    （RK3572=351a、RK3576=350e），新 SoC 的 Loader 依靠标记层兜底，
    无需先补 PID。此外序列号不得出现在 adb（含 unauthorized/offline
    等任意状态）或 fastboot 枚举中；调用方必须先合并这些序列号到
    exclude_serials，避免把健康的 adb 设备误判为可烧写。
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
        pid = entry["pid"].lower()
        if pid in ROCKUSB_HEALTHY_FUNCTION_PIDS:
            # 健康运行态功能枚举（ADB/其他功能）永远不是烧写目标，
            # 即使产品名恰好命中标记或 PID 被误配置进 loader_pids。
            continue
        if pid not in pids and not is_rockusb_loader_product(entry["product"]):
            continue
        if serial in excluded or serial in serials:
            continue
        serials.append(serial)
    return serials
