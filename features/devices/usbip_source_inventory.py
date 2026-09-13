"""USB/IP 来源设备清单（source inventory）。

从 ``usbip.py`` 抽出的清单模块：在不 bind 的前提下枚举来源主机上
可导出的 Android 设备。Windows 路径解析 ``usbipd list`` + PnP 实例
身份；Ubuntu 路径直接读 udev 清单。每台设备合并：

- transport 字段：busid / vid_pid / current_busid；
- 物理身份（``resolve_physical_device_identity``）；
- serial 归因（VID:PID → serial，多设备同 VID:PID 无法唯一映射时
  置空）。

实现为模块级函数，注入 ``manager``（SSH 执行、``_find_android_devices_linux``
等可覆盖入口），与 ``usbip_transaction`` / ``usbip_source_sessions``
的依赖注入风格一致。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from foundation.networking import parse_host_address, split_host_port

from .physical_identity import resolve_physical_device_identity
from .usb import (
    configured_usbip_vid_pids,
    parse_usbipd_android_busids,
)
from .usbip_identity import (
    query_usbipd_busid_instance_ids,
    query_windows_usb_identities,
)
from .usbip_protocol import parse_adb_device_states


logger = logging.getLogger(__name__)


def list_source_devices(
    manager,
    device_host: str,
    device_password: str | None = None,
) -> dict[str, Any]:
    """List Android USB/IP busids on a Windows source without binding."""
    config = manager.config_manager.load_config()
    password = (
        device_password
        or manager.config_manager.find_device_host_password(device_host, config)
        or config.get("device_pswd", "")
    )
    if not password:
        return {"success": False, "error": f"未找到 {device_host} 的SSH凭据"}
    username, hostname = parse_host_address(device_host)
    ssh_hostname, ssh_port = split_host_port(hostname)
    ssh = manager._create_windows_ssh(ssh_hostname, username, password, ssh_port)
    if not ssh:
        return {"success": False, "error": f"SSH连接失败到 {device_host}"}
    try:
        source_os = manager._detect_source_os(ssh)
        if source_os not in ("windows", "linux"):
            return {"success": False, "error": "USB/IP仅支持Windows或Ubuntu主机"}
        if source_os == "linux":
            devices = _list_ubuntu_source_devices(manager, ssh, device_host, config)
            return {
                "success": True,
                "device_host": device_host,
                "source_os": manager._source_os_public(source_os),
                "devices": devices,
            }

        installed, _version = manager.check_usbipd_installed(ssh)
        if not installed:
            from .usbipd_setup import usbipd_not_installed_error

            return usbipd_not_installed_error()
        output = _usbipd_list_output(manager.ssh_manager, ssh)
        busids = parse_usbipd_android_busids(
            output, configured_usbip_vid_pids(config)
        )
        labels = {}
        vid_pid_by_busid: dict[str, str] = {}
        for line in output.splitlines():
            stripped = line.strip()
            parts = stripped.split()
            if parts and parts[0] in busids:
                clean = re.sub(r"\s+", " ", stripped)
                labels[parts[0]] = clean
                vp = re.search(r"([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})", clean)
                if vp:
                    vid_pid_by_busid[parts[0]] = f"{vp[1].lower()}:{vp[2].lower()}"
        serial_by_vid_pid = query_windows_usb_serials(
            manager.ssh_manager,
            ssh,
            {
                value.split(":", 1)[0]
                for value in vid_pid_by_busid.values()
            },
        )
        identity_by_vid_pid = query_windows_usb_identities(
            manager.ssh_manager,
            ssh,
            {
                value.split(":", 1)[0]
                for value in vid_pid_by_busid.values()
            },
        )
        pnp_instance_by_busid = query_usbipd_busid_instance_ids(
            manager.ssh_manager, ssh
        )
        serial_by_busid = {
            busid: (
                serial_by_vid_pid.get(vid_pid_by_busid[busid], "")
                or (
                    serial_by_vid_pid.get("*", "")
                    if len(busids) == 1 else ""
                )
            )
            for busid in busids
            if busid in vid_pid_by_busid
        }
        if len(busids) == 1 and not serial_by_busid.get(busids[0]):
            adb_serials = query_windows_adb_serials(manager.ssh_manager, ssh)
            if len(adb_serials) == 1:
                serial_by_busid[busids[0]] = adb_serials[0]
        devices: list[dict[str, Any]] = []
        for item in busids:
            vid_pid = vid_pid_by_busid.get(item, "")
            pnp_instance_id = pnp_instance_by_busid.get(item, "")
            identity = (
                identity_by_vid_pid.get(
                    f"pnp:{pnp_instance_id.casefold()}", {}
                )
                if pnp_instance_id
                else {}
            ) or identity_by_vid_pid.get(vid_pid, {})
            android_serial = serial_by_busid.get(item, "")
            physical = resolve_physical_device_identity(
                source_host=device_host,
                current_usb_busid=item,
                logical_android_serial=android_serial,
                usb_serial=identity.get("usb_serial", ""),
                container_id=identity.get("container_id", ""),
                pnp_instance_id=(
                    identity.get("pnp_instance_id", "")
                    or pnp_instance_id
                ),
                location_path=identity.get("location_path", ""),
                vid_pid=vid_pid,
            )
            devices.append({
                "busid": item,
                "serial": android_serial,
                "logical_device_id": (
                    android_serial
                    or identity.get("pnp_instance_id", "")
                    or item
                ),
                **physical.to_dict(),
                "vid_pid": vid_pid,
                # Backward-compatible alias; current_usb_busid is the new
                # explicit transport field.
                "current_busid": item,
                "label": _append_serial(
                    labels.get(item, item), android_serial
                ),
            })
        return {
            "success": True,
            "device_host": device_host,
            "source_os": manager._source_os_public(source_os),
            "devices": devices,
        }
    finally:
        ssh.close()


def _list_ubuntu_source_devices(
    manager, ssh, device_host: str, config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build the source-device inventory for an Ubuntu/Linux source host."""
    items = manager._find_android_devices_linux(ssh, config)
    devices: list[dict[str, Any]] = []
    for item in items:
        android_serial = item.get("serial", "")
        physical = resolve_physical_device_identity(
            source_host=device_host,
            current_usb_busid=item["busid"],
            logical_android_serial=android_serial,
            usb_serial=android_serial,
            location_path=item.get("location_path", ""),
            vid_pid=item.get("vid_pid", ""),
        )
        devices.append({
            "busid": item["busid"],
            "serial": android_serial,
            "logical_device_id": android_serial or item["busid"],
            **physical.to_dict(),
            "vid_pid": item.get("vid_pid", ""),
            "current_busid": item["busid"],
            "label": _append_serial(item.get("label", ""), android_serial),
        })
    return devices


def query_windows_usb_serials(
    ssh_manager,
    ssh,
    vendor_ids: set[str] | None = None,
) -> dict[str, str]:
    """Query Windows for USB device serials, keyed by ``vid:pid``.

    Parses ``Get-PnpDevice`` output to extract device IDs like
    ``USB\\VID_xxxx&PID_yyyy\\SERIAL``. When multiple devices share the same
    VID:PID the value is cleared (``""``) since the serial cannot be
    uniquely mapped back to a busid.
    """
    ps = (
        "Get-PnpDevice -PresentOnly -Class USB | "
        "ForEach-Object { $_.InstanceId }"
    )
    try:
        result = ssh_manager.execute_command(
            ssh, f'powershell -NoProfile -Command "{ps}"', timeout=15
        )
    except Exception:
        return {}
    if not result.ok or not result.stdout:
        return {}
    raw: dict[str, list[str]] = {}
    candidates: list[str] = []
    for line in result.stdout.splitlines():
        match = re.search(
            r"USB\\VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})"
            r"([^\\]*)\\(.+)",
            line.strip(),
        )
        if not match:
            continue
        vid, pid, interface, rest = match.groups()
        vid = vid.lower()
        pid = pid.lower()
        if vendor_ids and vid not in vendor_ids:
            continue
        # Interface instance IDs (MI_XX) are Windows-generated values, not
        # stable Android serials.
        if "&MI_" in interface.upper():
            continue
        serial = rest.strip().split("&")[0].strip()
        if not serial or serial.startswith(("REV_", "MI_")):
            continue
        raw.setdefault(f"{vid}:{pid}", []).append(serial)
        candidates.append(serial)
    result_map = {
        key: (values[0] if len(set(values)) == 1 else "")
        for key, values in raw.items()
    }
    unique_candidates = set(candidates)
    if len(unique_candidates) == 1:
        # Android changes PID across adb/recovery/rockusb modes. When the
        # selected USB/IP inventory contains exactly one busid, this
        # vendor-scoped fallback still maps that physical device safely.
        result_map["*"] = next(iter(unique_candidates))
    return result_map


def query_windows_adb_serials(ssh_manager, ssh) -> list[str]:
    """Return stable Android serials visible to Windows ADB."""
    try:
        result = ssh_manager.execute_command(
            ssh,
            "adb devices",
            timeout=15,
        )
    except Exception:
        return []
    if not result.ok:
        logger.debug(
            "[USB/IP] Windows adb inventory failed: %s",
            (result.stderr or result.stdout or "").strip(),
        )
        return []
    states = parse_adb_device_states(result.stdout)
    return sorted({
        serial
        for serial, state in states.items()
        if state in {
            "device",
            "recovery",
            "sideload",
            "unauthorized",
            "offline",
        }
    })


def _append_serial(label: str, serial: str | None) -> str:
    if not serial:
        return label
    return f"{label}  [{serial}]"


def _usbipd_list_output(ssh_manager, ssh) -> str:
    # usbipd list 需要 PTY 才会返回完整设备表。
    result = ssh_manager.execute_command(
        ssh, "usbipd list", timeout=15, get_pty=True
    )
    output = "\n".join(
        part for part in (result.stdout, result.stderr) if part
    )
    logger.info("USB/IP devices (code=%s):\n%s", result.code, output)
    return output


__all__ = [
    "list_source_devices",
    "query_windows_adb_serials",
    "query_windows_usb_serials",
]
