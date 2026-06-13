"""客户端识别辅助函数 - IP解析、来源判断、Tailscale、远程命令"""

import ipaddress
import logging
import urllib.parse
from typing import Any, Dict, Optional, Tuple

from core.common_utils import CommonUtils
from core.config import config_manager
from modules.client_manager import client_manager
from modules.ssh_async import ssh_async_manager

logger = logging.getLogger(__name__)


def get_client_id_from_request(request) -> str:
    """从请求中获取client_id（优先从配置文件读取用户名）"""
    client_ip = get_client_ip(request)

    config = config_manager.load_config()
    client_hosts = config.get('client_hosts', {})

    if client_ip in client_hosts:
        username = client_hosts[client_ip]
    else:
        username = request.headers.get('X-Client-Username')
        if username and request.headers.get('X-Client-Username-Encoding') == 'percent':
            username = urllib.parse.unquote(username)
        if not username or username == 'unknown':
            username = 'unknown'

    return client_manager.get_client_id(client_ip, username)


def get_client_ip(request, fallback_ip: Optional[str] = None) -> str:
    """提取客户端真实IP地址（支持代理）。"""
    if fallback_ip:
        return fallback_ip
    for header in ('X-Forwarded-For', 'X-Real-IP'):
        value = request.headers.get(header, '').strip()
        if value:
            return value.split(',')[0].strip()
    return request.client.host if request.client else 'unknown'


def parse_client_id(client_id: str) -> tuple[str, str]:
    """解析 username@ip 格式，用户名里含 @ 时也能保留。"""
    if not client_id or '@' not in client_id:
        return client_id or 'unknown', 'unknown'
    username, client_ip = client_id.rsplit('@', 1)
    return username or 'unknown', client_ip or 'unknown'


def get_client_source(ip: str) -> Dict[str, str]:
    """按客户端 IP 判断来源类型。"""
    try:
        ip_obj = ipaddress.ip_address(ip)
        if ip_obj.is_private:
            return {'source': 'internal', 'source_label': '内网'}
        return {'source': 'public', 'source_label': '公网'}
    except (ValueError, TypeError):
        return {'source': 'unknown', 'source_label': '未知'}


def is_public_origin_request(request) -> bool:
    """Return True when the browser is accessing through a Tailscale network."""
    if not request:
        return False
    hosts = [
        (request.headers.get('host') or '').lower(),
        (request.headers.get('x-forwarded-host') or '').lower(),
    ]
    for part in hosts:
        if part and part.split(':')[0].startswith('100.'):
            return True
    if any('tailscale.com' in h for h in hosts):
        return True
    origin = (request.headers.get('origin') or '').lower()
    return 'tailscale.com' in origin


def resolve_tailscale_device_host(request, client_id: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (device_host, usbip_attach_host) for Tailscale-origin requests."""
    if not is_public_origin_request(request):
        return None, None
    username, client_ip = parse_client_id(client_id)
    if not client_ip or client_ip == 'unknown':
        return None, None
    device_host = f"{username}@{client_ip}" if username and username != 'unknown' else client_ip
    return device_host, client_ip


async def run_windows_command_via_ssh(device_host: str, command: str, timeout: int = 30) -> dict:
    """Execute a command on a Windows client via direct SSH over Tailscale."""
    config = config_manager.load_config()
    password = config_manager.find_device_host_password(device_host, config) or config.get('device_pswd', '')
    if not password:
        return {'returncode': -1, 'stdout': '', 'stderr': f'No SSH credentials for {device_host}'}
    username, hostname = CommonUtils.parse_host_address(device_host)
    if not username or not hostname:
        return {'returncode': -1, 'stdout': '', 'stderr': f'Invalid device_host: {device_host}'}
    try:
        exit_code, stdout, stderr = await ssh_async_manager.execute_command_simple(
            hostname, username, password, command, timeout=timeout
        )
        return {'returncode': exit_code, 'stdout': stdout, 'stderr': stderr}
    except Exception as e:
        return {'returncode': -1, 'stdout': '', 'stderr': str(e)}


async def probe_windows_usbipd(device_host: str) -> Dict[str, Any]:
    """Check if usbipd is installed on the Windows client via SSH."""
    result = await run_windows_command_via_ssh(device_host, 'usbipd --version', timeout=15)
    output = (result.get('stdout') or '').strip() or (result.get('stderr') or '').strip()
    return {
        'installed': result.get('returncode', -1) == 0 and bool(output),
        'version': output,
        'raw': result,
    }


def is_manual_username_fallback_error(error: Optional[str]) -> bool:
    """判断用户名识别失败是否属于可手动保存的网络不可达场景。"""
    if not error:
        return True
    error_lower = error.lower()
    return any(keyword in error_lower for keyword in (
        'network is unreachable',
        'no route to host',
        'connection refused',
        'timed out',
        'timeout',
        '连接超时',
        '连接被拒绝',
        '网络不可达',
        '无法访问',
    ))


_SENSITIVE_FIELDS = ('password', 'pswd', 'api_key', 'secret', 'token', 'private_key')


def _mask_value(value: str) -> str:
    return value[:4] + '*' * (len(value) - 4) if len(value) > 4 else '****'


def hide_sensitive_info(config: dict) -> dict:
    """隐藏配置中的敏感信息。"""
    if not isinstance(config, dict):
        return config
    safe = {}
    for key, value in config.items():
        if any(s in key.lower() for s in _SENSITIVE_FIELDS) and isinstance(value, str) and value:
            safe[key] = _mask_value(value)
        elif isinstance(value, dict):
            safe[key] = hide_sensitive_info(value)
        elif isinstance(value, list):
            safe[key] = [hide_sensitive_info(item) if isinstance(item, dict) else item for item in value]
        else:
            safe[key] = value
    return safe
