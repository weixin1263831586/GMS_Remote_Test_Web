"""网络辅助函数 - VPN连接、SSH路由、Ping测试"""

import asyncio
import ipaddress
import logging
import os
import re
import subprocess
from typing import Any

from foundation.processes import run_local_shell_command


_PING_RTT_PATTERN = re.compile(r"time[=<]([\d.]+)\s*ms")
_PING_AVG_PATTERN = re.compile(r"=\s*[\d.]+/([\d.]+)/")
_PING_LOSS_PATTERN = re.compile(r"(\d+)%\s*packet loss")
_ssh_manager = None
_is_config_host_local = None

logger = logging.getLogger(__name__)


def configure_network_dependencies(*, ssh_manager, is_config_host_local) -> None:
    global _ssh_manager, _is_config_host_local
    _ssh_manager = ssh_manager
    _is_config_host_local = is_config_host_local


def get_primary_vpn_target(config: dict[str, Any]) -> str:
    """获取主要VPN目标地址"""
    vpn_target = config.get('vpn_target', 'www.google.com')
    if isinstance(vpn_target, list):
        vpn_target = vpn_target[0] if vpn_target else 'www.google.com'
    return str(vpn_target or 'www.google.com')


# This UI manages NetworkManager VPN profiles. Generic tunnel interfaces such
# as Tailscale's ``tun`` connection have their own lifecycle and must not make
# the managed VPN status look connected.
VPN_CONNECTION_TYPES = {"vpn"}


def has_active_vpn_connection(nmcli_output: str) -> bool:
    """判断 nmcli -t -f NAME,TYPE,STATE 输出里是否有活跃的 VPN 类型连接。

    nmcli -t 每行 "NAME:TYPE:STATE"——只认 TYPE 列明确属于 VPN 类型，
    避免连接名或其它字段误含子串导致假阳性（误报已连接）。
    """
    return bool(parse_vpn_connection_names(nmcli_output or ""))


def check_local_vpn_connected() -> bool:
    """检查本地VPN是否已连接

    以"是否存在活跃的 VPN 类型连接"为权威判据（与 connect/disconnect 信号一致），
    ping 仅作可达性补充——避免手动断开后因 ping 目标仍可达而误报"已连接"。
    """
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'NAME,TYPE,STATE', 'connection', 'show', '--active'],
            capture_output=True, text=True, timeout=3
        )
        return has_active_vpn_connection(result.stdout)
    except Exception as e:
        logger.debug(f"[VPN Status] Local nmcli check failed: {e}")

    return False


def get_configured_vpn_connection_name(config: dict[str, Any]) -> str:
    """获取配置的VPN连接名称"""
    return str(config.get('vpn_connection_name') or os.getenv('GMS_VPN_CONNECTION_NAME', '')).strip()


def parse_vpn_connection_names(nmcli_output: str) -> list:
    """解析 nmcli 输出，返回所有 VPN 连接名称列表"""
    names = []
    for line in nmcli_output.splitlines():
        parts = re.split(r"(?<!\\):", line)
        if len(parts) >= 2 and parts[1].strip().lower() in VPN_CONNECTION_TYPES:
            names.append(parts[0].replace(r'\:', ':').strip())
    return names


async def execute_config_host_command(
    config: dict[str, Any],
    ssh,
    command: str,
    timeout: int,
) -> tuple[str, str, int]:
    """在配置主机上执行命令（本地或远程）"""
    if _is_config_host_local(config):
        return await asyncio.to_thread(run_local_shell_command, command, timeout)
    # SSH 探测为阻塞调用，在线程中执行。
    return await asyncio.to_thread(_ssh_manager.execute_command, ssh, command, timeout)


async def resolve_vpn_connection_name(
    config: dict[str, Any],
    ssh=None,
    active_only: bool = False,
) -> str:
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
