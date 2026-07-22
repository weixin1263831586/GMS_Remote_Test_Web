"""Validation helpers for server-side HTTP(S) requests."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse


class UnsafeOutboundURLError(ValueError):
    """Raised when an outbound URL could reach an untrusted network target."""


# 调用方统一捕获此边界错误。
UnsafeOutboundURL = UnsafeOutboundURLError


@dataclass(frozen=True)
class ResolvedOutboundTarget:
    url: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


def normalized_hostname(value: str) -> str:
    return str(value or "").strip().rstrip(".").lower()


def url_hostname(url: str) -> str:
    try:
        return normalized_hostname(urlparse(str(url or "")).hostname or "")
    except ValueError:
        return ""


def resolve_outbound_target(
    url: str,
    *,
    allowed_private_hosts: set[str] | None = None,
) -> ResolvedOutboundTarget:
    """Resolve and validate an HTTP(S) target for connection pinning.

    Callers that invoke another HTTP client should pin the returned addresses;
    otherwise DNS rebinding can change the destination after validation.
    """

    text = str(url or "").strip()
    try:
        parsed = urlparse(text)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeOutboundURL("Invalid outbound URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise UnsafeOutboundURL("Only HTTP(S) URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeOutboundURL("Credentials in URLs are not allowed")

    host = normalized_hostname(parsed.hostname)
    allowed = {
        normalized_hostname(item) for item in (allowed_private_hosts or set())
        if normalized_hostname(item)
    }
    if host not in allowed and port not in {None, 80, 443}:
        raise UnsafeOutboundURL("Non-standard ports require an authorized host")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                host,
                port or (443 if parsed.scheme.lower() == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        raise UnsafeOutboundURL("Outbound host cannot be resolved") from exc
    if not addresses:
        raise UnsafeOutboundURL("Outbound host cannot be resolved")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(str(address).split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeOutboundURL("Outbound host resolved unexpectedly") from exc
        if host not in allowed and not ip.is_global:
            raise UnsafeOutboundURL("Private or reserved outbound address is blocked")
    return ResolvedOutboundTarget(
        url=parsed.geturl(),
        hostname=host,
        port=port or (443 if parsed.scheme.lower() == "https" else 80),
        addresses=tuple(sorted(addresses)),
    )


def validate_outbound_url(
    url: str,
    *,
    allowed_private_hosts: set[str] | None = None,
) -> str:
    """Allow HTTP(S) URLs whose resolved addresses are public or pre-authorized.

    Private destinations are allowed only by exact hostname. The validation is
    repeated immediately before each request and redirects are disabled by
    callers, preventing a redirect from changing the trust boundary.
    """

    text = str(url or "").strip()
    try:
        parsed = urlparse(text)
        _ = parsed.port
    except ValueError as exc:
        raise UnsafeOutboundURL("Invalid outbound URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise UnsafeOutboundURL("Only HTTP(S) URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeOutboundURL("Credentials in URLs are not allowed")
    allowed = {
        normalized_hostname(item)
        for item in (allowed_private_hosts or set())
        if normalized_hostname(item)
    }
    if normalized_hostname(parsed.hostname) in allowed:
        # 私网目标由组织配置授权，连接固定场景需直接解析目标地址。
        return parsed.geturl()
    return resolve_outbound_target(url).url


def same_http_origin(left: str, right: str) -> bool:
    """Return whether two HTTP(S) URLs have the same normalized origin."""

    try:
        first = urlparse(str(left or ""))
        second = urlparse(str(right or ""))
        if first.scheme.lower() not in {"http", "https"}:
            return False
        if second.scheme.lower() not in {"http", "https"}:
            return False
        first_port = first.port or (443 if first.scheme.lower() == "https" else 80)
        second_port = second.port or (443 if second.scheme.lower() == "https" else 80)
    except ValueError:
        return False
    return (
        first.scheme.lower() == second.scheme.lower()
        and normalized_hostname(first.hostname or "")
        == normalized_hostname(second.hostname or "")
        and first_port == second_port
    )
