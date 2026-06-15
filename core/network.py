"""网络辅助函数 - VPN连接、SSH路由、Ping测试"""

import asyncio
import ipaddress
import logging
import os
import subprocess
from typing import Any, Dict, Tuple

from core.ssh import ssh_manager
from core.state import _PING_RTT_PATTERN, _PING_AVG_PATTERN, _PING_LOSS_PATTERN

logger = logging.getLogger(__name__)


def get_primary_vpn_target(config: Dict[str, Any]) -> str:
    """获取主要VPN目标地址"""
    vpn_target = config.get('vpn_target', 'www.google.com')
    if isinstance(vpn_target, list):
        vpn_target = vpn_target[0] if vpn_target else 'www.google.com'
    return str(vpn_target or 'www.google.com')


def check_local_vpn_connected(vpn_target: str) -> bool:
    """检查本地VPN是否已连接"""
    for _ in range(2):
        try:
            result = subprocess.run(
                ['ping', '-c', '1', '-W', '1', vpn_target],
                capture_output=True, text=True, timeout=3
            )
            output = f"{result.stdout}\n{result.stderr}"
            if result.returncode == 0 and ('1 received' in output or 'bytes from' in output):
                return True
        except Exception as e:
            logger.debug(f"[VPN Status] Local ping check failed: {e}")

    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'NAME,TYPE,STATE', 'connection', 'show', '--active'],
            capture_output=True, text=True, timeout=3
        )
        output = result.stdout.lower()
        return 'vpn' in output or 'tun' in output or 'tap' in output
    except Exception as e:
        logger.debug(f"[VPN Status] Local nmcli check failed: {e}")
        return False


def get_configured_vpn_connection_name(config: Dict[str, Any]) -> str:
    """获取配置的VPN连接名称"""
    return str(config.get('vpn_connection_name') or os.getenv('GMS_VPN_CONNECTION_NAME', '')).strip()


def parse_vpn_connection_names(nmcli_output: str) -> list:
    """解析 nmcli 输出，返回所有 VPN 连接名称列表"""
    names = []
    for line in nmcli_output.splitlines():
        parts = line.split(':')
        if len(parts) >= 2 and 'vpn' in parts[1].lower():
            names.append(parts[0].replace(r'\:', ':').strip())
    return names


async def execute_config_host_command(config: Dict[str, Any], ssh, command: str, timeout: int) -> Tuple[str, str, int]:
    """在配置主机上执行命令（本地或远程）"""
    from core.test_suite_utils import is_config_host_local

    if is_config_host_local(config):
        return await asyncio.to_thread(run_local_shell_command, command, timeout)
    return ssh_manager.execute_command(ssh, command, timeout=timeout)


async def resolve_vpn_connection_name(config: Dict[str, Any], ssh=None, active_only: bool = False) -> str:
    """解析VPN连接名称"""
    vpn_name = get_configured_vpn_connection_name(config)
    if vpn_name:
        return vpn_name

    if active_only:
        cmd = "nmcli -t -f NAME,TYPE,STATE connection show --active 2>/dev/null"
    else:
        cmd = "nmcli -t -f NAME,TYPE connection show 2>/dev/null"
    output, _, _ = await execute_config_host_command(config, ssh, cmd, timeout=5)
    names = parse_vpn_connection_names(output)
    return names[0] if names else ""


def run_local_shell_command(command: str, timeout: int = 30) -> Tuple[str, str, int]:
    """在本地执行 shell 命令"""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return '', 'Command timed out', -1
    except Exception as e:
        return '', str(e), -1


def are_same_network(ip1: str, ip2: str, prefix_len: int = 24) -> bool:
    """检查两个IP是否在同一网段"""
    try:
        network1 = ipaddress.IPv4Network(f"{ip1}/{prefix_len}", strict=False)
        network2 = ipaddress.IPv4Network(f"{ip2}/{prefix_len}", strict=False)
        return network1 == network2
    except (ipaddress.AddressValueError, ValueError):
        parts1 = ip1.split('.')
        parts2 = ip2.split('.')
        if len(parts1) == 4 and len(parts2) == 4:
            return parts1[:3] == parts2[:3]
        return False


def _validate_ip_address(ip: str) -> bool:
    """验证IPv4地址格式和范围"""
    try:
        ipaddress.IPv4Address(ip)
        return True
    except (ipaddress.AddressValueError, ValueError):
        return False


def _extract_network(ip: str) -> str:
    """从IP地址提取网络地址"""
    try:
        network = ipaddress.IPv4Network(f"{ip}/24", strict=False)
        return str(network.network_address)
    except (ipaddress.AddressValueError, ValueError):
        return '.'.join(ip.split('.')[:3]) + '.0'


def _parse_ping_output(ping_output: str, exit_status: int) -> tuple[bool, str]:
    """解析ping输出，返回(可达性, 延迟)"""
    if exit_status == 0:
        if "0% packet loss" in ping_output:
            time_match = _PING_RTT_PATTERN.search(ping_output)
            if time_match:
                return True, f"{time_match.group(1)}ms"
            else:
                avg_match = _PING_AVG_PATTERN.search(ping_output)
                if avg_match:
                    return True, f"{avg_match.group(1)}ms"
                else:
                    return True, '<10ms'
        elif "packet loss" in ping_output:
            loss_match = _PING_LOSS_PATTERN.search(ping_output)
            if loss_match:
                loss_percent = int(loss_match.group(1))
                if loss_percent < 100:
                    return True, f'{loss_percent}% 丢包'
                else:
                    return False, 'N/A (100% 丢包)'
            else:
                return False, 'N/A'
        else:
            return True, 'N/A'
    else:
        if "100% packet loss" in ping_output or "Network is unreachable" in ping_output:
            return False, 'N/A (不可达)'
        else:
            return False, 'N/A'


def _generate_route_commands(test_network: str, target_network: str, test_host_ip: str) -> dict:
    """生成路由命令"""
    test_gateway = '.'.join(test_network.split('.')[:3]) + '.1'

    return {
        'windows': [
            "# 在测试主机上执行以下命令:",
            "# 添加到客户端网段的路由（通过测试主机网关）",
            f"route add {target_network} mask 255.255.255.0 {test_gateway}",
            "# 检查路由表: route print",
            f"# 删除路由: route delete {target_network}"
        ],
        'linux': [
            "# 在测试主机上执行以下命令:",
            "# 添加到客户端网段的路由（通过测试主机网关）",
            f"sudo ip route add {target_network}/24 via {test_gateway}",
            "# 检查路由表: ip route show",
            f"# 删除路由: sudo ip route del {target_network}/24"
        ]
    }
