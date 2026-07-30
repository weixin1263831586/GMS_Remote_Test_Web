"""客户端识别辅助函数 - IP解析、来源判断、Tailscale、远程命令"""

import ipaddress
import logging
from typing import Any

from features.auth import get_authenticated_user
from foundation.networking import parse_host_address

from . import runtime


logger = logging.getLogger(__name__)
_DEFAULT_TRUSTED_PROXIES = ('127.0.0.0/8', '::1/128')


def _trusted_proxy_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    configured: Any = None
    try:
        config = runtime.config_manager.load_config()
        configured = config.get('trusted_proxies')
    except Exception:
        pass
    values = configured if isinstance(configured, list) else _DEFAULT_TRUSTED_PROXIES
    networks = []
    for value in values:
        try:
            networks.append(ipaddress.ip_network(str(value).strip(), strict=False))
        except ValueError:
            logger.warning('Ignoring invalid trusted proxy network: %s', value)
    return networks


def _is_trusted_proxy(host: str, networks) -> bool:
    try:
        address = ipaddress.ip_address(str(host or '').strip())
    except ValueError:
        return False
    return any(address in network for network in networks)


def _valid_ip(value: str) -> str | None:
    candidate = str(value or '').strip().strip('[]')
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def get_client_id_from_request(request) -> str:
    """返回稳定的运行时客户端 ID；匿名模式使用用户名和 IP。"""
    user = get_authenticated_user(request)
    return user.id if user else get_client_display_id_from_request(request)


def owner_id_from_request(request) -> str:
    """Return an account id, or the stable anonymous client id in development."""
    user = get_authenticated_user(request)
    return user.id if user else get_client_display_id_from_request(request)


def get_client_ip(request) -> str:
    """Resolve client IP without trusting forwarding headers from browsers."""
    peer = request.client.host if request.client else 'unknown'
    networks = _trusted_proxy_networks()
    if not _is_trusted_proxy(peer, networks):
        return _valid_ip(peer) or peer

    forwarded = request.headers.get('X-Forwarded-For', '').strip()
    if forwarded:
        chain = [item for item in (_valid_ip(part) for part in forwarded.split(',')) if item]
        # Walk from the nearest hop towards the browser. The first address not
        # belonging to a trusted proxy is the authoritative client.
        for candidate in reversed(chain):
            if not _is_trusted_proxy(candidate, networks):
                return candidate
    real_ip = _valid_ip(request.headers.get('X-Real-IP', ''))
    if real_ip:
        return real_ip
    return _valid_ip(peer) or peer


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


def normalize_client_display_id(value: str) -> str:
    """Collapse a repeated host suffix in a persisted username@ip identity."""
    normalized = str(value or '').strip()
    while '@' in normalized:
        prefix, separator, host = normalized.rpartition('@')
        if not separator or not host or not prefix.endswith(f'@{host}'):
            break
        normalized = prefix
    return normalized


def format_client_display_id(username: str, client_ip: str) -> str:
    """Build username@ip without appending the same host more than once."""
    normalized_username = normalize_client_display_id(username)
    normalized_ip = str(client_ip or '').strip()
    if not normalized_username or normalized_username == 'unknown':
        return normalized_ip
    if not normalized_ip or normalized_ip == 'unknown':
        return normalized_username
    if normalized_username.endswith(f'@{normalized_ip}'):
        return normalized_username
    return f"{normalized_username}@{normalized_ip}"


def get_client_display_id_from_request(request) -> str:
    """Return username@ip for UI and tools that require a reachable client host."""
    client_ip = get_client_ip(request)
    username = get_client_username_from_request(request)
    return format_client_display_id(username, client_ip)


def resolve_client_display_id(
    client_id: str,
    stored_display_id: str = "",
) -> str:
    """Resolve an internal account id to the user-management display identity."""
    identity = str(client_id or "").strip()
    stored = normalize_client_display_id(stored_display_id)
    account_username = ""
    try:
        from features.auth import auth_service

        account = next(
            (
                item for item in auth_service.list_users()
                if str(item.get("id") or "") == identity
            ),
            None,
        )
        if account:
            account_username = normalize_client_display_id(str(
                account.get("username") or account.get("display_name") or ""
            ).strip())
    except Exception:
        pass
    account_base_username = (
        parse_client_id(account_username)[0] if account_username else ""
    )

    try:
        with runtime.global_state.user_states_lock:
            states = list(runtime.global_state.user_states.items())
        for state_id, state in states:
            state_display = normalize_client_display_id(
                state.get("display_client_id") or ""
            )
            state_username = normalize_client_display_id(
                state.get("client_username") or ""
            )
            state_ip = str(state.get("client_ip") or "").strip()
            matches = (
                str(state_id) == identity
                or (stored and state_display == stored)
                or (
                    account_base_username
                    and parse_client_id(state_username)[0] == account_base_username
                )
            )
            if not matches:
                continue
            if state_display:
                return state_display
            if state_username and state_ip:
                return format_client_display_id(state_username, state_ip)
    except Exception:
        pass

    if account_username:
        try:
            config = runtime.config_manager.load_config()
            matching_ips = [
                str(ip)
                for ip, username in (config.get("client_hosts") or {}).items()
                if str(username or "").strip() == account_base_username
            ]
            if len(matching_ips) == 1:
                return format_client_display_id(
                    account_base_username,
                    matching_ips[0],
                )
        except Exception:
            pass

    if stored and stored != identity:
        return stored
    return account_username or stored or identity or "unknown"


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
        if any(s in key.lower() for s in _SENSITIVE_FIELDS) and isinstance(value, str) and value:
            safe[key] = _mask_value(value)
        elif isinstance(value, dict):
            safe[key] = hide_sensitive_info(value)
        elif isinstance(value, list):
            safe[key] = [hide_sensitive_info(item) if isinstance(item, dict) else item for item in value]
        else:
            safe[key] = value
    return safe
