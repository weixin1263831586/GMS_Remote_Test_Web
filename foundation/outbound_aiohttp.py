"""DNS-pinning adapter for server-side aiohttp clients."""

from __future__ import annotations

import socket

from aiohttp.abc import AbstractResolver, ResolveResult

from .outbound import ResolvedOutboundTarget, UnsafeOutboundURL, normalized_hostname


class PinnedOutboundResolver(AbstractResolver):
    """Connect only to validated addresses, retaining the URL's Host and TLS name."""

    def __init__(self, target: ResolvedOutboundTarget):
        self.target = target

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[ResolveResult]:
        actual_host = normalized_hostname(host).encode("idna").decode("ascii")
        expected_host = self.target.hostname.encode("idna").decode("ascii")
        if actual_host != expected_host or port != self.target.port:
            raise UnsafeOutboundURL("Outbound connection differs from the validated target")
        return [
            ResolveResult(
                hostname=host, host=address, port=port,
                family=socket.AF_INET6 if ":" in address else socket.AF_INET,
                proto=socket.IPPROTO_TCP, flags=socket.AI_NUMERICHOST,
            )
            for address in self.target.addresses
        ]

    async def close(self) -> None:
        return None
