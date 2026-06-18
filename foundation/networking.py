from __future__ import annotations

import socket
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


def is_local_host(host: str) -> bool:
    if not host:
        return False
    if '@' in host:
        host = host.rsplit('@', 1)[1]
    normalized = host.strip().strip('[]')
    if normalized in {'localhost', '127.0.0.1', '::1'}:
        return True
    try:
        local_addresses = {
            info[4][0]
            for info in socket.getaddrinfo(socket.gethostname(), None)
            if info[4]
        }
        local_addresses.add(socket.gethostbyname(socket.gethostname()))
    except OSError:
        local_addresses = set()
    return normalized in local_addresses
