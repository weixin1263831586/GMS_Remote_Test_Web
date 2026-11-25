"""Bounded public HTTPS document retrieval using the shared outbound boundary."""

from __future__ import annotations

import ipaddress
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request

from foundation.outbound import open_outbound, resolve_outbound_target


MAX_BYTES = 12 * 1024 * 1024
TIMEOUT = 15


def validate_url(url: str) -> str:
    if len(url) > 2048 or any(ord(c) < 33 for c in url):
        raise ValueError('Invalid document URL')
    parts = urlsplit(url)
    host = parts.hostname or ''
    if (
        parts.scheme != 'https'
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
        or not re.fullmatch(r'[a-zA-Z0-9.-]+', host)
        or '.' not in host
        or parts.fragment
        or '\\' in url
    ):
        raise ValueError('Only public HTTPS document URLs are allowed')
    if host.lower().rstrip('.') in {'docs.partner.android.com', 'accounts.google.com'}:
        raise ValueError('Restricted source requires an authorized connector')
    if parts.query:
        raise ValueError('Public document URLs must not contain query parameters')
    return host


def _target(url):
    validate_url(url)
    target = resolve_outbound_target(url)
    if any(ipaddress.ip_address(addr).is_multicast for addr in target.addresses):
        raise ValueError('Non-public destination rejected')
    return target


def public_addresses(host: str) -> list[str]:
    return list(_target(f'https://{host}/').addresses)


def fetch_html(url: str, *, etag: str = '', modified: str = ''):
    target = _target(url)
    headers = {
        'Accept': 'text/html',
        'Accept-Language': 'en',
        'Accept-Encoding': 'identity',
        'User-Agent': 'GMS-Remote-Test-Knowledge/1.0',
    }
    if etag:
        headers['If-None-Match'] = etag
    if modified:
        headers['If-Modified-Since'] = modified
    deadline = time.monotonic() + TIMEOUT
    try:
        response = open_outbound(Request(url, headers=headers), target, timeout=TIMEOUT)
    except HTTPError as exc:
        with exc:
            if exc.code == 304:
                return None, etag, modified
            raise ValueError(f'HTTP {exc.code}; redirects are not followed') from exc
    with response:
        if response.status != 200:
            raise ValueError(f'HTTP {response.status}')
        if response.headers.get_content_type() not in {'text/html', 'application/xhtml+xml'}:
            raise ValueError('Unsupported document content type')
        if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            raise ValueError('Unsupported content encoding')
        chunks = bytearray()
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError('Document fetch deadline exceeded')
            block = response.read1(65536)
            if not block:
                break
            chunks.extend(block)
            if len(chunks) > MAX_BYTES:
                raise ValueError('Document exceeds size limit')
        return bytes(chunks), response.headers.get('ETag', ''), response.headers.get('Last-Modified', '')
