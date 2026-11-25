"""邮件地址与报文：纯函数校验、MIME 报文和信封组装。

地址校验与报文构建共用头字段处理；发送决策仍在 service 管线，
SMTP 连接策略在 transport。附件遵循白名单（``allowed_attachment_roots``
下的普通文件才可发送），单个附件失败降级为 missing/blocked 清单，
不阻断整体发送（历史行为，测试有覆盖）。
"""

from __future__ import annotations

import logging
import os
import re
from email.encoders import encode_base64
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


_SAFE_ADDRESS_RE = re.compile(r'[^\s,;<>"@]+@[^\s,;<>"@]+')


def split_emails(email_input: str | list[str] | None) -> list[str]:
    """将输入（字符串或列表）转换为干净的邮件地址列表。

    支持逗号、分号分隔；递归处理列表内的混合分隔符。来自 rk-email send_email.py。
    """
    if not email_input:
        return []

    if isinstance(email_input, list):
        # 递归处理列表中的每一项，防止项内含有逗号
        result: list[str] = []
        for item in email_input:
            result.extend(split_emails(item))
        return result

    raw_list = email_input.replace(';', ',').split(',')
    return [e.strip() for e in raw_list if e.strip()]


def normalize_email_addresses(
    email_input: str | list[str] | None,
) -> list[str] | None:
    """Split and validate envelope/header addresses; ``None`` means invalid."""
    if email_input is None:
        return []
    if not isinstance(email_input, (str, list)):
        return None
    if isinstance(email_input, list) and not all(
        isinstance(item, str) for item in email_input
    ):
        return None
    addresses = split_emails(email_input)
    if any(not _SAFE_ADDRESS_RE.fullmatch(address) for address in addresses):
        return None
    return addresses


def safe_header_text(value: Any) -> str:
    """Collapse CR/LF and other folding whitespace before MIME headers."""
    return ' '.join(str(value or '').split())


def resolve_allowed_attachment(
    path: str, allowed_roots: list[str | os.PathLike[str]]
) -> Path | None:
    """Return a real attachment path only if it lives under an allowed root."""
    if not path:
        return None
    try:
        candidate = Path(path).expanduser().resolve(strict=True)
    except OSError:
        return None
    if not candidate.is_file():
        return None
    for root in allowed_roots:
        try:
            root_path = Path(root).expanduser().resolve(strict=True)
        except OSError:
            continue
        if candidate == root_path or root_path in candidate.parents:
            return candidate
    return None


def build_mime_message(
    *,
    from_addr: str,
    display_name: str | None,
    to_list: list[str],
    cc_list: list[str],
    subject: str,
    body: str,
    is_html: bool = False,
    attachment_paths: list[str] | None = None,
    allowed_attachment_roots: list[str | os.PathLike[str]] | None = None,
) -> tuple[MIMEMultipart, dict]:
    """Assemble the MIME message and return ``(message, envelope)``.

    envelope 携带 ``from_addr / to / cc / recipients / attachments_missing /
    attachments_blocked``：recipients 是 SMTP envelope 收件人（to+cc 去重），
    attachment 清单供管线记入台账。
    """
    message = MIMEMultipart()
    if display_name:
        message["From"] = formataddr((display_name, from_addr))
    else:
        message["From"] = from_addr
    message["To"] = ", ".join(to_list)
    if cc_list:
        message["Cc"] = ", ".join(cc_list)
    message["Subject"] = subject

    message.attach(MIMEText(body, "html" if is_html else "plain", _charset="utf-8"))

    missing_attachments: list[str] = []
    blocked_attachments: list[str] = []
    allowed_roots = list(allowed_attachment_roots or [])
    for path in attachment_paths or []:
        abs_path = os.path.abspath(str(path))
        resolved = resolve_allowed_attachment(str(path), allowed_roots)
        if resolved is None:
            if os.path.exists(abs_path):
                blocked_attachments.append(abs_path)
            else:
                missing_attachments.append(abs_path)
            continue
        try:
            filename = safe_header_text(resolved.name)[:255]
            with resolved.open("rb") as fh:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(fh.read())
            encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
            message.attach(part)
        except OSError as exc:  # 单个附件失败不阻断整体发送
            logger.warning("email attachment unreadable: %s (%s)", resolved.name, type(exc).__name__)
            missing_attachments.append(str(resolved))

    envelope = dict(
        from_addr=from_addr,
        to=list(to_list),
        cc=list(cc_list),
        recipients=list({*to_list, *cc_list}),
        attachments_missing=missing_attachments,
        attachments_blocked=blocked_attachments,
    )
    return message, envelope
