"""通用邮件发送 API。

提供 ``POST /api/email/send``，供周报、报告分析等模块复用。SMTP 凭证复用
``redmine_dashboard.email`` 配置（与 Redmine 部门提醒共用一套设置）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request

from features.auth.service import require_authenticated_user
from foundation.responses import error_response, success_response
from features.email.service import send_email
from features.redmine.api import get_redmine_config_for_request
from foundation.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/email/send")
async def send_email_endpoint(request: Request):
    """通用发信端点。

    Body 字段：
    - to (必填): str | list[str]  收件人，支持逗号/分号分隔
    - subject (必填): str
    - body (必填): str            正文（纯文本或 HTML）
    - is_html (可选): bool        默认 false
    - cc (可选): str | list[str]
    - attachment_paths (可选): list[str]
    - sender_name (可选): str
    """
    require_authenticated_user(request)
    body = await request.json()
    to = body.get("to")
    subject = str(body.get("subject") or "").strip()
    content = body.get("body")
    if not to:
        return error_response("to is required", status_code=400)
    if not subject:
        return error_response("subject is required", status_code=400)
    if content is None:
        return error_response("body is required", status_code=400)

    kwargs: dict[str, Any] = {
        "is_html": bool(body.get("is_html", False)),
        "manager": get_redmine_config_for_request(request),
    }
    if body.get("cc"):
        kwargs["cc"] = body.get("cc")
    if body.get("attachment_paths"):
        kwargs["attachment_paths"] = body.get("attachment_paths")
        kwargs["allowed_attachment_roots"] = [
            settings.data_root / "reports",
            settings.data_root / "redmine",
        ]
    if body.get("sender_name"):
        kwargs["sender_name"] = str(body.get("sender_name")).strip()

    try:
        result = await asyncio.to_thread(send_email, to, subject, content, **kwargs)
    except Exception as exc:
        logger.exception("send_email failed")
        return error_response(f"邮件发送失败: {exc}", status_code=500)

    if not result.get("sent"):
        return error_response(
            result.get("error", "邮件发送失败"),
            status_code=503,
            mode=result.get("mode"),
        )

    # result 已含 sent/mode/to/cc/recipients，补上 subject 即可
    return success_response(data={"subject": subject, **result}, message="邮件发送成功")
