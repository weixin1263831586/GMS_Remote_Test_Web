from __future__ import annotations

import ipaddress
import logging
import socket
import subprocess
import time
from urllib.parse import urlparse


logger = logging.getLogger(__name__)

_DEFAULT_TRUSTED_PROXIES = ('127.0.0.0/8', '::1/128')


def _trusted_proxy_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Trusted reverse-proxy networks (config key ``trusted_proxies``).

    11.txt P2: shared here so features/auth, features/users and the agent
    enrollment rate limiter resolve client IPs identically — only proxies on
    this list may contribute X-Forwarded-For hops.

    The config source is read through :func:`_load_trusted_proxies_config` so
    tests (and future feature-scoped runtimes) can patch a single seam.
    """
    configured = _load_trusted_proxies_config()
    values = configured if isinstance(configured, list) else _DEFAULT_TRUSTED_PROXIES
    networks = []
    for value in values:
        try:
            networks.append(ipaddress.ip_network(str(value).strip(), strict=False))
        except ValueError:
            logger.warning('Ignoring invalid trusted proxy network: %s', value)
    return networks


def _load_trusted_proxies_config():
    try:
        from foundation.config import config_manager

        return config_manager.load_config().get('trusted_proxies')
    except Exception:
        return None


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


def sanitize_url(url: str) -> str:
    if not url:
        return url
    for prefix in ('view-source://', 'view-source:', 'about://', 'about:'):
        if url.startswith(prefix):
            url = url[len(prefix) :]
            break
    parsed = urlparse(url)
    return url if parsed.scheme else f'https://{url}'


def parse_host_address(host: str) -> tuple[str | None, str]:
    if '@' not in host:
        return None, host
    return tuple(host.split('@', 1))


def split_host_port(hostname: str, default_port: int = 22) -> tuple[str, int]:
    """Parse host[:port] for IPv4/hostname targets."""
    if not hostname:
        return hostname, default_port
    if hostname.count(':') == 1:
        host, port_text = hostname.rsplit(':', 1)
        if port_text.isdigit():
            return host, int(port_text)
    return hostname, default_port


# 缓存所有网卡地址，避免多网卡主机被误判为远端。
_LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1'}
_cached_local_ips: set[str] | None = None
_cached_local_ips_time = 0.0
_LOCAL_IPS_TTL = 60.0


def _collect_local_ips() -> set[str]:
    """Return the set of all addresses bound to this machine, with caching."""
    global _cached_local_ips, _cached_local_ips_time
    now = time.time()
    if _cached_local_ips is not None and (now - _cached_local_ips_time) < _LOCAL_IPS_TTL:
        return _cached_local_ips

    local_ips = set(_LOCAL_HOSTS)

    # 主机名解析结果是本机地址来源之一。
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if addr:
                local_ips.add(addr)
    except OSError:
        pass
    try:
        local_ips.add(socket.gethostbyname(socket.gethostname()))
    except OSError:
        pass

    # 获取默认出站路由使用的本机地址。
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('8.8.8.8', 80))
            local_ips.add(sock.getsockname()[0])
    except OSError:
        pass

    # Linux 下通过 hostname -I 补齐所有网卡地址。
    try:
        result = subprocess.run(
            ['hostname', '-I'],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            local_ips.update(ip for ip in result.stdout.split() if ip)
    except (OSError, subprocess.SubprocessError):
        pass

    _cached_local_ips = local_ips
    _cached_local_ips_time = now
    return local_ips


def is_local_host(host: str) -> bool:
    if not host:
        return False
    if '@' in host:
        host = host.rsplit('@', 1)[1]
    normalized = host.strip().strip('[]')
    return normalized in _collect_local_ips()
