"""Public HTTPS document retrieval with DNS pinning and bounded response size."""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

MAX_BYTES = 12 * 1024 * 1024
TIMEOUT = 15


def validate_url(url: str) -> str:
    if len(url) > 2048 or any(ord(c) < 33 for c in url):
        raise ValueError("Invalid document URL")
    parts = urlsplit(url)
    host = parts.hostname or ""
    if (parts.scheme != "https" or parts.username or parts.password or
            parts.port not in (None, 443) or not re.fullmatch(r"[a-zA-Z0-9.-]+", host)
            or "." not in host or parts.fragment or "\\" in url):
        raise ValueError("Only public HTTPS document URLs are allowed")
    if host.lower() in {"docs.partner.android.com", "accounts.google.com"}:
        raise ValueError("Restricted source requires an authorized connector")
    return host


def public_addresses(host: str) -> list[str]:
    addresses = list(dict.fromkeys(row[4][0] for row in
                                    socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(addr).is_global or
                            ipaddress.ip_address(addr).is_multicast for addr in addresses):
        raise ValueError("Non-public destination rejected")
    return addresses


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str):
        super().__init__(host, timeout=TIMEOUT, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch_html(url: str, *, etag: str = "", modified: str = ""):
    host = validate_url(url)
    parts = urlsplit(url)
    connection = _PinnedHTTPS(host, public_addresses(host)[0])
    headers = {"Accept": "text/html", "Accept-Language": "en", "Accept-Encoding": "identity",
               "User-Agent": "GMS-Remote-Test-Knowledge/1.0"}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    deadline = time.monotonic() + TIMEOUT
    try:
        connection.request("GET", (parts.path or "/") + ("?" + parts.query if parts.query else ""),
                           headers=headers)
        response = connection.getresponse()
        if response.status == 304:
            return None, etag, modified
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}; redirects are not followed")
        if response.headers.get_content_type() not in {"text/html", "application/xhtml+xml"}:
            raise ValueError("Unsupported document content type")
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise ValueError("Unsupported content encoding")
        chunks = bytearray()
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("Document fetch deadline exceeded")
            block = response.read1(65536)
            if not block:
                break
            chunks.extend(block)
            if len(chunks) > MAX_BYTES:
                raise ValueError("Document exceeds size limit")
        return bytes(chunks), response.getheader("ETag", ""), response.getheader("Last-Modified", "")
    finally:
        connection.close()
