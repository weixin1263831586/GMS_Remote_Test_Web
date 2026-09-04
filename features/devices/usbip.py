"""
USB/IP - 核心业务逻辑

特性：
- USB/IP设备转发
- Windows来源主机（usbipd-win）与Ubuntu/Linux来源主机（用户态usbipd）支持
- 设备绑定/解绑
"""

import logging
import re
import shlex
import time
from typing import Any

from foundation.network_quality import probe_tcp_quality
from foundation.networking import parse_host_address, split_host_port
from foundation.ssh_security import configure_strict_host_keys

from .physical_identity import resolve_physical_device_identity
from .ssh_credentials import find_device_host_password
from .usb import (
    ANDROID_USBIP_MARKERS,
    configured_usbip_vid_pids,
    parse_usbipd_android_busids,
    parse_usbipd_busid_statuses,
)
from .usbip_identity import (
    query_usbipd_busid_instance_ids,
    query_windows_usb_identities,
)
from .usbip_linux_source import (
    ensure_ubuntu_usbip_server,
    install_ubuntu_usbipd,
    list_ubuntu_usb_devices,
    source_os_label,
    stop_ubuntu_usbip_server,
)
from .usbip_readiness import wait_for_adb_serial_ready
from .usbip_transaction import (
    USBIP_PORT_COMMAND,
    parse_usbip_port_entries,
    rollback_ubuntu_attachments,
    usbip_attached_ports,
    usbip_error,
)
from .usbip_transaction import (
    detach_ubuntu_usbip_ports as _detach_ports_impl,
)
from .usbip_transaction import (
    rollback_windows_binds as _rollback_windows_binds_impl,
)
from .usbipd_setup import (
    USBIPD_INSTALL_CMD,
    USBIPD_INSTALL_GUIDE,
    check_usbipd_installed,
    install_usbipd,
    usbipd_not_installed_error,
)
from .utils import DeviceUtils


logger = logging.getLogger(__name__)

# usbipd-win 在驱动切换后可能接受 import（attach 返回 0），
# 却在 vhci 完成 USB 枚举前立即释放会话。实机上同一 BUSID
# 后续 attach 即可稳定；仅重试这种“命令成功但端口未稳定”
# 的目标，确定性命令失败不会反复执行。
USBIP_ATTACH_STABILIZATION_ATTEMPTS = 3
USBIP_ATTACH_PORT_POLL_ATTEMPTS = 6

__all__ = [
    "USBIPD_INSTALL_CMD",
    "USBIPD_INSTALL_GUIDE",
    "USBIPManager",
    "detach_ubuntu_usbip_ports",
    "find_device_host_password",
    "parse_adb_device_states",
    "parse_fastboot_devices",
    "parse_usbip_port_entries",
    "usbip_manager",
    "wait_for_adb_serial_ready",
]


def detach_ubuntu_usbip_ports(
    ssh,
    remote_host: str | None = '127.0.0.1',
    detach_all: bool = False,
    busids: list[str] | None = None,
) -> list[str]:
    """Detach Ubuntu usbip ports (compat wrapper over usbip_transaction)."""
    return _detach_ports_impl(
        usbip_manager.ssh_manager, ssh, remote_host, detach_all, busids
    )

def _usbip_attached_ports(ssh) -> set[str]:
    """Return the set of currently attached usbip port numbers (as strings)."""
    return usbip_attached_ports(usbip_manager.ssh_manager, ssh)


def parse_adb_device_states(output: str) -> dict[str, str]:
    """Parse all adb-visible serials, including recovery/offline/unauthorized."""
    states: dict[str, str] = {}
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    return states


def parse_fastboot_devices(output: str) -> list[str]:
    """Parse fastboot device serials."""
    return DeviceUtils.parse_fastboot_devices(output)


class USBIPManager:
    """Manages the full USB/IP lifecycle: Windows-side bind, Ubuntu-side attach, and protocol probing."""

    def __init__(self, ssh_manager=None, config_manager=None):
        self.ssh_manager = ssh_manager
        self.config_manager = config_manager
        self.active_connections: dict[str, Any] = {}  # {client_id: connection_info}
        self.device_sources: dict[str, dict[str, Any]] = {}  # {device_id: source_info}

    @staticmethod
    def _source_os_public(source_os: str) -> str:
        """Map internal OS kind to the public source_os API value."""
        return {"windows": "windows", "linux": "ubuntu"}.get(source_os, "")

    def _detect_source_os(self, ssh) -> str:
        """Classify a source host: 'windows', 'linux' or '' (unsupported)."""
        if self._is_windows_host(ssh):
            return "windows"
        try:
            result = self.ssh_manager.execute_command(
                ssh, "uname -s", timeout=8,
            )
        except Exception:
            return ""
        if result.ok and "linux" in (result.stdout or "").strip().lower():
            return "linux"
        return ""

    def start_usbip(
        self,
        device_host: str,
        device_password: str | None = None,
        usbip_attach_host: str | None = None,
        selected_busids: list[str] | None = None,
        adb_server_socket: str | None = None,
        allow_transport_only: bool = False,
    ) -> dict[str, Any]:
        """Start USB/IP forwarding to the Ubuntu host over the given Windows device_host.

        Args:
            device_host: Windows host as user@ip (password auto-resolved if omitted).
            usbip_attach_host: override the IP Ubuntu attaches from (defaults to device host).
        """
        try:
            config = self.config_manager.load_config()

            if not device_host:
                device_host = config.get('device_host', '')

            if not device_host or '@' not in device_host:
                return {'success': False, 'error': '无效的设备主机地址'}

            # 自动查找密码
            if not device_password:
                device_password = self.config_manager.find_device_host_password(
                    device_host,
                    config
                )

            if not device_password:
                device_password = config.get('device_pswd', '')

            if not device_password:
                return {
                    'success': False,
                    'error': f'未找到 {device_host} 的SSH凭据',
                    'instructions': '请先在登录页面输入SSH密码'
                }

            # 连接来源主机（Windows 或 Ubuntu/Linux）
            username, hostname = parse_host_address(device_host)
            ssh_hostname, ssh_port = split_host_port(hostname)
            usbip_attach_host = usbip_attach_host or config.get('usbip_attach_host') or ssh_hostname
            source_ssh = self._create_windows_ssh(ssh_hostname, username, device_password, ssh_port)

            if not source_ssh:
                return {'success': False, 'error': f'SSH连接失败到 {device_host}'}

            try:
                # 检查系统类型：Windows（usbipd-win）或 Ubuntu/Linux（用户态 usbipd）
                source_os = self._detect_source_os(source_ssh)
                if source_os not in ('windows', 'linux'):
                    return {'success': False, 'error': 'USB/IP仅支持Windows或Ubuntu主机'}

                if source_os == 'windows':
                    installed, _version = self.check_usbipd_installed(source_ssh)
                    if not installed:
                        return usbipd_not_installed_error()

                network_quality = probe_tcp_quality(usbip_attach_host, 3240)
                source_txn: dict[str, Any] = {'kind': source_os}

                if source_os == 'windows':
                    if not network_quality["reachable"]:
                        return usbip_error(
                            "USBIP_TCP_UNREACHABLE",
                            f"无法连接USB/IP来源 {usbip_attach_host}:3240",
                            retryable=True,
                            remediation="请检查usbipd服务、TCP 3240防火墙和网络路由。",
                            network_quality=network_quality,
                        )

                    adb_release = self._stop_windows_adb(source_ssh)
                    if not adb_release.get("success"):
                        return usbip_error(
                            "USBIP_ADB_RELEASE_FAILED",
                            f"释放Windows ADB占用失败: {adb_release.get('error')}",
                            remediation="请关闭占用设备的Android Studio、scrcpy或其他ADB任务后重试。",
                        )

                    discovered_busids = self._find_android_devices(source_ssh, config)
                    requested = [str(item) for item in selected_busids or []]
                    busids = requested or discovered_busids
                    if requested:
                        allowed_busids = set(discovered_busids)
                        if allow_transport_only:
                            # During an intentional Fastboot/Loader transition the
                            # USB PID and Windows label may be new to this release.
                            # A persisted assignment identifies the physical port;
                            # still require that BUSID to be currently connected.
                            source_output = self._usbipd_list_output(source_ssh)
                            allowed_busids.update(
                                parse_usbipd_busid_statuses(source_output)
                            )
                        if not set(requested).issubset(allowed_busids):
                            return {'success': False, 'error': '选择的USB设备已不可用，请刷新后重试'}
                    if not busids:
                        return {'success': False, 'error': '未找到Android设备'}

                    newly_bound: list[str] = []
                    bound = self._bind_devices(
                        source_ssh, busids, track_newly_bound=newly_bound
                    )
                    source_txn['newly_bound'] = newly_bound

                    if not bound:
                        return {'success': False, 'error': '设备绑定失败'}
                else:
                    # Ubuntu/Linux 来源：用户态 usbipd 服务端按 serial/vid 导出。
                    inventory = self._find_android_devices_linux(source_ssh, config)
                    discovered_busids = [item['busid'] for item in inventory]
                    requested = [str(item) for item in selected_busids or []]
                    busids = requested or discovered_busids
                    all_inventory = inventory
                    if requested:
                        allowed_busids = set(discovered_busids)
                        if allow_transport_only:
                            # Loader/MaskROM 等协议态可能更换 VID:PID，允许
                            # 当前主机上任意存在的 BUSID（含非 Android 过滤项）。
                            all_inventory = self._find_android_devices_linux(
                                source_ssh, config, include_all=True,
                            )
                            allowed_busids.update(
                                item['busid'] for item in all_inventory
                            )
                        if not set(requested).issubset(allowed_busids):
                            return {'success': False, 'error': '选择的USB设备已不可用，请刷新后重试'}
                    if not busids:
                        return {'success': False, 'error': '未找到Android设备'}

                    # transport-only 时设备可能处于 Loader/MaskROM，不在
                    # Android 过滤清单里：选择集必须从全量清单（all_inventory）
                    # 取，否则 serial/vid 过滤器全空，usbipd 冷启动直接失败。
                    # 总线 ID 过滤器缺失时直接报错，不做 VID 扩大匹配
                    # （会把源主机上所有 Rockchip 设备一起导出）。
                    selected_inventory = [
                        item for item in all_inventory
                        if item['busid'] in set(busids)
                    ]
                    export_serials = [
                        item['serial'] for item in selected_inventory
                        if item.get('serial')
                    ]
                    export_vids = sorted({
                        item['vid_pid'].split(':', 1)[0]
                        for item in selected_inventory
                        if item.get('vid_pid')
                    })
                    server = ensure_ubuntu_usbip_server(
                        self.ssh_manager,
                        source_ssh,
                        serials=export_serials,
                        vids=export_vids if not export_serials else (),
                        # USB/IP 的数据面 attach 来自 Worker/平台 Ubuntu 侧，
                        # 白名单需要逐 Worker 解析出口 IP（在 Worker 上执行
                        # ip route get），不能拿来源地址充数。
                        allow_worker_hosts=[
                            usbip_attach_host,
                            config.get('device_host') or '',
                        ],
                        worker_ssh_factory=lambda _host: self.ssh_manager.get_connection(config),
                    )
                    source_txn['started'] = bool(server.get('started'))
                    if not server.get('success'):
                        return usbip_error(
                            "USBIP_SOURCE_SERVER_FAILED",
                            f"Ubuntu来源USB/IP服务启动失败: {server.get('error')}",
                            retryable=True,
                            remediation=(
                                "请检查来源主机usbipd部署、/dev/bus/usb 权限"
                                "及是否有ADB进程占用设备。"
                            ),
                            detail=server.get('detail') or server.get('install_guide') or '',
                        )

                    # 服务端就绪后重新探测 TCP 3240。
                    network_quality = probe_tcp_quality(usbip_attach_host, 3240)
                    if not network_quality["reachable"]:
                        self._rollback_source_side(source_ssh, source_txn)
                        return usbip_error(
                            "USBIP_TCP_UNREACHABLE",
                            f"无法连接USB/IP来源 {usbip_attach_host}:3240",
                            retryable=True,
                            remediation="请检查usbipd服务、TCP 3240防火墙和网络路由。",
                            network_quality=network_quality,
                        )

                # 连接Ubuntu并attach设备
                ubuntu_ssh = self.ssh_manager.get_connection(config)
                if not ubuntu_ssh:
                    rollback_complete = self._rollback_source_side(
                        source_ssh, source_txn
                    )
                    return usbip_error(
                        "USBIP_ATTACH_FAILED",
                        '无法连接Ubuntu主机',
                        retryable=True,
                        remediation="请检查Ubuntu主机SSH配置后重试。",
                        rollback_complete=rollback_complete,
                    )

                try:
                    # 确保vhci驱动已加载
                    self._ensure_vhci_driver(ubuntu_ssh)
                    # 只清理本次要 attach 的 (host, busid) vhci 端口：
                    # 同一 Windows 主机上其他设备的 USB/IP 会话（如正在跑
                    # CTS 的另一台手机）不能被连带 detach。
                    detach_ubuntu_usbip_ports(
                        ubuntu_ssh, usbip_attach_host, detach_all=False,
                        busids=busids,
                    )

                    # Attach设备
                    attached, device_list = self._attach_devices(
                        ubuntu_ssh,
                        usbip_attach_host,
                        busids,
                        adb_server_socket=adb_server_socket,
                        allow_transport_only=allow_transport_only,
                    )

                    if not attached:
                        target_rollback_complete = rollback_ubuntu_attachments(
                            self.ssh_manager,
                            ubuntu_ssh,
                            usbip_attach_host,
                            busids,
                        )
                        self.ssh_manager.return_connection(ubuntu_ssh)
                        source_rollback_complete = self._rollback_source_side(
                            source_ssh, source_txn
                        )
                        return usbip_error(
                            "USBIP_ATTACH_FAILED",
                            'USB/IP attach 失败，未成功连接任何设备',
                            retryable=True,
                            remediation="请重试连接；若持续失败请检查Windows usbipd导出/TCP 3240及Ubuntu vhci状态。",
                            rollback_complete=(
                                target_rollback_complete
                                and source_rollback_complete
                            ),
                            devices=[],
                            device_list=[]
                        )

                    protocol_status = self._scope_protocol_status(
                        self.probe_protocol_status(
                            ubuntu_ssh,
                            adb_server_socket=adb_server_socket,
                        ),
                        device_list,
                    )

                    # 更新设备来源记录。只有 ADB serial 稳定后才按 serial 记录来源；
                    # fastboot/recovery/reboot 中 serial 可能暂时不可见或状态不是 device。
                    for device_id in device_list:
                        self.device_sources[device_id] = {
                            'source': device_host,
                            'source_os': self._source_os_public(source_os),
                            'timestamp': time.time()
                        }

                    self.ssh_manager.return_connection(ubuntu_ssh)

                    return {
                        'success': True,
                        'message': self._build_attach_message(attached, device_list, protocol_status),
                        'devices': attached,
                        'device_list': device_list,
                        'transport_connected': True,
                        'transport_state': 'attached',
                        'protocol_state': protocol_status.get('mode') or 'unknown',
                        'source_os': self._source_os_public(source_os),
                        'source_os_label': source_os_label(source_os),
                        'readiness': (
                            'test_ready' if protocol_status.get('mode') in {'adb', 'fastboot', 'recovery'}
                            else 'protocol_ready' if protocol_status.get('mode') not in {'unknown', 'offline', 'unauthorized'}
                            else 'transport_ready'
                        ),
                        'network_quality': network_quality,
                        'protocol_status': protocol_status,
                    }

                except Exception as e:
                    target_rollback_complete = rollback_ubuntu_attachments(
                        self.ssh_manager,
                        ubuntu_ssh,
                        usbip_attach_host,
                        busids,
                    )
                    ubuntu_ssh.close()
                    logger.error(f"Error in Ubuntu attach: {e}")
                    source_rollback_complete = self._rollback_source_side(
                        source_ssh, source_txn
                    )
                    return usbip_error(
                        "USBIP_ATTACH_FAILED",
                        str(e),
                        rollback_complete=(
                            target_rollback_complete
                            and source_rollback_complete
                        ),
                    )

            except Exception as e:
                logger.error(f"Error in source side: {e}")
                return {'success': False, 'error': str(e)}
            finally:
                source_ssh.close()

        except Exception as e:
            logger.error(f"Error in start_usbip: {e}")
            return {'success': False, 'error': str(e)}

    def list_source_devices(
        self, device_host: str, device_password: str | None = None
    ) -> dict[str, Any]:
        """List Android USB/IP busids on a Windows source without binding."""
        config = self.config_manager.load_config()
        password = (
            device_password
            or self.config_manager.find_device_host_password(device_host, config)
            or config.get("device_pswd", "")
        )
        if not password:
            return {"success": False, "error": f"未找到 {device_host} 的SSH凭据"}
        username, hostname = parse_host_address(device_host)
        ssh_hostname, ssh_port = split_host_port(hostname)
        ssh = self._create_windows_ssh(ssh_hostname, username, password, ssh_port)
        if not ssh:
            return {"success": False, "error": f"SSH连接失败到 {device_host}"}
        try:
            source_os = self._detect_source_os(ssh)
            if source_os not in ("windows", "linux"):
                return {"success": False, "error": "USB/IP仅支持Windows或Ubuntu主机"}
            if source_os == "linux":
                devices = self._list_ubuntu_source_devices(ssh, device_host, config)
                return {
                    "success": True,
                    "device_host": device_host,
                    "source_os": self._source_os_public(source_os),
                    "devices": devices,
                }

            installed, _version = self.check_usbipd_installed(ssh)
            if not installed:
                return usbipd_not_installed_error()
            output = self._usbipd_list_output(ssh)
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
            serial_by_vid_pid = self._query_windows_usb_serials(
                ssh,
                {
                    value.split(":", 1)[0]
                    for value in vid_pid_by_busid.values()
                },
            )
            identity_by_vid_pid = query_windows_usb_identities(
                self.ssh_manager,
                ssh,
                {
                    value.split(":", 1)[0]
                    for value in vid_pid_by_busid.values()
                },
            )
            pnp_instance_by_busid = query_usbipd_busid_instance_ids(
                self.ssh_manager, ssh
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
                adb_serials = self._query_windows_adb_serials(ssh)
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
                    "label": self._append_serial(
                        labels.get(item, item), android_serial
                    ),
                })
            return {
                "success": True,
                "device_host": device_host,
                "source_os": self._source_os_public(source_os),
                "devices": devices,
            }
        finally:
            ssh.close()

    def _list_ubuntu_source_devices(
        self, ssh, device_host: str, config: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Build the source-device inventory for an Ubuntu/Linux source host."""
        items = self._find_android_devices_linux(ssh, config)
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
                "label": self._append_serial(item.get("label", ""), android_serial),
            })
        return devices

    def _query_windows_usb_serials(
        self,
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
            result = self.ssh_manager.execute_command(
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
        result = {
            key: (values[0] if len(set(values)) == 1 else "")
            for key, values in raw.items()
        }
        unique_candidates = set(candidates)
        if len(unique_candidates) == 1:
            # Android changes PID across adb/recovery/rockusb modes. When the
            # selected USB/IP inventory contains exactly one busid, this
            # vendor-scoped fallback still maps that physical device safely.
            result["*"] = next(iter(unique_candidates))
        return result

    def _query_windows_adb_serials(self, ssh) -> list[str]:
        """Return stable Android serials visible to Windows ADB."""
        try:
            result = self.ssh_manager.execute_command(
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

    @staticmethod
    def _append_serial(label: str, serial: str | None) -> str:
        if not serial:
            return label
        return f"{label}  [{serial}]"

    def probe_source_os(
        self,
        device_host: str,
        device_password: str | None = None,
    ) -> dict[str, Any]:
        """Detect the OS of a source host via SSH; used for dropdown labels."""
        host = str(device_host or "").strip()
        if not host:
            return {"source_os": "", "error": "缺少设备主机地址"}
        config = self.config_manager.load_config()
        password = (
            device_password
            or self.config_manager.find_device_host_password(host, config)
            or config.get("device_pswd", "")
        )
        if not password:
            return {"source_os": "", "error": f"未找到 {host} 的SSH凭据"}
        username, hostname = parse_host_address(host)
        ssh_hostname, ssh_port = split_host_port(hostname)
        ssh = self._create_windows_ssh(ssh_hostname, username, password, ssh_port)
        if not ssh:
            return {"source_os": "", "error": f"SSH连接失败到 {host}"}
        try:
            return {"source_os": self._detect_source_os(ssh)}
        finally:
            ssh.close()

    def ensure_source_export_ready(
        self,
        device_host: str,
        busids: list[str] | None = None,
        device_password: str | None = None,
    ) -> dict[str, Any]:
        """Start the on-demand usbipd server for Ubuntu sources.

        Windows 来源的 usbipd-win 服务常驻，本方法为 no-op；Ubuntu 来源
        的用户态 usbipd 进程按需启动，用于 attach 前的 TCP 3240 预检
        补偿等场景。
        """
        config = self.config_manager.load_config()
        password = (
            device_password
            or self.config_manager.find_device_host_password(device_host, config)
            or config.get("device_pswd", "")
        )
        if not password:
            return {"success": False, "error": f"未找到 {device_host} 的SSH凭据"}
        username, hostname = parse_host_address(device_host)
        ssh_hostname, ssh_port = split_host_port(hostname)
        ssh = self._create_windows_ssh(ssh_hostname, username, password, ssh_port)
        if not ssh:
            return {"success": False, "error": f"SSH连接失败到 {device_host}"}
        try:
            if self._detect_source_os(ssh) != "linux":
                return {"success": True, "started": False}
            inventory = self._find_android_devices_linux(ssh, config)
            selected_busids = {str(item or "") for item in busids or []}
            selected = [
                item for item in inventory
                if not selected_busids or item["busid"] in selected_busids
            ]
            server = ensure_ubuntu_usbip_server(
                self.ssh_manager,
                ssh,
                serials=[
                    item["serial"] for item in selected if item.get("serial")
                ],
                vids=sorted({
                    item["vid_pid"].split(":", 1)[0]
                    for item in selected if item.get("vid_pid")
                }),
                allow_worker_hosts=[
                    config.get('usbip_attach_host') or ssh_hostname
                ],
                # Worker 侧凭据：平台配置的 Ubuntu/Worker 主机（用于在其上
                # 执行 ip route get 解析出口 IP）。
                worker_ssh_factory=(
                    lambda _host: self.ssh_manager.get_connection(config)
                ),
            )
            return {
                "success": bool(server.get("success")),
                "started": bool(server.get("started")),
                "detail": server.get("error") or server.get("detail") or "",
                "install_guide": server.get("install_guide") or "",
            }
        finally:
            ssh.close()

    def bind_source_devices(
        self,
        device_host: str,
        busids: list[str],
        device_password: str | None = None,
    ) -> dict[str, Any]:
        """Bind selected source USB devices for a remote Worker attach."""
        config = self.config_manager.load_config()
        password = (
            device_password
            or self.config_manager.find_device_host_password(device_host, config)
            or config.get("device_pswd", "")
        )
        if not password:
            return {"success": False, "error": f"未找到 {device_host} 的SSH凭据"}
        username, hostname = parse_host_address(device_host)
        ssh_hostname, ssh_port = split_host_port(hostname)
        ssh = self._create_windows_ssh(ssh_hostname, username, password, ssh_port)
        if not ssh:
            return {"success": False, "error": f"SSH连接失败到 {device_host}"}
        try:
            source_os = self._detect_source_os(ssh)
            if source_os not in ("windows", "linux"):
                return {"success": False, "error": "USB/IP仅支持Windows或Ubuntu主机"}
            selected = list(dict.fromkeys(
                str(item or "").strip() for item in busids or []
            ))
            if not selected or any(
                not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", item)
                for item in selected
            ):
                return {"success": False, "error": "无效的USB/IP BUSID"}
            if source_os == "linux":
                inventory = self._find_android_devices_linux(ssh, config)
                unavailable = [
                    item for item in selected
                    if item not in {entry["busid"] for entry in inventory}
                ]
                if unavailable:
                    return {
                        "success": False,
                        "error": (
                            "选择的USB设备已不可用，请刷新后重试: "
                            + ", ".join(unavailable)
                        ),
                    }
                selected_inventory = [
                    item for item in inventory if item["busid"] in set(selected)
                ]
                server = ensure_ubuntu_usbip_server(
                    self.ssh_manager,
                    ssh,
                    serials=[
                        item["serial"] for item in selected_inventory
                        if item.get("serial")
                    ],
                    vids=sorted({
                        item["vid_pid"].split(":", 1)[0]
                        for item in selected_inventory
                        if item.get("vid_pid")
                    }),
                    allow_worker_hosts=[
                        config.get("usbip_attach_host") or ssh_hostname
                    ],
                    # Worker 侧凭据：平台配置的 Ubuntu/Worker 主机（用于
                    # 在其上执行 ip route get 解析出口 IP）。
                    worker_ssh_factory=(
                        lambda _host: self.ssh_manager.get_connection(config)
                    ),
                )
                if not server.get("success"):
                    return {
                        "success": False,
                        "error": f"Ubuntu来源USB/IP服务启动失败: {server.get('error')}",
                        "install_guide": server.get("install_guide"),
                    }
                return {
                    "success": True,
                    "device_host": device_host,
                    "source_host": config.get("usbip_attach_host") or ssh_hostname,
                    "source_os": self._source_os_public(source_os),
                    "busids": selected,
                }

            if not self.check_usbipd_installed(ssh)[0]:
                return {"success": False, "error": "usbipd未安装"}
            available = set(self._find_android_devices(ssh, config))
            unavailable = [item for item in selected if item not in available]
            if unavailable:
                return {
                    "success": False,
                    "error": (
                        "选择的USB设备已不可用，请刷新后重试: "
                        + ", ".join(unavailable)
                    ),
                }
            adb_release = self._stop_windows_adb(ssh)
            if not adb_release.get("success"):
                return {
                    "success": False,
                    "error": f"释放Windows ADB占用失败: {adb_release.get('error')}",
                }
            bound = self._bind_devices(ssh, selected)
            if set(bound) != set(selected):
                missing = [item for item in selected if item not in bound]
                return {
                    "success": False,
                    "error": "部分USB设备绑定失败: " + ", ".join(missing),
                }
            return {
                "success": True,
                "device_host": device_host,
                "source_host": config.get("usbip_attach_host") or ssh_hostname,
                "busids": bound,
            }
        finally:
            ssh.close()

    def detach_source_sessions(
        self,
        device_host: str,
        busids: list[str],
        device_password: str | None = None,
    ) -> dict[str, Any]:
        """Drop stale usbipd exports without removing persistent bindings."""
        selected = [str(item).strip() for item in busids or []]
        if not selected or any(
            not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", item)
            for item in selected
        ):
            return {"success": False, "error": "无效的USB/IP BUSID"}

        config = self.config_manager.load_config()
        password = (
            device_password
            or self.config_manager.find_device_host_password(device_host, config)
            or config.get("device_pswd", "")
        )
        if not password:
            return {"success": False, "error": f"未找到 {device_host} 的SSH凭据"}

        username, hostname = parse_host_address(device_host)
        ssh_hostname, ssh_port = split_host_port(hostname)
        ssh = self._create_windows_ssh(ssh_hostname, username, password, ssh_port)
        if not ssh:
            return {"success": False, "error": f"SSH连接失败到 {device_host}"}

        try:
            source_os = self._detect_source_os(ssh)
            if source_os not in ("windows", "linux"):
                return {"success": False, "error": "USB/IP仅支持Windows或Ubuntu主机"}
            if source_os == "linux":
                # Ubuntu 来源无每设备 usbipd 会话；断开由接入主机侧 vhci
                # detach 完成，来源侧只在整源断开时停止 usbipd 进程。
                return {
                    "success": True,
                    "source_os": self._source_os_public(source_os),
                    "detached_busids": [],
                    "errors": {},
                }
            if not self.check_usbipd_installed(ssh)[0]:
                return {"success": False, "error": "usbipd未安装"}

            detached = []
            errors = {}
            for busid in selected:
                detach_result = self.ssh_manager.execute_command(
                    ssh,
                    f"usbipd detach --busid {busid}",
                    timeout=15,
                )
                detail = (detach_result.stderr or detach_result.stdout or "").strip()
                normalized_detail = detail.lower()
                if detach_result.ok or any(
                    marker in normalized_detail
                    for marker in (
                        "already not attached",
                        "is not attached",
                        "not currently attached",
                        "no devices are currently attached",
                    )
                ):
                    detached.append(busid)
                else:
                    errors[busid] = (
                        detail
                        or f"usbipd detach exited with code {detach_result.code}"
                    )
            return {
                "success": not errors,
                "detached_busids": detached,
                "errors": errors,
                "error": "; ".join(
                    f"{busid}: {detail}" for busid, detail in errors.items()
                ),
            }
        finally:
            ssh.close()

    def stop_usbip(self, client_id: str | None = None) -> dict[str, Any]:
        """Stop USB/IP forwarding for client_id, keeping device-source records for re-attach."""
        try:
            if client_id and client_id in self.active_connections:
                del self.active_connections[client_id]

            return {
                'success': True,
                'message': '✅ USB/IP连接已断开（设备来源保留）'
            }

        except Exception as e:
            logger.error(f"Error in stop_usbip: {e}")
            return {'success': True, 'message': '✅ USB/IP连接已断开'}

    def get_usbip_status(self, client_id: str | None = None) -> dict[str, Any]:
        """Return {connected, device_count}; connected if client_id is active OR any device-source record exists."""
        connected = False

        if client_id and client_id in self.active_connections:
            connected = True

        if not connected and self.device_sources:
            connected = True

        return {
            'connected': connected,
            'device_count': len(self.device_sources)
        }

    # ============ Helpers ============

    def _create_windows_ssh(self, hostname: str, username: str, password: str, port: int = 22):
        try:
            import paramiko
            ssh = paramiko.SSHClient()
            configure_strict_host_keys(ssh)
            ssh.connect(
                hostname=hostname,
                port=port,
                username=username,
                password=password,
                timeout=10
            )
            return ssh
        except Exception as e:
            logger.error(f"Error creating Windows SSH: {e}")
            return None

    def _is_windows_host(self, ssh) -> bool:
        try:
            result = self.ssh_manager.execute_command(ssh, 'ver 2>&1')
            return 'microsoft' in result.stdout.lower() or 'windows' in result.stdout.lower()
        except Exception:
            return False

    def _find_android_devices_linux(
        self, ssh, config: dict[str, Any], include_all: bool = False,
    ) -> list[dict[str, Any]]:
        """Enumerate USB devices on an Ubuntu/Linux source via udev."""
        try:
            return list_ubuntu_usb_devices(
                self.ssh_manager,
                ssh,
                vid_pids=() if include_all else configured_usbip_vid_pids(config),
                markers=() if include_all else ANDROID_USBIP_MARKERS,
            )
        except Exception as e:
            logger.error(f"Error finding Android devices on Ubuntu source: {e}")
            return []

    def _find_android_devices(self, ssh, config: dict[str, Any]) -> list[str]:
        try:
            output = self._usbipd_list_output(ssh)
            devices = parse_usbipd_android_busids(
                output,
                configured_usbip_vid_pids(config),
            )
            logger.info(f"Found USB/IP devices: {devices}")
            return devices

        except Exception as e:
            logger.error(f"Error finding Android devices: {e}")
            return []

    def _usbipd_list_output(self, ssh) -> str:
        # usbipd list 需要 PTY 才会返回完整设备表。
        result = self.ssh_manager.execute_command(
            ssh, "usbipd list", timeout=15, get_pty=True
        )
        output = "\n".join(
            part for part in (result.stdout, result.stderr) if part
        )
        logger.info("USB/IP devices (code=%s):\n%s", result.code, output)
        return output

    def _bind_devices(
        self,
        ssh,
        busids: list[str],
        track_newly_bound: list[str] | None = None,
    ) -> list[str]:
        """Bind USB devices on the Windows source host.

        ``track_newly_bound`` 收集本次调用真正执行 bind 的 busid（不含
        之前已处于 Shared 状态的设备），供 attach 失败时回滚，避免把
        本次事务之外预先存在的共享一并解除。
        """
        bound = []
        for busid in busids:
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", str(busid or "")):
                logger.error("Rejected invalid USB/IP busid: %r", busid)
                continue
            try:
                list_result = self.ssh_manager.execute_command(
                    ssh, f"usbipd list | findstr {busid}"
                )
                if list_result.code not in {0, 1}:
                    logger.error(
                        "Failed to inspect USB/IP device %s: %s",
                        busid,
                        (list_result.stderr or list_result.stdout).strip(),
                    )
                    continue

                # usbipd 状态是整词（STATE 列：Not Shared / Shared / Attached），
                # "Not Shared" 含子串 "Shared"，必须按词边界判定而非 substring。
                if re.search(r'\bShared\b', list_result.stdout) and not re.search(
                    r'\bNot Shared\b', list_result.stdout
                ):
                    logger.info(f"Device {busid} already shared")
                    bound.append(busid)
                    continue
                elif re.search(r'\bAttached\b', list_result.stdout):
                    # Detach first
                    detach_result = self.ssh_manager.execute_command(
                        ssh,
                        f"usbipd detach --busid {busid}",
                        timeout=15,
                    )
                    if not detach_result.ok:
                        logger.error(
                            "Failed to detach USB/IP device %s before bind: %s",
                            busid,
                            (detach_result.stderr or detach_result.stdout).strip(),
                        )
                        continue
                    time.sleep(1)

                # Bind
                bind_result = self.ssh_manager.execute_command(
                    ssh,
                    f"usbipd bind --busid {busid}",
                    timeout=15,
                )
                if not bind_result.ok:
                    logger.error(
                        "Failed to bind USB/IP device %s: %s",
                        busid,
                        (bind_result.stderr or bind_result.stdout).strip(),
                    )
                    continue
                time.sleep(2)
                logger.info(f"Device {busid} bound")
                bound.append(busid)
                if track_newly_bound is not None:
                    track_newly_bound.append(busid)

            except Exception as e:
                logger.error(f"Error binding device {busid}: {e}")

        return bound

    def _rollback_windows_binds(
        self,
        win_ssh,
        newly_bound: list[str],
    ) -> bool:
        """Undo Windows-side binds created by the current start_usbip attempt.

        实现在 usbip_transaction.rollback_windows_binds；此处仅注入本
        manager 的 SSH 工厂与连接管理器。只回滚本次新 bind 的 busid。
        """
        return _rollback_windows_binds_impl(
            self.ssh_manager,
            win_ssh,
            newly_bound,
        )

    def _rollback_source_side(self, source_ssh, source_txn: dict[str, Any]) -> bool:
        """Undo source-side export state created by the current attempt.

        Windows 只回滚本次新 bind 的 busid；Ubuntu 仅在本次全新启动了
        usbipd 进程时才停止它，复用/重启自既有实例时不破坏原有导出。
        """
        if not source_txn:
            return True
        if source_txn.get('kind') == 'windows':
            return self._rollback_windows_binds(
                source_ssh, list(source_txn.get('newly_bound') or []),
            )
        if source_txn.get('kind') == 'linux' and source_txn.get('started'):
            result = stop_ubuntu_usbip_server(self.ssh_manager, source_ssh)
            if not result.get('success'):
                logger.warning(
                    "[USB/IP] Failed to stop Ubuntu usbipd after rollback: %s",
                    result.get('detail'),
                )
            return bool(result.get('success'))
        return True

    def _stop_windows_adb(self, ssh) -> dict[str, Any]:
        """Gracefully stop Windows ADB and force it only when still running."""
        list_result = self.ssh_manager.execute_command(
            ssh,
            'tasklist /FI "IMAGENAME eq adb.exe" /NH',
            timeout=10,
        )
        if not list_result.ok:
            return {
                "success": False,
                "error": (list_result.stderr or list_result.stdout).strip()
                or "无法确认Windows ADB状态",
            }
        if "adb.exe" not in (list_result.stdout or "").lower():
            return {"success": True, "stopped": False, "forced": False}

        stop_result = self.ssh_manager.execute_command(
            ssh,
            "adb kill-server",
            timeout=15,
        )
        time.sleep(1)
        list_result = self.ssh_manager.execute_command(
            ssh,
            'tasklist /FI "IMAGENAME eq adb.exe" /NH',
            timeout=10,
        )
        if not list_result.ok:
            return {
                "success": False,
                "error": (list_result.stderr or list_result.stdout).strip()
                or "无法确认Windows ADB状态",
            }
        if "adb.exe" in (list_result.stdout or "").lower():
            force_result = self.ssh_manager.execute_command(
                ssh,
                "taskkill /F /IM adb.exe /T",
                timeout=15,
            )
            time.sleep(1)
            verify_result = self.ssh_manager.execute_command(
                ssh,
                'tasklist /FI "IMAGENAME eq adb.exe" /NH',
                timeout=10,
            )
            if not verify_result.ok or "adb.exe" in (verify_result.stdout or "").lower():
                return {
                    "success": False,
                    "error": "Windows adb.exe 仍在运行，USB设备句柄未释放: " + (
                        (
                            verify_result.stderr or verify_result.stdout
                            or force_result.stderr or force_result.stdout
                        ).strip()
                        or "unknown process state"
                    ),
                }
            logger.warning(
                "Windows ADB required force stop before USB/IP export: "
                "code=%s detail=%s",
                force_result.code,
                (force_result.stderr or force_result.stdout).strip(),
            )
            return {"success": True, "stopped": True, "forced": True}
        logger.info(
            "Windows ADB stopped gracefully before USB/IP export: "
            "code=%s detail=%s",
            stop_result.code,
            (stop_result.stderr or stop_result.stdout).strip(),
        )
        return {"success": True, "stopped": True, "forced": False}

    def _ensure_vhci_driver(self, ssh):
        try:
            lsmod_result = self.ssh_manager.execute_command(ssh, 'lsmod | grep vhci_hcd')
            if not lsmod_result.stdout.strip():
                logger.info("Loading vhci_hcd driver...")
                self.ssh_manager.execute_command(ssh, 'sudo modprobe vhci_hcd')
                time.sleep(1)
        except Exception as e:
            logger.error(f"Error ensuring vhci driver: {e}")

    def _attach_devices(
        self,
        ssh,
        device_ip: str,
        busids: list[str],
        adb_server_socket: str | None = None,
        allow_transport_only: bool = False,
    ) -> tuple[list[str], list[str]]:
        """Attach the busids on Ubuntu, returning (attached_busids, newly-seen adb device ids)."""
        try:
            adb_devices_command = self._adb_devices_command(
                adb_server_socket
            )
            adb_before_result = self.ssh_manager.execute_command(
                ssh,
                adb_devices_command,
            )
            devices_before = set(DeviceUtils.parse_adb_devices(adb_before_result.stdout))
            adb_states_before = parse_adb_device_states(adb_before_result.stdout)
            fastboot_before_result = self.ssh_manager.execute_command(
                ssh,
                'fastboot devices',
                timeout=5,
            )
            fastboot_before = set(parse_fastboot_devices(
                fastboot_before_result.stdout
                or fastboot_before_result.stderr or ""
            ))
            logger.info(f"Devices before attach: {devices_before}")

            expected_busids = set(busids)
            stable_busids: set[str] = set()
            retryable_busids: set[str] = set()
            for stabilization_attempt in range(
                1, USBIP_ATTACH_STABILIZATION_ATTEMPTS + 1,
            ):
                for busid in busids:
                    if busid in stable_busids:
                        continue
                    cmd = f'sudo usbip attach -r {device_ip} -b {busid}'
                    logger.info(
                        "Attaching %s from %s (stabilization attempt %s/%s)...",
                        busid,
                        device_ip,
                        stabilization_attempt,
                        USBIP_ATTACH_STABILIZATION_ATTEMPTS,
                    )
                    attach_result = self.ssh_manager.execute_command(
                        ssh, cmd, timeout=15,
                    )
                    if not attach_result.ok:
                        logger.warning(
                            "Attach %s failed (code=%s): %s",
                            busid,
                            attach_result.code,
                            attach_result.stderr or attach_result.stdout,
                        )
                    else:
                        # 只有命令已成功、但稍后端口掉线的 BUSID
                        # 才允许进入下一轮；避免对权限、导出等
                        # 确定性失败重复 attach。
                        retryable_busids.add(busid)
                        logger.info("Attach %s command succeeded", busid)

                # ``usbip attach`` 返回 0 时 vhci 仍可能在 USB 枚举
                # 前掉线。要求精确 source/BUSID 连续两次出现；
                # 第一次最多等待数秒覆盖正常枚举延迟。
                first_snapshot: set[str] = set()
                for poll_attempt in range(USBIP_ATTACH_PORT_POLL_ATTEMPTS):
                    if poll_attempt:
                        time.sleep(1)
                    first_poll = self.ssh_manager.execute_command(
                        ssh, USBIP_PORT_COMMAND, timeout=10,
                    )
                    if not first_poll.ok:
                        logger.warning(
                            "Unable to verify USB/IP attachments: "
                            "code=%s detail=%s",
                            first_poll.code,
                            (first_poll.stderr or first_poll.stdout or '').strip(),
                        )
                        return [], []
                    first_snapshot = {
                        entry['busid']
                        for entry in parse_usbip_port_entries(first_poll.stdout or '')
                        if entry['host'] == str(device_ip)
                    } & expected_busids
                    if first_snapshot == expected_busids:
                        break

                time.sleep(1)
                second_poll = self.ssh_manager.execute_command(
                    ssh, USBIP_PORT_COMMAND, timeout=10,
                )
                if not second_poll.ok:
                    logger.warning(
                        "Unable to verify USB/IP attachment stability: "
                        "code=%s detail=%s",
                        second_poll.code,
                        (second_poll.stderr or second_poll.stdout or '').strip(),
                    )
                    return [], []
                second_snapshot = {
                    entry['busid']
                    for entry in parse_usbip_port_entries(second_poll.stdout or '')
                    if entry['host'] == str(device_ip)
                } & expected_busids
                stable_busids = first_snapshot & second_snapshot
                missing = expected_busids - stable_busids
                if not missing:
                    break
                if (
                    stabilization_attempt
                    >= USBIP_ATTACH_STABILIZATION_ATTEMPTS
                    or not missing.issubset(retryable_busids)
                ):
                    logger.warning(
                        "USB/IP attach did not stabilize after enumeration: %s",
                        ', '.join(sorted(missing)),
                    )
                    return [], []
                logger.warning(
                    "USB/IP attach session dropped before enumeration; "
                    "retrying exact BUSID(s): %s",
                    ', '.join(sorted(missing)),
                )
                time.sleep(1)

            # A repeated manual request can receive an "already attached"
            # command error.  The verified target-side port is authoritative;
            # unrelated ADB/Fastboot devices are never used as a substitute.
            attached = list(busids)

            self.ssh_manager.execute_command(ssh, 'sudo udevadm trigger', timeout=8)
            self.ssh_manager.execute_command(ssh, 'sudo udevadm settle', timeout=8)

            if allow_transport_only:
                # RockUSB Loader has no ADB/Fastboot protocol endpoint. Once
                # usbip attach and udev settle succeed, upgrade_tool is the
                # authoritative readiness probe in the firmware workflow.
                return attached, []

            devices_after = set()
            deadline = time.time() + 30
            while time.time() < deadline:
                adb_after_result = self.ssh_manager.execute_command(
                    ssh,
                    adb_devices_command,
                    timeout=8,
                )
                adb_states = parse_adb_device_states(adb_after_result.stdout)
                devices_after = set(DeviceUtils.parse_adb_devices(adb_after_result.stdout))
                logger.info(f"Devices after attach: {devices_after}")

                new_devices = list(devices_after - devices_before)
                if new_devices:
                    logger.info(f"New devices via USB/IP: {new_devices}")
                    return attached, new_devices

                non_device_adb = {
                    serial: state
                    for serial, state in adb_states.items()
                    if state != "device" and adb_states_before.get(serial) != state
                }
                if non_device_adb:
                    logger.info(f"USB/IP protocol visible but not ADB device-ready: {non_device_adb}")
                    return attached, []

                fastboot_after_result = self.ssh_manager.execute_command(
                    ssh,
                    'fastboot devices',
                    timeout=5,
                )
                fastboot_devices = parse_fastboot_devices(
                    fastboot_after_result.stdout
                    or fastboot_after_result.stderr or ""
                )
                new_fastboot_devices = sorted(set(fastboot_devices) - fastboot_before)
                if new_fastboot_devices:
                    logger.info(f"USB/IP fastboot devices visible: {new_fastboot_devices}")
                    return attached, []

                for device_id in devices_after:
                    if device_id in self.device_sources:
                        logger.info(f"Found existing USB/IP device still online: {device_id}")
                        return attached, [device_id]

                time.sleep(1)

            new_devices = list(devices_after - devices_before)
            logger.info(f"New devices via USB/IP: {new_devices}")

            # 没有新设备时返回仍在线的已记录 USB/IP 设备。
            if not new_devices:
                # 检查是否有之前记录的USB/IP设备现在仍然在线
                for device_id in devices_after:
                    if device_id in self.device_sources:
                        new_devices = [device_id]
                        logger.info(f"Found existing USB/IP device still online: {device_id}")
                        break

            return attached, new_devices

        except Exception as e:
            logger.error(f"Error attaching devices: {e}")
            return [], []

    @staticmethod
    def _adb_devices_command(adb_server_socket: str | None = None) -> str:
        if not adb_server_socket:
            return "adb devices"
        return (
            "ADB_SERVER_SOCKET="
            + shlex.quote(adb_server_socket)
            + " adb devices"
        )

    def probe_protocol_status(
        self,
        ssh,
        adb_server_socket: str | None = None,
    ) -> dict[str, Any]:
        """Probe Android protocol states after USB/IP transport is attached."""
        status: dict[str, Any] = {
            "adb": {},
            "adb_ready": [],
            "recovery": [],
            "sideload": [],
            "unauthorized": [],
            "offline": [],
            "fastboot": [],
            "mode": "unknown",
        }
        try:
            adb_probe = self.ssh_manager.execute_command(
                ssh,
                self._adb_devices_command(adb_server_socket),
                timeout=8,
            )
            adb_states = parse_adb_device_states(
                adb_probe.stdout or adb_probe.stderr or ""
            )
            status["adb"] = adb_states
            status["adb_ready"] = [serial for serial, state in adb_states.items() if state == "device"]
            status["recovery"] = [serial for serial, state in adb_states.items() if state == "recovery"]
            status["sideload"] = [serial for serial, state in adb_states.items() if state == "sideload"]
            status["unauthorized"] = [serial for serial, state in adb_states.items() if state == "unauthorized"]
            status["offline"] = [serial for serial, state in adb_states.items() if state == "offline"]
        except Exception as exc:
            logger.debug("[USB/IP] adb protocol probe failed: %s", exc)

        try:
            fastboot_probe = self.ssh_manager.execute_command(ssh, "fastboot devices", timeout=8)
            status["fastboot"] = parse_fastboot_devices(
                fastboot_probe.stdout or fastboot_probe.stderr or ""
            )
        except Exception as exc:
            logger.debug("[USB/IP] fastboot protocol probe failed: %s", exc)

        if status["fastboot"]:
            status["mode"] = "fastboot"
        elif status["recovery"] or status["sideload"]:
            status["mode"] = "recovery"
        elif status["adb_ready"]:
            status["mode"] = "adb"
        elif status["unauthorized"]:
            status["mode"] = "unauthorized"
        elif status["offline"]:
            status["mode"] = "offline"
        elif status["adb"]:
            status["mode"] = "adb_non_device"
        return status

    def _build_attach_message(
        self,
        attached: list[str],
        device_list: list[str],
        protocol_status: dict[str, Any],
    ) -> str:
        if device_list:
            return f'✅ 成功连接{len(attached)}个USB/IP设备，ADB在线: {", ".join(device_list)}'
        mode = (protocol_status or {}).get("mode") or "unknown"
        if mode in {"fastboot", "recovery", "unauthorized", "offline", "adb_non_device"}:
            return f'✅ USB/IP传输已连接，当前协议状态: {mode}'
        return '✅ USB/IP传输已连接，等待设备枚举完成'

    def _scope_protocol_status(
        self,
        protocol_status: dict[str, Any],
        device_list: list[str],
    ) -> dict[str, Any]:
        """Keep protocol status focused on the USB/IP devices from this attach.

        device_list 为空（transport-only/Loader/枚举失败）时，全局探测结果
        无法归因到本次 attach：Ubuntu 上其他来源设备（如直连的
        RK3562GMS7）的 ADB/Fastboot 状态不能算作 USB/IP 设备状态，否则
        重连 worker 会把"adb"误判为传输已恢复。此时清空归因列表并标记
        mode=unknown，原始探测保留在 ``unscoped`` 字段供诊断。
        """
        scoped = dict(protocol_status or {})
        if not device_list:
            scoped["adb"] = {}
            for key in ("adb_ready", "recovery", "sideload", "unauthorized", "offline", "fastboot"):
                scoped[key] = []
            scoped["unscoped"] = dict(protocol_status or {})
            scoped["mode"] = "unknown"
            return scoped
        allowed = set(device_list)
        adb_states = scoped.get("adb") or {}
        if isinstance(adb_states, dict):
            scoped["adb"] = {
                serial: state
                for serial, state in adb_states.items()
                if serial in allowed
            }
        for key in ("adb_ready", "recovery", "sideload", "unauthorized", "offline", "fastboot"):
            values = scoped.get(key) or []
            if isinstance(values, list):
                scoped[key] = [serial for serial in values if serial in allowed]
        if scoped.get("adb_ready"):
            scoped["mode"] = "adb"
        return scoped

    def check_usbipd_installed(self, ssh) -> tuple[bool, str]:
        """Check whether usbipd is installed on the Windows host; return (installed, version)."""
        return check_usbipd_installed(self.ssh_manager, ssh)

    def install_usbipd(self, ssh, config: dict[str, Any]) -> dict[str, Any]:
        """Install usbipd on the source host (winget on Windows, upload on Ubuntu)."""
        if self._detect_source_os(ssh) == "linux":
            return install_ubuntu_usbipd(
                self.ssh_manager,
                ssh,
                local_binary=config.get("usbip_linux_server_bin"),
            )
        return install_usbipd(self.ssh_manager, ssh, config)


# 全局USB/IP管理器实例
usbip_manager = USBIPManager()
