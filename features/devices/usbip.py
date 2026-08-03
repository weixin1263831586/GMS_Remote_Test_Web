"""
USB/IP - 核心业务逻辑

特性：
- USB/IP设备转发
- Windows主机支持
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

from .ssh_credentials import find_device_host_password
from .usb import (
    parse_usbipd_android_busids,
)
from .utils import DeviceUtils


logger = logging.getLogger(__name__)

__all__ = [
    "USBIPManager",
    "detach_ubuntu_usbip_ports",
    "find_device_host_password",
    "parse_adb_device_states",
    "parse_fastboot_devices",
    "usbip_manager",
    "wait_for_adb_serial_ready",
]

# usbipd 安装命令常量
USBIPD_INSTALL_CMD = 'winget install dorssel.usbipd-win --source winget'

USBIPD_INSTALL_GUIDE = '''在Windows电脑上以【管理员身份】运行PowerShell执行：
{install_cmd}
验证安装：usbipd --version'''


def usbip_error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    remediation: str = "",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "success": False,
        "error_code": code,
        "error": message,
        "retryable": retryable,
        "remediation": remediation,
        **extra,
    }


def detach_ubuntu_usbip_ports(
    ssh,
    remote_host: str | None = '127.0.0.1',
    detach_all: bool = False,
    busids: list[str] | None = None,
) -> list[str]:
    """Detach Ubuntu usbip ports that point to a remote USB/IP host."""
    detached: list[str] = []
    stdout, stderr, code = usbip_manager.ssh_manager.execute_command(ssh, 'usbip port', timeout=10)
    if code != 0:
        logger.info(f"[USB/IP] usbip port returned {code}: {stderr or stdout}")
        return detached

    current_port: str | None = None
    current_block: list[str] = []
    for line in [*(stdout or '').splitlines(), 'Port 999999:']:
        port_match = re.match(r'\s*Port\s+(\d+):', line)
        if port_match:
            block_text = '\n'.join(current_block)
            host_matches = bool(remote_host and remote_host in block_text)
            busid_matches = not busids or any(busid in block_text for busid in busids)
            if current_port and (detach_all or (host_matches and busid_matches)):
                detach_out, detach_err, detach_code = usbip_manager.ssh_manager.execute_command(
                    ssh, f'sudo usbip detach -p {current_port}', timeout=15
                )
                logger.info(
                    f"[USB/IP] Detached stale Ubuntu usbip port {current_port}: "
                    f"code={detach_code} out={detach_out} err={detach_err}"
                )
                detached.append(current_port)
            current_port = port_match.group(1)
            current_block = [line]
        elif current_port:
            current_block.append(line)

    if detached:
        time.sleep(2)
    return detached


def wait_for_adb_serial_ready(ssh, serial_no: str, timeout: int = 30) -> dict[str, Any]:
    """Wait until a specific ADB serial is in device state and shell responds."""
    quoted_serial = shlex.quote(serial_no)
    deadline = time.time() + timeout
    last_output = ''
    last_error = ''

    usbip_manager.ssh_manager.execute_command(ssh, 'adb start-server', timeout=10)
    while time.time() < deadline:
        state_out, state_err, state_code = usbip_manager.ssh_manager.execute_command(
            ssh, f'adb -s {quoted_serial} get-state', timeout=8
        )
        state_text = (state_out or state_err or '').strip()
        last_output = state_out or ''
        last_error = state_err or ''

        if state_code == 0 and state_text == 'device':
            shell_out, shell_err, shell_code = usbip_manager.ssh_manager.execute_command(
                ssh, f"adb -s {quoted_serial} shell echo ready", timeout=10
            )
            last_output = shell_out or ''
            last_error = shell_err or ''
            if shell_code == 0 and 'ready' in shell_out:
                return {'ready': True}

        time.sleep(2)

    devices_out, devices_err, _ = usbip_manager.ssh_manager.execute_command(ssh, 'adb devices', timeout=8)
    return {
        'ready': False,
        'state': (last_output or last_error or '').strip(),
        'devices': (devices_out or devices_err or '').strip(),
    }


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
    devices: list[str] = []
    for raw_line in (output or "").splitlines():
        parts = raw_line.strip().split()
        if len(parts) >= 2 and parts[1].lower() in {"fastboot", "fastbootd"}:
            devices.append(parts[0])
    return devices


class USBIPManager:
    """Manages the full USB/IP lifecycle: Windows-side bind, Ubuntu-side attach, and protocol probing."""

    def __init__(self, ssh_manager=None, config_manager=None):
        self.ssh_manager = ssh_manager
        self.config_manager = config_manager
        self.active_connections: dict[str, Any] = {}  # {client_id: connection_info}
        self.device_sources: dict[str, dict[str, Any]] = {}  # {device_id: source_info}

    def start_usbip(
        self,
        device_host: str,
        device_password: str | None = None,
        usbip_attach_host: str | None = None,
        selected_busids: list[str] | None = None,
        adb_server_socket: str | None = None,
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

            # 连接Windows主机
            username, hostname = parse_host_address(device_host)
            ssh_hostname, ssh_port = split_host_port(hostname)
            usbip_attach_host = usbip_attach_host or config.get('usbip_attach_host') or ssh_hostname
            win_ssh = self._create_windows_ssh(ssh_hostname, username, device_password, ssh_port)

            if not win_ssh:
                return {'success': False, 'error': f'SSH连接失败到 {device_host}'}

            try:
                # 检查系统类型
                is_windows = self._is_windows_host(win_ssh)
                if not is_windows:
                    win_ssh.close()
                    return {'success': False, 'error': 'USB/IP仅支持Windows主机'}

                # 检查usbipd是否已安装
                installed, _version = self.check_usbipd_installed(win_ssh)
                if not installed:
                    win_ssh.close()
                    return {
                        'success': False,
                        'error': 'usbipd未安装',
                        'install_guide': USBIPD_INSTALL_GUIDE.format(install_cmd=USBIPD_INSTALL_CMD)
                    }

                network_quality = probe_tcp_quality(usbip_attach_host, 3240)
                if not network_quality["reachable"]:
                    return usbip_error(
                        "USBIP_TCP_UNREACHABLE",
                        f"无法连接USB/IP来源 {usbip_attach_host}:3240",
                        retryable=True,
                        remediation="请检查usbipd服务、TCP 3240防火墙和网络路由。",
                        network_quality=network_quality,
                    )

                adb_release = self._stop_windows_adb(win_ssh)
                if not adb_release.get("success"):
                    return usbip_error(
                        "USBIP_ADB_RELEASE_FAILED",
                        f"释放Windows ADB占用失败: {adb_release.get('error')}",
                        remediation="请关闭占用设备的Android Studio、scrcpy或其他ADB任务后重试。",
                    )

                discovered_busids = self._find_android_devices(win_ssh, config)
                requested = [str(item) for item in selected_busids or []]
                busids = requested or discovered_busids
                if requested and not set(requested).issubset(discovered_busids):
                    return {'success': False, 'error': '选择的USB设备已不可用，请刷新后重试'}
                if not busids:
                    win_ssh.close()
                    return {'success': False, 'error': '未找到Android设备'}

                bound = self._bind_devices(win_ssh, busids)
                win_ssh.close()

                if not bound:
                    return {'success': False, 'error': '设备绑定失败'}

                # 连接Ubuntu并attach设备
                ubuntu_ssh = self.ssh_manager.get_connection(config)
                if not ubuntu_ssh:
                    return {'success': False, 'error': '无法连接Ubuntu主机'}

                try:
                    # 确保vhci驱动已加载
                    self._ensure_vhci_driver(ubuntu_ssh)
                    detach_ubuntu_usbip_ports(ubuntu_ssh, usbip_attach_host, detach_all=False)

                    # Attach设备
                    attached, device_list = self._attach_devices(
                        ubuntu_ssh,
                        usbip_attach_host,
                        busids,
                        adb_server_socket=adb_server_socket,
                    )

                    if not attached:
                        self.ssh_manager.return_connection(ubuntu_ssh)
                        return {
                            'success': False,
                            'error': 'USB/IP attach 失败，未成功连接任何设备',
                            'devices': [],
                            'device_list': []
                        }

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
                        'readiness': (
                            'test_ready' if protocol_status.get('mode') in {'adb', 'fastboot', 'recovery'}
                            else 'protocol_ready' if protocol_status.get('mode') not in {'unknown', 'offline', 'unauthorized'}
                            else 'transport_ready'
                        ),
                        'network_quality': network_quality,
                        'protocol_status': protocol_status,
                    }

                except Exception as e:
                    ubuntu_ssh.close()
                    logger.error(f"Error in Ubuntu attach: {e}")
                    return {'success': False, 'error': str(e)}

            except Exception as e:
                win_ssh.close()
                logger.error(f"Error in Windows side: {e}")
                return {'success': False, 'error': str(e)}

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
            if not self._is_windows_host(ssh):
                return {"success": False, "error": "USB/IP仅支持Windows主机"}
            output = self._usbipd_list_output(ssh)
            busids = parse_usbipd_android_busids(
                output, config.get("usbip_vid_pid")
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
            identity_by_vid_pid = self._query_windows_usb_identities(
                ssh,
                {
                    value.split(":", 1)[0]
                    for value in vid_pid_by_busid.values()
                },
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
            return {
                "success": True,
                "device_host": device_host,
                "devices": [
                    {
                        "busid": item,
                        "serial": serial_by_busid.get(item, ""),
                        "logical_device_id": (
                            serial_by_busid.get(item, "")
                            or identity_by_vid_pid.get(
                                vid_pid_by_busid.get(item, ""), {}
                            ).get("pnp_instance_id", "")
                            or item
                        ),
                        "usb_serial": identity_by_vid_pid.get(
                            vid_pid_by_busid.get(item, ""), {}
                        ).get("usb_serial", ""),
                        "pnp_instance_id": identity_by_vid_pid.get(
                            vid_pid_by_busid.get(item, ""), {}
                        ).get("pnp_instance_id", ""),
                        "location_path": identity_by_vid_pid.get(
                            vid_pid_by_busid.get(item, ""), {}
                        ).get("location_path", ""),
                        "vid_pid": vid_pid_by_busid.get(item, ""),
                        "current_busid": item,
                        "label": self._append_serial(
                            labels.get(item, item),
                            serial_by_busid.get(item),
                        ),
                    }
                    for item in busids
                ],
            }
        finally:
            ssh.close()

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
            stdout, _stderr, code = self.ssh_manager.execute_command(
                ssh, f'powershell -NoProfile -Command "{ps}"', timeout=15
            )
        except Exception:
            return {}
        if code != 0 or not stdout:
            return {}
        raw: dict[str, list[str]] = {}
        candidates: list[str] = []
        for line in stdout.splitlines():
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

    def _query_windows_usb_identities(
        self,
        ssh,
        vendor_ids: set[str] | None = None,
    ) -> dict[str, dict[str, str]]:
        """Return stable PnP and physical-location identity when unambiguous."""
        ps = (
            "Get-PnpDevice -PresentOnly -Class USB | ForEach-Object { "
            "$l=(Get-PnpDeviceProperty -InstanceId $_.InstanceId "
            "-KeyName 'DEVPKEY_Device_LocationPaths' -ErrorAction SilentlyContinue).Data; "
            "Write-Output ($_.InstanceId + '|' + ($l -join ',')) }"
        )
        try:
            stdout, _stderr, code = self.ssh_manager.execute_command(
                ssh, f'powershell -NoProfile -Command "{ps}"', timeout=20
            )
        except Exception:
            return {}
        if code != 0 or not stdout:
            return {}
        grouped: dict[str, list[dict[str, str]]] = {}
        for raw in stdout.splitlines():
            instance_id, _separator, location = raw.strip().partition("|")
            match = re.search(
                r"USB\\VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})"
                r"([^\\]*)\\(.+)",
                instance_id,
            )
            if not match:
                continue
            vid, pid, interface, tail = match.groups()
            vid = vid.lower()
            if vendor_ids and vid not in vendor_ids:
                continue
            if "&MI_" in interface.upper():
                continue
            usb_serial = tail.strip().split("&")[0].strip()
            grouped.setdefault(f"{vid}:{pid.lower()}", []).append({
                "usb_serial": usb_serial,
                "pnp_instance_id": instance_id,
                "location_path": location.strip(),
            })
        return {
            key: values[0]
            for key, values in grouped.items()
            if len({item["pnp_instance_id"] for item in values}) == 1
        }

    def _query_windows_adb_serials(self, ssh) -> list[str]:
        """Return stable Android serials visible to Windows ADB."""
        try:
            stdout, stderr, code = self.ssh_manager.execute_command(
                ssh,
                "adb devices",
                timeout=15,
            )
        except Exception:
            return []
        if code != 0:
            logger.debug(
                "[USB/IP] Windows adb inventory failed: %s",
                (stderr or stdout or "").strip(),
            )
            return []
        states = parse_adb_device_states(stdout)
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

    def bind_source_devices(
        self,
        device_host: str,
        busids: list[str],
        device_password: str | None = None,
    ) -> dict[str, Any]:
        """Bind selected Windows USB devices for a remote Worker attach."""
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
            if not self._is_windows_host(ssh):
                return {"success": False, "error": "USB/IP仅支持Windows主机"}
            if not self.check_usbipd_installed(ssh)[0]:
                return {"success": False, "error": "usbipd未安装"}
            selected = list(dict.fromkeys(
                str(item or "").strip() for item in busids or []
            ))
            if not selected or any(
                not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", item)
                for item in selected
            ):
                return {"success": False, "error": "无效的USB/IP BUSID"}
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
            if not self._is_windows_host(ssh):
                return {"success": False, "error": "USB/IP仅支持Windows主机"}
            if not self.check_usbipd_installed(ssh)[0]:
                return {"success": False, "error": "usbipd未安装"}

            detached = []
            errors = {}
            for busid in selected:
                stdout, stderr, code = self.ssh_manager.execute_command(
                    ssh,
                    f"usbipd detach --busid {busid}",
                    timeout=15,
                )
                detail = (stderr or stdout or "").strip()
                normalized_detail = detail.lower()
                if code == 0 or any(
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
                    errors[busid] = detail or f"usbipd detach exited with code {code}"
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
            stdout, _stderr, _code = self.ssh_manager.execute_command(ssh, 'ver 2>&1')
            return 'microsoft' in stdout.lower() or 'windows' in stdout.lower()
        except Exception:
            return False

    def _find_android_devices(self, ssh, config: dict[str, Any]) -> list[str]:
        try:
            output = self._usbipd_list_output(ssh)
            devices = parse_usbipd_android_busids(output, config.get('usbip_vid_pid'))
            logger.info(f"Found USB/IP devices: {devices}")
            return devices

        except Exception as e:
            logger.error(f"Error finding Android devices: {e}")
            return []

    def _usbipd_list_output(self, ssh) -> str:
        # usbipd list 需要 PTY 才会返回完整设备表。
        stdout, stderr, code = self.ssh_manager.execute_command(
            ssh, "usbipd list", timeout=15, get_pty=True
        )
        output = "\n".join(part for part in (stdout, stderr) if part)
        logger.info("USB/IP devices (code=%s):\n%s", code, output)
        return output

    def _bind_devices(self, ssh, busids: list[str]) -> list[str]:
        bound = []
        for busid in busids:
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", str(busid or "")):
                logger.error("Rejected invalid USB/IP busid: %r", busid)
                continue
            try:
                stdout, stderr, list_code = self.ssh_manager.execute_command(
                    ssh, f"usbipd list | findstr {busid}"
                )
                if list_code not in {0, 1}:
                    logger.error(
                        "Failed to inspect USB/IP device %s: %s",
                        busid,
                        (stderr or stdout).strip(),
                    )
                    continue

                if 'Shared' in stdout:
                    logger.info(f"Device {busid} already shared")
                    bound.append(busid)
                    continue
                elif 'Attached' in stdout:
                    # Detach first
                    detach_out, detach_err, detach_code = (
                        self.ssh_manager.execute_command(
                            ssh,
                            f"usbipd detach --busid {busid}",
                            timeout=15,
                        )
                    )
                    if detach_code != 0:
                        logger.error(
                            "Failed to detach USB/IP device %s before bind: %s",
                            busid,
                            (detach_err or detach_out).strip(),
                        )
                        continue
                    time.sleep(1)

                # Bind
                bind_out, bind_err, bind_code = self.ssh_manager.execute_command(
                    ssh,
                    f"usbipd bind --busid {busid}",
                    timeout=15,
                )
                if bind_code != 0:
                    logger.error(
                        "Failed to bind USB/IP device %s: %s",
                        busid,
                        (bind_err or bind_out).strip(),
                    )
                    continue
                time.sleep(2)
                logger.info(f"Device {busid} bound")
                bound.append(busid)

            except Exception as e:
                logger.error(f"Error binding device {busid}: {e}")

        return bound

    def _stop_windows_adb(self, ssh) -> dict[str, Any]:
        """Gracefully stop Windows ADB and force it only when still running."""
        list_out, list_err, list_code = self.ssh_manager.execute_command(
            ssh,
            'tasklist /FI "IMAGENAME eq adb.exe" /NH',
            timeout=10,
        )
        if list_code != 0:
            return {
                "success": False,
                "error": (list_err or list_out).strip() or "无法确认Windows ADB状态",
            }
        if "adb.exe" not in (list_out or "").lower():
            return {"success": True, "stopped": False, "forced": False}

        stop_out, stop_err, stop_code = self.ssh_manager.execute_command(
            ssh,
            "adb kill-server",
            timeout=15,
        )
        time.sleep(1)
        list_out, list_err, list_code = self.ssh_manager.execute_command(
            ssh,
            'tasklist /FI "IMAGENAME eq adb.exe" /NH',
            timeout=10,
        )
        if list_code != 0:
            return {
                "success": False,
                "error": (list_err or list_out).strip() or "无法确认Windows ADB状态",
            }
        if "adb.exe" in (list_out or "").lower():
            force_out, force_err, force_code = self.ssh_manager.execute_command(
                ssh,
                "taskkill /F /IM adb.exe /T",
                timeout=15,
            )
            time.sleep(1)
            verify_out, verify_err, verify_code = self.ssh_manager.execute_command(
                ssh,
                'tasklist /FI "IMAGENAME eq adb.exe" /NH',
                timeout=10,
            )
            if verify_code != 0 or "adb.exe" in (verify_out or "").lower():
                return {
                    "success": False,
                    "error": "Windows adb.exe 仍在运行，USB设备句柄未释放: " + (
                        (verify_err or verify_out or force_err or force_out).strip()
                        or "unknown process state"
                    ),
                }
            logger.warning(
                "Windows ADB required force stop before USB/IP export: code=%s detail=%s",
                force_code,
                (force_err or force_out).strip(),
            )
            return {"success": True, "stopped": True, "forced": True}
        logger.info(
            "Windows ADB stopped gracefully before USB/IP export: code=%s detail=%s",
            stop_code,
            (stop_err or stop_out).strip(),
        )
        return {"success": True, "stopped": True, "forced": False}

    def _ensure_vhci_driver(self, ssh):
        try:
            stdout, _, _ = self.ssh_manager.execute_command(ssh, 'lsmod | grep vhci_hcd')
            if not stdout.strip():
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
    ) -> tuple[list[str], list[str]]:
        """Attach the busids on Ubuntu, returning (attached_busids, newly-seen adb device ids)."""
        try:
            adb_devices_command = self._adb_devices_command(
                adb_server_socket
            )
            stdout_before, _, _ = self.ssh_manager.execute_command(
                ssh,
                adb_devices_command,
            )
            devices_before = set(DeviceUtils.parse_adb_devices(stdout_before))
            adb_states_before = parse_adb_device_states(stdout_before)
            fastboot_before_out, fastboot_before_err, _ = self.ssh_manager.execute_command(
                ssh,
                'fastboot devices',
                timeout=5,
            )
            fastboot_before = set(parse_fastboot_devices(fastboot_before_out or fastboot_before_err or ""))
            logger.info(f"Devices before attach: {devices_before}")

            # Attach设备
            attached = []
            for busid in busids:
                cmd = f'sudo usbip attach -r {device_ip} -b {busid}'
                logger.info(f"Attaching {busid} from {device_ip}...")
                attach_out, attach_err, attach_code = self.ssh_manager.execute_command(ssh, cmd, timeout=15)
                if attach_code != 0:
                    logger.warning(f"Attach {busid} failed (code={attach_code}): {attach_err or attach_out}")
                else:
                    logger.info(f"Attach {busid} succeeded")
                    attached.append(busid)

            if not attached:
                protocol_status = self.probe_protocol_status(
                    ssh,
                    adb_server_socket=adb_server_socket,
                )
                visible_devices = protocol_status.get("adb_ready") or []
                if visible_devices:
                    logger.info(
                        "USB/IP attach commands failed, but ADB devices are already visible: %s",
                        visible_devices,
                    )
                    return list(busids), list(visible_devices)
                for key in ("fastboot", "recovery", "sideload", "unauthorized", "offline"):
                    if protocol_status.get(key):
                        logger.info(
                            "USB/IP attach commands failed, but protocol %s is visible: %s",
                            key,
                            protocol_status.get(key),
                        )
                        return list(busids), []
                return [], []

            self.ssh_manager.execute_command(ssh, 'sudo udevadm trigger', timeout=8)
            self.ssh_manager.execute_command(ssh, 'sudo udevadm settle', timeout=8)

            devices_after = set()
            deadline = time.time() + 30
            while time.time() < deadline:
                stdout_after, _, _ = self.ssh_manager.execute_command(
                    ssh,
                    adb_devices_command,
                    timeout=8,
                )
                adb_states = parse_adb_device_states(stdout_after)
                devices_after = set(DeviceUtils.parse_adb_devices(stdout_after))
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

                fastboot_out, fastboot_err, _ = self.ssh_manager.execute_command(
                    ssh,
                    'fastboot devices',
                    timeout=5,
                )
                fastboot_devices = parse_fastboot_devices(fastboot_out or fastboot_err or "")
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
            adb_out, adb_err, _ = self.ssh_manager.execute_command(
                ssh,
                self._adb_devices_command(adb_server_socket),
                timeout=8,
            )
            adb_states = parse_adb_device_states(adb_out or adb_err or "")
            status["adb"] = adb_states
            status["adb_ready"] = [serial for serial, state in adb_states.items() if state == "device"]
            status["recovery"] = [serial for serial, state in adb_states.items() if state == "recovery"]
            status["sideload"] = [serial for serial, state in adb_states.items() if state == "sideload"]
            status["unauthorized"] = [serial for serial, state in adb_states.items() if state == "unauthorized"]
            status["offline"] = [serial for serial, state in adb_states.items() if state == "offline"]
        except Exception as exc:
            logger.debug("[USB/IP] adb protocol probe failed: %s", exc)

        try:
            fastboot_out, fastboot_err, _ = self.ssh_manager.execute_command(ssh, "fastboot devices", timeout=8)
            status["fastboot"] = parse_fastboot_devices(fastboot_out or fastboot_err or "")
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
        """Keep protocol status focused on the USB/IP devices from this attach."""
        if not device_list:
            return protocol_status
        allowed = set(device_list)
        scoped = dict(protocol_status or {})
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
        try:
            stdout, _stderr, code = self.ssh_manager.execute_command(ssh, 'usbipd --version')
            if code == 0 and stdout.strip():
                return True, stdout.strip()
            return False, ''
        except Exception as e:
            logger.error(f"Error checking usbipd: {e}")
            return False, ''

    def install_usbipd(self, ssh, config: dict[str, Any]) -> dict[str, Any]:
        """在 Windows 主机自动安装 usbipd。"""
        try:
            # 检查是否已经是管理员权限
            check_admin_cmd = 'whoami /groups | findstr S-1-16-12288'
            stdout, stderr, code = self.ssh_manager.execute_command(ssh, check_admin_cmd)

            if code != 0 or 'S-1-16-12288' not in stdout:
                return {
                    'success': False,
                    'error': f'需要管理员权限。请在 Windows 上以【管理员身份】运行 PowerShell，然后执行: {USBIPD_INSTALL_CMD}'
                }

            # 执行自动安装命令（添加自动接受参数）
            install_cmd = f'{USBIPD_INSTALL_CMD} --accept-package-agreements --accept-source-agreements'
            stdout, stderr, code = self.ssh_manager.execute_command(ssh, install_cmd, timeout=120)

            if code == 0:
                # 验证安装
                installed, version = self.check_usbipd_installed(ssh)
                if installed:
                    return {
                        'success': True,
                        'message': f'usbipd 安装成功！版本: {version}',
                        'version': version
                    }
                else:
                    return {
                        'success': True,
                        'message': 'usbipd 安装完成，请验证版本'
                    }
            else:
                return {
                    'success': False,
                    'error': f'安装失败: {stderr or stdout}'
                }

        except Exception as e:
            logger.error(f"Error installing usbipd: {e}")
            return {
                'success': False,
                'error': str(e)
            }


# 全局USB/IP管理器实例
usbip_manager = USBIPManager()
