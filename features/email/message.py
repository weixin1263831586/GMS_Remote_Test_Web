"""MIME 报文构建：把已通过管线校验的字段组装成可发送的邮件。

只做「组装」，不做「决策」：收件人/主题合法性在 service 管线校验，
SMTP 连接策略在 transport。附件遵循白名单（``allowed_attachment_roots``
下的普通文件才可发送），单个附件失败降级为 missing/blocked 清单，
不阻断整体发送（历史行为，测试有覆盖）。
"""

from __future__ import annotations

import os
from email.encoders import encode_base64
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

from features.email.parsing import safe_header_text


class EmailEnvelope(dict):
    """结构化信封：管线在台账与返回值中直接展开。"""


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
) -> tuple[MIMEMultipart, EmailEnvelope]:
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
        except Exception as exc:  # 单个附件失败不阻断整体发送
            missing_attachments.append(f"{resolved} ({exc})")

    envelope = EmailEnvelope(
        from_addr=from_addr,
        to=list(to_list),
        cc=list(cc_list),
        recipients=list({*to_list, *cc_list}),
        attachments_missing=missing_attachments,
        attachments_blocked=blocked_attachments,
    )
    return message, envelope
