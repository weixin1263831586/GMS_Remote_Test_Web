"""
USB/IP - 核心业务编排

特性：
- USB/IP设备转发
- Windows来源主机（usbipd-win）与Ubuntu/Linux来源主机（用户态usbipd）支持
- 设备绑定/解绑

模块边界（拆分债务收敛后的形态）：

- ``usbip_protocol``        协议态探测与解析（无状态）；
- ``usbip_source_sessions`` 来源主机会话与 bind/detach/probe 操作；
- ``usbip_source_inventory`` 来源设备清单（Windows PnP / Ubuntu udev）；
- ``usbip_transaction``     目标侧 attach/detach 事务与回滚；
- ``usbip_linux_source``    Ubuntu 用户态 usbipd 服务端；
- ``usbipd_setup``          usbipd-win 安装检查/引导。

本文件保留 ``USBIPManager`` 编排层：``start_usbip`` 的端到端事务
（来源导出 → 目标 attach → 协议归因 → 回滚），以及面向测试/路由的
兼容代理方法。实例方法（而非模块函数）是测试的覆盖点，内部调用
必须经 ``self.`` 分发。
"""

import logging
import time
from typing import Any

from foundation.network_quality import probe_tcp_quality
from foundation.networking import parse_host_address, split_host_port

from .ssh_credentials import find_device_host_password
from .usb import (
    ANDROID_USBIP_MARKERS,
    configured_usbip_vid_pids,
    parse_usbipd_android_busids,
    parse_usbipd_busid_statuses,
)
from .usbip_linux_source import (
    ensure_ubuntu_usbip_server,
    install_ubuntu_usbipd,
    list_ubuntu_usb_devices,
    source_os_label,
    stop_ubuntu_usbip_server,
)
from .usbip_protocol import (
    build_adb_devices_command,
    build_attach_message,
    parse_adb_device_states,
    parse_fastboot_devices,
)
from .usbip_protocol import (
    probe_protocol_status as _probe_protocol_impl,
)
from .usbip_protocol import (
    scope_protocol_status as _scope_protocol_impl,
)
from .usbip_readiness import wait_for_adb_serial_ready
from .usbip_source_inventory import (
    _usbipd_list_output,
)
from .usbip_source_inventory import (
    list_source_devices as _list_source_devices_impl,
)
from .usbip_source_inventory import (
    query_windows_adb_serials as _query_adb_serials_impl,
)
from .usbip_source_inventory import (
    query_windows_usb_serials as _query_usb_serials_impl,
)
from .usbip_source_sessions import (
    bind_source_devices as _bind_source_devices_impl,
)
from .usbip_source_sessions import (
    bind_usbipd_devices,
    create_source_ssh,
    is_windows_host,
    source_os_public,
    stop_windows_adb,
)
from .usbip_source_sessions import (
    detach_source_sessions as _detach_source_sessions_impl,
)
from .usbip_source_sessions import (
    ensure_source_export_ready as _ensure_export_ready_impl,
)
from .usbip_source_sessions import (
    probe_source_os as _probe_source_os_impl,
)
from .usbip_transaction import (
    USBIP_PORT_COMMAND,
    parse_usbip_port_entries,
    rollback_ubuntu_attachments,
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


class USBIPManager:
    """Manages the full USB/IP lifecycle: Windows-side bind, Ubuntu-side attach, and protocol probing."""

    def __init__(self, ssh_manager=None, config_manager=None):
        self.ssh_manager = ssh_manager
        self.config_manager = config_manager
        self.active_connections: dict[str, Any] = {}  # {client_id: connection_info}
        self.device_sources: dict[str, dict[str, Any]] = {}  # {device_id: source_info}

    # ============ Source-side delegates (impl in usbip_source_sessions) ============

    @staticmethod
    def _source_os_public(source_os: str) -> str:
        """Map internal OS kind to the public source_os API value."""
        return source_os_public(source_os)

    def _detect_source_os(self, ssh) -> str:
        """Classify a source host: 'windows', 'linux' or '' (unsupported).

        Windows 判定经 ``self._is_windows_host`` 分发：测试在实例上
        覆盖该方法，绕过会破坏 start_usbip 的 OS 分支测试。
        """
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

    def _create_windows_ssh(self, hostname: str, username: str, password: str, port: int = 22):
        return create_source_ssh(hostname, username, password, port)

    def _is_windows_host(self, ssh) -> bool:
        return is_windows_host(self.ssh_manager, ssh)

    def probe_source_os(
        self,
        device_host: str,
        device_password: str | None = None,
    ) -> dict[str, Any]:
        """Detect the OS of a source host via SSH; used for dropdown labels."""
        return _probe_source_os_impl(self, device_host, device_password)

    def ensure_source_export_ready(
        self,
        device_host: str,
        busids: list[str] | None = None,
        device_password: str | None = None,
    ) -> dict[str, Any]:
        """Start the on-demand usbipd server for Ubuntu sources (see impl)."""
        return _ensure_export_ready_impl(self, device_host, busids, device_password)

    def bind_source_devices(
        self,
        device_host: str,
        busids: list[str],
        device_password: str | None = None,
    ) -> dict[str, Any]:
        """Bind selected source USB devices for a remote Worker attach."""
        return _bind_source_devices_impl(self, device_host, busids, device_password)

    def detach_source_sessions(
        self,
        device_host: str,
        busids: list[str],
        device_password: str | None = None,
    ) -> dict[str, Any]:
        """Drop stale usbipd exports without removing persistent bindings."""
        return _detach_source_sessions_impl(self, device_host, busids, device_password)

    def _bind_devices(
        self,
        ssh,
        busids: list[str],
        track_newly_bound: list[str] | None = None,
    ) -> list[str]:
        """Bind USB devices on the Windows source host (impl in usbip_source_sessions)."""
        return bind_usbipd_devices(self.ssh_manager, ssh, busids, track_newly_bound)

    def _stop_windows_adb(self, ssh) -> dict[str, Any]:
        """Gracefully stop Windows ADB and force it only when still running."""
        return stop_windows_adb(self.ssh_manager, ssh)

    # ============ Inventory delegates (impl in usbip_source_inventory) ============

    def list_source_devices(
        self, device_host: str, device_password: str | None = None
    ) -> dict[str, Any]:
        """List Android USB/IP busids on a Windows source without binding."""
        return _list_source_devices_impl(self, device_host, device_password)

    def _query_windows_usb_serials(
        self,
        ssh,
        vendor_ids: set[str] | None = None,
    ) -> dict[str, str]:
        """Query Windows for USB device serials, keyed by ``vid:pid``."""
        return _query_usb_serials_impl(self.ssh_manager, ssh, vendor_ids)

    def _query_windows_adb_serials(self, ssh) -> list[str]:
        """Return stable Android serials visible to Windows ADB."""
        return _query_adb_serials_impl(self.ssh_manager, ssh)

    # ============ Protocol delegates (impl in usbip_protocol) ============

    @staticmethod
    def _adb_devices_command(adb_server_socket: str | None = None) -> str:
        return build_adb_devices_command(adb_server_socket)

    def probe_protocol_status(
        self,
        ssh,
        adb_server_socket: str | None = None,
    ) -> dict[str, Any]:
        """Probe Android protocol states after USB/IP transport is attached."""
        return _probe_protocol_impl(
            self.ssh_manager, ssh, adb_server_socket=adb_server_socket
        )

    def _scope_protocol_status(
        self,
        protocol_status: dict[str, Any],
        device_list: list[str],
    ) -> dict[str, Any]:
        """Keep protocol status focused on the USB/IP devices from this attach."""
        return _scope_protocol_impl(protocol_status, device_list)

    def _build_attach_message(
        self,
        attached: list[str],
        device_list: list[str],
        protocol_status: dict[str, Any],
    ) -> str:
        return build_attach_message(attached, device_list, protocol_status)

    # ============ Orchestration (kept here) ============

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

    # ============ Helpers (target-side attach machinery) ============

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
        return _usbipd_list_output(self.ssh_manager, ssh)

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
