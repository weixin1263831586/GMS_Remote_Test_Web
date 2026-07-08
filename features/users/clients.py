"""客户端识别辅助函数 - IP解析、来源判断、Tailscale、远程命令"""

import ipaddress
import logging
from typing import Any

from features.auth import get_authenticated_user, require_authenticated_user
from foundation.networking import parse_host_address

from . import runtime


logger = logging.getLogger(__name__)


def get_client_id_from_request(request) -> str:
    """Return a stable client id for runtime state.

    Platform login is optional for ordinary client usage. Authenticated users
    are identified by their account ``username`` (stable across networks and
    machines); anonymous clients fall back to username@ip resolved from
    server-side client host config and request metadata.
    """
    user = get_authenticated_user(request)
    if user:
        return user.username
    return get_client_display_id_from_request(request)


def owner_id_from_request(request, *, default: str = "legacy") -> str:
    """Resolve a per-owner storage key for the current request.

    Authenticated requests use the account ``username``; unauthenticated (or internal/no-request) callers fall
    back to the anonymous client id, then ``default``. Centralizes the ``try/except`` resolution that redmine/gerrit previously triplicated.
    """
    if request is None:
        return default
    try:
        return require_authenticated_user(request).username
    except Exception:
        return get_client_id_from_request(request) or default


def get_client_ip(request, fallback_ip: str | None = None) -> str:
    """提取客户端真实IP地址（支持代理）。"""
    if fallback_ip:
        return fallback_ip
    for header in ('X-Forwarded-For', 'X-Real-IP'):
        value = request.headers.get(header, '').strip()
        if value:
            return value.split(',')[0].strip()
    return request.client.host if request.client else 'unknown'


def get_client_username_from_request(request, fallback: str | None = None) -> str:
    """Return the client machine username for display/SSH routing."""
    client_ip = get_client_ip(request)
    try:
        config = runtime.config_manager.load_config()
        client_hosts = config.get('client_hosts') or {}
        username = str(client_hosts.get(client_ip) or '').strip()
        if username and username != 'unknown':
            return username
    except Exception:
        pass

    user = get_authenticated_user(request)
    username = str(getattr(user, 'username', '') or fallback or '').strip()
    return username or 'unknown'


def get_client_display_id_from_request(request) -> str:
    """Return username@ip for UI and tools that require a reachable client host."""
    client_ip = get_client_ip(request)
    username = get_client_username_from_request(request)
    if username and username != 'unknown' and client_ip and client_ip != 'unknown':
        return f"{username}@{client_ip}"
    return username if username and username != 'unknown' else client_ip


def parse_client_id(client_id: str) -> tuple[str, str]:
    """解析 username@ip 格式，用户名里含 @ 时也能保留。"""
    if not client_id or '@' not in client_id:
        return client_id or 'unknown', 'unknown'
    username, client_ip = client_id.rsplit('@', 1)
    return username or 'unknown', client_ip or 'unknown'


def get_client_source(ip: str) -> dict[str, str]:
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


def resolve_tailscale_device_host(request, client_id: str) -> tuple[str | None, str | None]:
    """Return (device_host, usbip_attach_host) for Tailscale-origin requests."""
    if not is_public_origin_request(request):
        return None, None
    user = get_authenticated_user(request)
    username = user.username if user else parse_client_id(client_id)[0]
    client_ip = get_client_ip(request)
    if not client_ip or client_ip == 'unknown':
        return None, None
    device_host = f"{username}@{client_ip}" if username and username != 'unknown' else client_ip
    return device_host, client_ip


async def run_windows_command_via_ssh(device_host: str, command: str, timeout: int = 30) -> dict:
    """Execute a command on a Windows client via direct SSH over Tailscale."""
    config = runtime.config_manager.load_config()
    password = runtime.config_manager.find_device_host_password(device_host, config) or config.get('device_pswd', '')
    if not password:
        return {'returncode': -1, 'stdout': '', 'stderr': f'No SSH credentials for {device_host}'}
    username, hostname = parse_host_address(device_host)
    if not username or not hostname:
        return {'returncode': -1, 'stdout': '', 'stderr': f'Invalid device_host: {device_host}'}
    try:
        exit_code, stdout, stderr = await runtime.ssh_async_manager.execute_command_simple(
            hostname, username, password, command, timeout=timeout
        )
        return {'returncode': exit_code, 'stdout': stdout, 'stderr': stderr}
    except Exception as e:
        return {'returncode': -1, 'stdout': '', 'stderr': str(e)}


async def probe_windows_usbipd(device_host: str) -> dict[str, Any]:
    """Check if usbipd is installed on the Windows client via SSH."""
    result = await run_windows_command_via_ssh(device_host, 'usbipd --version', timeout=15)
    output = (result.get('stdout') or '').strip() or (result.get('stderr') or '').strip()
    return {
        'installed': result.get('returncode', -1) == 0 and bool(output),
        'version': output,
        'raw': result,
    }


def is_manual_username_fallback_error(error: str | None) -> bool:
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
        # wifi 节点是测试台共享的 SSID/密码，前端「连接 Wi-Fi」弹框需要明文预填，故整段保留。
        if key == "wifi" and isinstance(value, dict):
            safe[key] = value
        elif any(s in key.lower() for s in _SENSITIVE_FIELDS) and isinstance(value, str) and value:
            safe[key] = _mask_value(value)
        elif isinstance(value, dict):
            safe[key] = hide_sensitive_info(value)
        elif isinstance(value, list):
            safe[key] = [hide_sensitive_info(item) if isinstance(item, dict) else item for item in value]
        else:
            safe[key] = value
    return safe
