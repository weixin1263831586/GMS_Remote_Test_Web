"""Access port for user-host local device inventory owned by the devices feature.

Consumers that must not import ``features.devices`` (e.g. user management)
reach "Android devices directly attached to a user host" inventory through
this port. The composition root registers a late-bound provider; when
nothing is registered the accessor returns ``None`` so callers render "no
data" instead of failing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any


logger = logging.getLogger(__name__)

HostInventoryProvider = Callable[[str], dict[str, Any] | None]

_inventory_provider: HostInventoryProvider | None = None


def configure_host_inventory_provider(provider: HostInventoryProvider | None) -> None:
    """Register (or clear) the user-host inventory provider."""
    global _inventory_provider
    _inventory_provider = provider


def host_local_device_inventory(device_host: str) -> dict[str, Any] | None:
    """Return ``{"devices": [...], "source_os": str, "available": bool}``.

    ``None`` means the capability is not wired (single-module consumers) or
    the host has no inventory yet; callers fall back to "no data".
    """
    if _inventory_provider is None:
        return None
    try:
        return _inventory_provider(str(device_host or "").strip())
    except Exception:
        logger.warning(
            "host inventory provider failed for %s", device_host, exc_info=True,
        )
        return None
