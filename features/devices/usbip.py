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

from foundation.networking import parse_host_address as _parse_host_address
from foundation.networking import split_host_port

from .usb import (
    parse_usbipd_android_busids,
)
from .utils import DeviceUtils


logger = logging.getLogger(__name__)

# usbipd 安装命令常量
USBIPD_INSTALL_CMD = 'winget install dorssel.usbipd-win --source winget'

USBIPD_INSTALL_GUIDE = '''在Windows电脑上以【管理员身份】运行PowerShell执行：
{install_cmd}
验证安装：usbipd --version'''


def find_device_host_password(device_host: str, config: dict[str, Any] | None = None) -> str | None:
    """Compatibility wrapper for callers that import the USB/IP helper directly."""
    if config is not None:
        username, hostname = _parse_host_address(device_host)
        for credential in config.get('client_ssh_credentials', []):
            credential_host = str(
                credential.get('device_host') or ''
            ).strip()
            credential_username = str(
                credential.get('username') or ''
            ).strip()
            credential_hostname = str(
                credential.get('host')
                or credential.get('hostname')
                or ''
            ).strip()
            if credential_host == device_host or (
                credential_username == username
                and credential_hostname == hostname
            ):
                return credential.get('password')
        for credential in config.get('client_ssh_credentials', []):
            if credential.get('username') == username:
                return credential.get('password')
        return None
    return usbip_manager.config_manager.find_device_host_password(
        device_host,
        config,
    )


def detach_ubuntu_usbip_ports(ssh, remote_host: str | None = '127.0.0.1', detach_all: bool = False) -> list[str]:
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
            if current_port and (detach_all or (remote_host and remote_host in block_text)):
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
        if len(parts) >= 2 and parts[1].lower() == "fastboot":
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
        usbip_attach_host: str | None = None
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
            username, hostname = _parse_host_address(device_host)
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

                self.ssh_manager.execute_command(win_ssh, 'taskkill /F /IM adb.exe /T')

                busids = self._find_android_devices(win_ssh, config)
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
                        busids
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
                        self.probe_protocol_status(ubuntu_ssh),
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
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
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
            # get_pty=True: usbipd list needs an interactive (PTY) session to return the full device table.
            stdout, stderr, code = self.ssh_manager.execute_command(
                ssh,
                'usbipd list',
                timeout=15,
                get_pty=True
            )
            output = "\n".join(part for part in (stdout, stderr) if part)

            logger.info(f"USB/IP devices (code={code}):\n{output}")

            devices = parse_usbipd_android_busids(output, config.get('usbip_vid_pid'))
            logger.info(f"Found USB/IP devices: {devices}")
            return devices

        except Exception as e:
            logger.error(f"Error finding Android devices: {e}")
            return []

    def _bind_devices(self, ssh, busids: list[str]) -> list[str]:
        bound = []
        for busid in busids:
            try:
                stdout, _, _ = self.ssh_manager.execute_command(ssh, f'usbipd list | findstr {busid}')

                if 'Shared' in stdout:
                    logger.info(f"Device {busid} already shared")
                    bound.append(busid)
                    continue
                elif 'Attached' in stdout:
                    # Detach first
                    self.ssh_manager.execute_command(ssh, f'usbipd detach --busid {busid}', timeout=15)
                    time.sleep(1)

                # Bind
                self.ssh_manager.execute_command(ssh, f'usbipd bind --busid {busid}', timeout=15)
                time.sleep(2)
                logger.info(f"Device {busid} bound")
                bound.append(busid)

            except Exception as e:
                logger.error(f"Error binding device {busid}: {e}")

        return bound

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
        busids: list[str]
    ) -> tuple[list[str], list[str]]:
        """Attach the busids on Ubuntu, returning (attached_busids, newly-seen adb device ids)."""
        try:
            stdout_before, _, _ = self.ssh_manager.execute_command(ssh, 'adb devices')
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
                protocol_status = self.probe_protocol_status(ssh)
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
                stdout_after, _, _ = self.ssh_manager.execute_command(ssh, 'adb devices', timeout=8)
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

            # No new devices: still return a previously-recorded USB/IP device if it's still online.
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

    def probe_protocol_status(self, ssh) -> dict[str, Any]:
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
            adb_out, adb_err, _ = self.ssh_manager.execute_command(ssh, "adb devices", timeout=8)
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
        return f'✅ USB/IP传输已连接，等待设备枚举完成'

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
        """
        自动安装 usbipd 到 Windows 主机

        Args:
            ssh: SSH 连接对象
            config: 配置字典

        Returns:
            安装结果字典
        """
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
