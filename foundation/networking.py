from __future__ import annotations

import socket
import subprocess
import time
from urllib.parse import urlparse


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


# Cached set of all local interface IPs. gethostname()/getaddrinfo() only
# resolve to the *primary* address, which drifts (e.g. hostname resolving to a
# Tailscale IP instead of the LAN IP) and makes a genuinely-local configured
# host look remote — turning local ops into doomed SSH-to-self. Enumerate every
# NIC address instead so host/IP mismatches can't cause false negatives.
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

    # Primary resolution path (what the old code relied on alone — kept as one
    # signal among many).
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

    # Outbound-socket trick: the IP the kernel would use to reach the internet.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('8.8.8.8', 80))
            local_ips.add(sock.getsockname()[0])
    except OSError:
        pass

    # `hostname -I` lists every NIC address — the most reliable source on Linux
    # and the one that catches multi-NIC hosts (LAN + Tailscale + docker, ...).
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


def get_local_ips() -> set[str]:
    """Public alias for the cached local-IP set used by is_local_host."""
    return _collect_local_ips()


def is_local_host(host: str) -> bool:
    if not host:
        return False
    if '@' in host:
        host = host.rsplit('@', 1)[1]
    normalized = host.strip().strip('[]')
    return normalized in _collect_local_ips()
