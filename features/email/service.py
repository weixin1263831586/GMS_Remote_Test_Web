"""通用邮件发送服务。

综合参考：
- ``features/redmine/api.py:_send_reminder_email`` —— SMTP 配置读取、163 企业邮兼容
  （授权码 ≠ 登录密码、发件人必须 = 登录账号、SSL 端口自动判定）与错误兜底。
- ``rk-skills/06-workflow-tools/rk-email/scripts/send_email.py`` —— 多收件人/抄送、
  HTML 正文、附件、发件人昵称。

凭证复用 ``config_runtime.json → redmine_dashboard.email``，与 Redmine 部门提醒
共用同一套 SMTP 设置；阻塞调用应交由调用方通过 ``asyncio.to_thread`` 包裹。
"""

from __future__ import annotations

import os
import smtplib
from email.encoders import encode_base64
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any


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


def _load_email_config(manager) -> dict[str, Any]:
    """从 redmine_dashboard.email 读取 SMTP 配置。"""
    dashboard_cfg = manager.load_config().get("redmine_dashboard") or {}
    return dashboard_cfg.get("email") or {}


def _resolve_allowed_attachment(path: str, allowed_roots: list[str | os.PathLike[str]]) -> Path | None:
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


def send_email(
    to: str | list[str],
    subject: str,
    body: str,
    *,
    is_html: bool = False,
    cc: str | list[str] | None = None,
    attachment_paths: list[str] | None = None,
    allowed_attachment_roots: list[str | os.PathLike[str]] | None = None,
    sender_name: str | None = None,
    manager=None,
) -> dict[str, Any]:
    """发送邮件，返回 ``{"sent": bool, "mode": str, "error": str | None}``。

    - ``to`` / ``cc``：字符串（逗号/分号分隔）或列表。
    - ``is_html``：True 时 body 作为 HTML 正文。
    - ``attachment_paths``：附件路径列表；默认拒绝读取。只有落在
      ``allowed_attachment_roots`` 下的普通文件才会作为附件发送。
    - ``sender_name``：发件人昵称；缺省用配置里的 username 或 from_addr。
    - ``manager``：redmine owner-aware config manager（必传）。
    """
    email_cfg = _load_email_config(manager)
    smtp_host = str(email_cfg.get("smtp_host") or "").strip()
    default_from = str(email_cfg.get("default_from_addr") or "").strip()
    from_addr = str(email_cfg.get("from_addr") or email_cfg.get("username") or default_from).strip()
    if not smtp_host:
        return {
            "sent": False,
            "mode": "unconfigured",
            "error": "SMTP 未配置，请在 Redmine 看板「设置 → SMTP」中填写 smtp_host",
        }

    smtp_port = int(email_cfg.get("smtp_port") or 465)
    username = str(email_cfg.get("username") or "").strip()
    password = str(email_cfg.get("password") or "").strip()
    is_qiye_163 = smtp_host.lower().endswith("qiye.163.com")
    # 163 企业邮要求发件人与登录账号一致；缺省（default_from）或与账号不符时，强制对齐
    if is_qiye_163 and username and (not from_addr or from_addr == default_from or from_addr != username):
        from_addr = username
    use_ssl = bool(email_cfg.get("use_ssl", smtp_port == 465))
    use_tls = bool(email_cfg.get("use_tls", not use_ssl and smtp_port != 465))
    timeout = int(email_cfg.get("timeout") or 10)

    # 注意：SMTP 授权码 与 Redmine 网页登录/API 密码是两回事，不能互相兜底。
    # 163 企业邮用错误凭据会被服务器直接断开连接（而非返回认证失败码），
    # 因此这里必须用专门的 SMTP 授权码；为空时直接返回明确错误，引导用户填写。
    if is_qiye_163 and (not username or not password):
        return {
            "sent": False,
            "mode": "unconfigured",
            "error": "163 企业邮箱 SMTP 需要用户名和授权码（注意：是邮箱 SMTP 授权码，不是 Redmine 登录密码），请在 Redmine 看板「设置 → SMTP」中填写",
        }

    final_to_list = split_emails(to)
    final_cc_list = split_emails(cc)
    if not final_to_list:
        return {"sent": False, "mode": "unconfigured", "error": "收件人列表为空"}

    message = MIMEMultipart()
    display_name = sender_name or email_cfg.get("sender_name")
    if display_name:
        message["From"] = f"{display_name} <{from_addr}>"
    else:
        message["From"] = from_addr
    message["To"] = ", ".join(final_to_list)
    if final_cc_list:
        message["Cc"] = ", ".join(final_cc_list)
    message["Subject"] = subject

    # 邮件正文
    message.attach(MIMEText(body, "html" if is_html else "plain", _charset="utf-8"))

    # 处理附件（参考 rk-email send_email.py）
    missing_attachments: list[str] = []
    blocked_attachments: list[str] = []
    allowed_roots = list(allowed_attachment_roots or [])
    for path in attachment_paths or []:
        abs_path = os.path.abspath(str(path))
        resolved = _resolve_allowed_attachment(str(path), allowed_roots)
        if resolved is None:
            if os.path.exists(abs_path):
                blocked_attachments.append(abs_path)
            else:
                missing_attachments.append(abs_path)
            continue
        try:
            filename = resolved.name
            with resolved.open("rb") as fh:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(fh.read())
            encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
            message.attach(part)
        except Exception as exc:  # 单个附件失败不阻断整体发送
            missing_attachments.append(f"{resolved} ({exc})")

    # 真正的 SMTP 接收者列表 = to + cc 去重
    smtp_recipients = list({*final_to_list, *final_cc_list})

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=timeout) as smtp:
                if username and password:
                    smtp.login(username, password)
                smtp.sendmail(from_addr, smtp_recipients, message.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout) as smtp:
                if use_tls:
                    smtp.starttls()
                if username and password:
                    smtp.login(username, password)
                smtp.sendmail(from_addr, smtp_recipients, message.as_string())
    except smtplib.SMTPAuthenticationError as exc:
        return {
            "sent": False,
            "mode": "smtp",
            "error": f"SMTP认证失败，请在设置中填写企业邮箱SMTP授权码/密码，发件人需与账号一致: {exc}",
        }
    except smtplib.SMTPServerDisconnected as exc:
        return {
            "sent": False,
            "mode": "smtp",
            "error": f"SMTP连接被服务器关闭，请检查企业邮箱SMTP授权码/密码、账号是否开启SMTP服务，发件人需与账号一致: {exc}",
        }

    result: dict[str, Any] = {
        "sent": True,
        "mode": "smtp",
        "to": final_to_list,
        "cc": final_cc_list,
        "recipients": smtp_recipients,
    }
    if missing_attachments:
        result["attachments_missing"] = missing_attachments
    if blocked_attachments:
        result["attachments_blocked"] = blocked_attachments
    return result
