"""通用邮件发送 API。

提供 ``POST /api/email/send``，供周报、报告分析等模块复用。SMTP 凭证复用
``redmine_dashboard.email`` 配置（与 Redmine 部门提醒共用一套设置）。

权限与滥用防护：
- 细分权限 ``email.send``（登录 ≠ 允许发邮件）；
- 收件人数量上限（to + cc 合计）、主题/正文大小上限；
- 每用户滑动窗口限流（内存实现，多 Worker 共享由 DB limiter 之前先用
  进程内限流兜底——邮件不是凭据爆破面，进程内 + per-user 已足够）；
- 发送结果写入审计日志（success/failure + 收件人数）。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import APIRouter, Request

from features.auth import require_authenticated_user
from features.email.service import normalize_email_addresses, send_email
from foundation.responses import error_response, success_response


logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Abuse limits: "登录过"与"允许发送邮件"不是同一个权限。
# ---------------------------------------------------------------------------

MAX_RECIPIENTS = 30            # to + cc 合计
MAX_SUBJECT_CHARS = 500
MAX_BODY_CHARS = 2_000_000     # ~2MB 正文(HTML 报告)
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_SENDS = 10      # 每用户每窗口

_rate_events: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = Lock()


def _rate_limited(username: str) -> bool:
    """滑动窗口 per-user 限流;返回 True 表示超过限额。"""
    now = time.monotonic()
    with _rate_lock:
        events = _rate_events[username]
        while events and events[0] <= now - RATE_LIMIT_WINDOW_SECONDS:
            events.popleft()
        if len(events) >= RATE_LIMIT_MAX_SENDS:
            return True
        events.append(now)
        return False


def _normalize_recipients(value: Any) -> list[str] | None:
    """to/cc 归一化为合法地址列表;非法类型/地址返回 None(调用方报 400)。

    与 service.split_emails 的分隔语义保持一致(逗号、分号均拆分),
    否则 ``["a@x.com;b@x.com;..."]`` 一项即可绕过收件人数量上限。
    每项必须是单个不含空白/CRLF/分隔符的地址:SMTP 头(To/Cc)由地址
    列表 join 生成,内嵌 CRLF 等于向邮件基础设施注入任意头
    (header injection),必须在 API 边界拒绝。
    """
    return normalize_email_addresses(value)


# SMTP 凭证存放在 redmine 配置树；组合根在启动时注入按请求解析 manager 的 provider。
_manager_provider = None
# 报告附件解析能力由组合根注入（features/reports 提供），email
# 不反向 import reports——否则闭合 email→reports→redmine→email 依赖环。
# See docs/architecture/adr/0002-feature-foundation-boundary.md.
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
    user = require_authenticated_user(request)
    # 细分权限:user/device_operator 角色默认授予 email.send(见
    # features/auth/constants.py);登录但没有该权限的账号被拒绝。
    if not user.has_permission("email.send"):
        logger.warning(
            "[EMAIL_SEND] denied: user %s lacks email.send", user.username
        )
        return error_response("没有发送邮件的权限 (email.send)", status_code=403)
    if _rate_limited(user.username):
        logger.warning(
            "[EMAIL_SEND] rate limited: user %s exceeds %d/%ds",
            user.username, RATE_LIMIT_MAX_SENDS, RATE_LIMIT_WINDOW_SECONDS,
        )
        return error_response(
            f"发送过于频繁,请稍后再试(每 {RATE_LIMIT_WINDOW_SECONDS} 秒最多 "
            f"{RATE_LIMIT_MAX_SENDS} 封)", status_code=429,
        )

    body = await request.json()
    to = _normalize_recipients(body.get("to"))
    cc = _normalize_recipients(body.get("cc"))
    if to is None or cc is None:
        return error_response("to/cc 必须是邮箱地址列表", status_code=400)
    subject = " ".join(str(body.get("subject") or "").split())
    # subject 进入 Subject 头;剥离 \r/\n 等空白折叠字符防止头注入。
    content = body.get("body")
    if not to:
        return error_response("to is required", status_code=400)
    if not subject:
        return error_response("subject is required", status_code=400)
    if content is None:
        return error_response("body is required", status_code=400)
    if len(to) + len(cc) > MAX_RECIPIENTS:
        return error_response(
            f"收件人数量超限(to+cc 最多 {MAX_RECIPIENTS} 个)", status_code=400
        )
    if len(subject) > MAX_SUBJECT_CHARS:
        return error_response(
            f"主题过长(最多 {MAX_SUBJECT_CHARS} 字符)", status_code=400
        )
    if len(str(content)) > MAX_BODY_CHARS:
        return error_response(
            f"正文过大(最多 {MAX_BODY_CHARS} 字符)", status_code=413
        )

    # 客户端路径型附件（attachment_paths）已下线——路径不能证明所有权。
    if body.get("attachment_paths"):
        return error_response(
            "attachment_paths is no longer accepted; pass attachment_report_ids",
            status_code=400,
        )

    kwargs: dict[str, Any] = {
        "is_html": bool(body.get("is_html", False)),
        "manager": _manager_provider(request) if _manager_provider is not None else None,
    }
    if cc:
        kwargs["cc"] = cc
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
        # sender_name 拼入 From 头的显示名,同样剥离 CRLF。
        kwargs["sender_name"] = " ".join(str(body.get("sender_name")).split())

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
        logger.warning(
            "[EMAIL_SEND] failed: user=%s recipients=%d subject=%r error=%s",
            user.username, len(to) + len(cc), subject[:80],
            result.get("error", ""),
        )
        return error_response(
            result.get("error", "邮件发送失败"),
            status_code=503,
            mode=result.get("mode"),
        )

    # result 已含 sent/mode/to/cc/recipients，补上 subject 即可
    logger.info(
        "[EMAIL_SEND] sent: user=%s recipients=%d subject=%r mode=%s",
        user.username, len(to) + len(cc), subject[:80], result.get("mode", ""),
    )
    return success_response(data={"subject": subject, **result}, message="邮件发送成功")
