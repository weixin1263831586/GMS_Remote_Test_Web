"""Agent token / enrollment / approval API endpoints.

Extracted from api.py (2026-09-08 audit) so the auth API stays under the
reviewable-size limit. Mounted onto the /api/auth router from api.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from foundation.responses import error_response

from .access import (
    get_authenticated_user,
    is_elevated,
    require_elevated_admin,
    require_role,
)
from .constants import AGENT_SCOPES
from .service import CurrentUser, auth_service


router = APIRouter()  # mounted onto the /api/auth router in api.py

# ---------------------------------------------------------------------------
# Agent Service Tokens (2026-09-08 audit §二/§四)
# ---------------------------------------------------------------------------

@router.get("/agent-scopes")
async def auth_agent_scopes(
    _admin: CurrentUser = Depends(require_role("admin")),
):
    return {"success": True, "scopes": AGENT_SCOPES}


@router.get("/agent-tokens")
async def auth_agent_tokens(
    _admin: CurrentUser = Depends(require_role("admin")),
):
    return {"success": True, "tokens": auth_service.list_agent_tokens()}


@router.post("/agent-tokens")
async def auth_create_agent_token(
    req: dict,
    admin: CurrentUser = Depends(require_elevated_admin),
):
    """Create one agent service token.

    The raw token is returned exactly once; the database keeps only its
    SHA-256 hash. Deploy it to the build server as a 0600 file and point
    GMS_AUTH_TOKEN_FILE at it.
    """
    try:
        record = auth_service.create_agent_token(
            name=str(req.get("name") or ""),
            owner=admin,
            scopes=req.get("scopes") or [],
            allowed_workers=req.get("allowed_workers"),
            allowed_devices=req.get("allowed_devices"),
            expires_days=req.get("expires_days"),
        )
    except (ValueError, TypeError) as exc:
        return error_response(str(exc), status_code=400)
    return {"success": True, "token": record}


@router.delete("/agent-tokens/{token_id}")
async def auth_revoke_agent_token(
    token_id: str,
    _admin: CurrentUser = Depends(require_elevated_admin),
):
    if not auth_service.revoke_agent_token(token_id):
        return error_response("token 不存在或已吊销", status_code=404)
    return {"success": True, "revoked": token_id}


@router.post("/agent-enrollment-codes")
async def auth_create_agent_enrollment(
    req: dict,
    admin: CurrentUser = Depends(require_elevated_admin),
):
    """Mint a one-shot enrollment code (5-minute TTL).

    The build server runs `gms-rt-agent-enroll <code>` once; the code is
    deleted on use and the returned Service Token is stored 0600 client-side.
    """
    try:
        record = auth_service.create_agent_enrollment(
            name=str(req.get("name") or ""),
            creator=admin,
            scopes=req.get("scopes") or [],
            allowed_workers=req.get("allowed_workers"),
            allowed_devices=req.get("allowed_devices"),
            expires_days=req.get("expires_days"),
        )
    except (ValueError, TypeError) as exc:
        return error_response(str(exc), status_code=400)
    return {"success": True, "enrollment": record}


@router.post("/agent-enroll")
async def auth_agent_enroll(req: dict):
    """Exchange an enrollment code for an Agent Service Token.

    Intentionally does not require a session: the build server only holds the
    one-shot code. Scopes/ACLs/expiry come from the enrollment record.
    """
    try:
        record = auth_service.redeem_agent_enrollment(str(req.get("code") or ""))
    except (ValueError, TypeError):
        record = None
    if record is None:
        return error_response(
            "配对码无效、已使用或已过期", status_code=403
        )
    return {"success": True, "token": record}


# ---------------------------------------------------------------------------
# One-shot Approval Tokens (2026-09-08 audit §五)
# ---------------------------------------------------------------------------

_APPROVAL_TOOLS = {
    # tool name -> (required, description)
    "gms_rt_shell_exec": (None, "one-shot device shell command"),
    "gms_rt_burn_firmware": ("elevated_admin", "firmware burn"),
}


@router.post("/approval-tokens")
async def auth_create_approval_token(request: Request, req: dict):
    """Issue a one-shot approval token for a destructive agent action.

    Must be called by a human session (cookie) — an Agent Service Token can
    never mint approvals for itself. Firmware burn additionally requires a
    live admin elevation on the approving session.
    """
    caller = get_authenticated_user(request)
    if caller is None:
        return error_response("请先登录后再创建审批令牌", status_code=401)
    # Agent principals must never be able to mint approvals for themselves;
    # only a human session (cookie) can approve destructive actions.
    if getattr(request.state, "auth_method", None) == "agent_token":
        return error_response(
            "审批令牌必须由用户本人会话创建，Agent token 不能自批", status_code=401
        )
    tool = str(req.get("tool") or "").strip()
    device = str(req.get("device") or "").strip()
    command = str(req.get("command") or "")
    if tool not in _APPROVAL_TOOLS:
        return error_response(
            f"tool 必须是 {sorted(_APPROVAL_TOOLS)} 之一", status_code=400
        )
    required, _description = _APPROVAL_TOOLS[tool]
    if required == "elevated_admin" and not (
        caller.role == "admin" or is_elevated(request)
    ):
        return error_response(
            "烧录审批需要管理员提权会话", status_code=403
        )
    try:
        record = auth_service.create_approval_token(
            user=caller, tool=tool, device=device, command=command
        )
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    return {"success": True, "approval": record}


@router.post("/approval-tokens/consume")
async def auth_consume_approval_token(request: Request, req: dict):
    """Validate-and-consume one approval token before a destructive action.

    Called by the gms-rt CLI right before executing the approved command.
    The approval token itself is the proof of human approval; the caller may
    hold a human session or an agent service token. Enforces the exact
    tool + device + SHA256(command) binding, single use, and TTL, plus the
    agent token's allowed_devices ACL.
    """
    caller = get_authenticated_user(request)
    if caller is None:
        return error_response("请先认证后再消费审批令牌", status_code=401)
    token = str(req.get("token") or "").strip()
    tool = str(req.get("tool") or "").strip()
    device = str(req.get("device") or "").strip()
    command = str(req.get("command") or "")
    if not token or not tool or not device:
        return error_response("token/tool/device 必填", status_code=400)
    if tool not in _APPROVAL_TOOLS:
        return error_response(f"未知工具: {tool}", status_code=400)
    # Agent token device ACL applies at approval time so a token scoped to
    # specific devices cannot be driven against anything else.
    record = getattr(request.state, "agent_token_record", None)
    if record is not None and not auth_service.agent_acl_allows(
        record, "devices", device
    ):
        return error_response(
            f"Agent token 不允许操作设备 {device}", status_code=403
        )
    if auth_service.consume_approval_token(
        token, tool=tool, device=device, command=command
    ):
        return {"success": True, "consumed": True}
    return error_response(
        "审批令牌无效、已使用、已过期或与命令不匹配", status_code=403
    )
