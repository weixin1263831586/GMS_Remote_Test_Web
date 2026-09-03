"""Authorization helpers for user-owned USB/IP client hosts."""

from fastapi import HTTPException

from features.auth import get_authenticated_user, is_elevated


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
    if user is None:
        return
    # 已通过管理员验证（提权）的会话视同管理员：烧写等高危操作走的是
    # require_elevated_admin，若此处仍按 role 拦截会出现"能烧写却不能
    # 连接 USB/IP 主机"的策略矛盾。
    if is_elevated(request):
        return
    if (
        user.role != "admin"
        and explicit_host
        and explicit_host != request_host
    ):
        raise HTTPException(403, "USB/IP host belongs to another client")
