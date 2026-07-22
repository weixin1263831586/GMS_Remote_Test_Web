"""Authorization helpers for user-owned USB/IP client hosts."""

from fastapi import HTTPException

from features.auth import get_authenticated_user


def usbip_request_user(request):
    try:
        return get_authenticated_user(request)
    except AttributeError:
        return None


def enforce_usbip_host_access(
    request,
    explicit_host: str | None,
    request_host: str,
) -> None:
    user = usbip_request_user(request)
    if (
        user
        and user.role != "admin"
        and explicit_host
        and explicit_host != request_host
    ):
        raise HTTPException(403, "USB/IP host belongs to another client")
