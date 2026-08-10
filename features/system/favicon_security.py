"""Security policy for outbound favicon requests."""

from __future__ import annotations

import asyncio
import os
import socket

from aiohttp.abc import AbstractResolver, ResolveResult

from foundation.outbound import resolve_outbound_target, validate_outbound_url


def allowed_private_favicon_hosts() -> set[str]:
    """Return private hosts explicitly trusted by deployment configuration."""
    return {
        item.strip().rstrip(".").lower()
        for item in os.getenv("GMS_FAVICON_ALLOWED_PRIVATE_HOSTS", "").split(",")
        if item.strip()
    }


def validated_favicon_url(url: str) -> str:
    return validate_outbound_url(
        url,
        allowed_private_hosts=allowed_private_favicon_hosts(),
    )


class FaviconResolver(AbstractResolver):
    """Resolve favicon hosts to pre-validated IPs to prevent DNS rebinding."""

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        host_for_url = f"[{host}]" if ":" in host else host
        scheme = "https" if port == 443 else "http"
        target = await asyncio.to_thread(
            resolve_outbound_target,
            f"{scheme}://{host_for_url}:{port}",
            allowed_private_hosts=allowed_private_favicon_hosts(),
        )
        return [
            ResolveResult(
                hostname=host,
                host=address,
                port=port,
                family=socket.AF_INET6 if ":" in address else socket.AF_INET,
                proto=socket.IPPROTO_TCP,
                flags=socket.AI_NUMERICHOST,
            )
            for address in target.addresses
        ]

    async def close(self) -> None:
        return None
