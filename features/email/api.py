"""通用邮件发送 API。

提供 ``POST /api/email/send``，供周报、报告分析等模块复用。SMTP 凭证复用
``redmine_dashboard.email`` 配置（与 Redmine 部门提醒共用一套设置）。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from features.auth import require_authenticated_user
from features.email.service import send_email
from foundation.responses import error_response, success_response


logger = logging.getLogger(__name__)
router = APIRouter()

# SMTP 凭证存放在 redmine 配置树；组合根在启动时注入按请求解析 manager 的 provider。
_manager_provider = None
# R04/R13：报告附件解析能力由组合根注入（features/reports 提供），email
# 不反向 import reports——否则闭合 email→reports→redmine→email 依赖环。
_attachment_resolver = None


def configure_manager_provider(provider: Callable[[Any], Any] | None) -> None:
    """Register the callable turning a request into an owner config manager.

    Passing ``None`` explicitly clears the provider (the endpoint then
    sends with the default manager).
    """
    global _manager_provider
    _manager_provider = provider


def configure_attachment_resolver(
    provider: Callable[[Any, list], Any] | None,
) -> None:
    """Register the report-attachment resolver (owner-checked ZIP bundles).

    Implemented by features.reports.email_attachments and wired by the
    composition root. Passing ``None`` disables report attachments.
    """
    global _attachment_resolver
    _attachment_resolver = provider


@router.post("/api/email/send")
async def send_email_endpoint(request: Request):
    """发送支持抄送、HTML 和附件的邮件。"""
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

    # R04: 客户端路径型附件（attachment_paths）已下线——路径不能证明所有权。
    if body.get("attachment_paths"):
        return error_response(
            "attachment_paths is no longer accepted; pass attachment_report_ids",
            status_code=400,
        )

    kwargs: dict[str, Any] = {
        "is_html": bool(body.get("is_html", False)),
        "manager": _manager_provider(request) if _manager_provider is not None else None,
    }
    if body.get("cc"):
        kwargs["cc"] = body.get("cc")
    report_ids = body.get("attachment_report_ids") or []
    if report_ids:
        if _attachment_resolver is None:
            return error_response(
                "报告附件功能未配置", status_code=503
            )
        try:
            attachments, denied = await asyncio.to_thread(
                _attachment_resolver, request, report_ids
            )
        except Exception as exc:
            logger.exception("resolving report attachments failed")
            return error_response(f"附件解析失败: {exc}", status_code=500)
        if denied:
            return error_response(
                "附件报告不存在或无权访问: " + ", ".join(sorted(set(denied))),
                status_code=403,
            )
        kwargs["attachment_paths"] = [str(path) for path in attachments]
        kwargs["allowed_attachment_roots"] = [  # 临时打包目录，唯一可信来源
            attachments[0].parent
        ] if attachments else []
    if body.get("sender_name"):
        kwargs["sender_name"] = str(body.get("sender_name")).strip()

    try:
        result = await asyncio.to_thread(send_email, to, subject, content, **kwargs)
    except Exception as exc:
        logger.exception("send_email failed")
        return error_response(f"邮件发送失败: {exc}", status_code=500)
    finally:
        for path in kwargs.get("attachment_paths") or []:
            # 临时 ZIP 仅服务本次发送，读完即清理。
            shutil.rmtree(Path(path).parent, ignore_errors=True)

    if not result.get("sent"):
        return error_response(
            result.get("error", "邮件发送失败"),
            status_code=503,
            mode=result.get("mode"),
        )

    # result 已含 sent/mode/to/cc/recipients，补上 subject 即可
    return success_response(data={"subject": subject, **result}, message="邮件发送成功")
