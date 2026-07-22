"""Outbound trust boundary for suite archive downloads."""

from __future__ import annotations

import os
from urllib.parse import urlparse

from foundation.outbound import ResolvedOutboundTarget, resolve_outbound_target


def validate_suite_download_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Only HTTP(S) download URLs are allowed")
    return parsed.geturl()


def allowed_suite_download_hosts() -> set[str]:
    return {
        item.strip().lower().rstrip(".")
        for item in os.getenv("GMS_SUITE_DOWNLOAD_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    }


def resolve_suite_download_target(url: str) -> ResolvedOutboundTarget:
    return resolve_outbound_target(
        validate_suite_download_url(url),
        allowed_private_hosts=allowed_suite_download_hosts(),
    )


def curl_resolve_arguments(target: ResolvedOutboundTarget) -> list[str]:
    arguments: list[str] = []
    for address in target.addresses:
        curl_address = f"[{address}]" if ":" in address else address
        arguments.extend(
            [
                "--resolve",
                f"{target.hostname}:{target.port}:{curl_address}",
            ]
        )
    return arguments
