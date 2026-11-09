"""SMTP 传输层：一次连接、认证、发送，以及错误到策划文案的映射。

``smtplib`` 只在本模块导入（测试通过
``patch("features.email.transport.smtplib...")`` 打桩）。所有错误都映射为
``{"sent": False, "mode": "smtp", "error": <面向用户的中文文案>}``，
不向调用方泄露 SMTP 异常原文（服务端日志里才有细节）。
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass, field
from email.message import Message
from typing import Any


logger = logging.getLogger(__name__)


@dataclass
class SmtpSettings:
    """管线校验后的连接参数（163 企业邮对齐规则已在管线阶段应用）。"""

    host: str
    port: int = 465
    from_addr: str = ""
    username: str = ""
    password: str = ""
    use_ssl: bool = True
    use_tls: bool = False
    timeout: int = 10
    extra: dict[str, Any] = field(default_factory=dict)


def smtp_deliver(
    settings: SmtpSettings,
    message: Message,
    recipients: list[str],
) -> dict[str, Any]:
    """Send one assembled message; never raises.

    SMTPAuthenticationError / SMTPServerDisconnected 的文案沿用了历史
    send_email 的中文提示（163 授权码等高频故障），保持对外契约不变。
    """
    try:
        if settings.use_ssl:
            with smtplib.SMTP_SSL(
                settings.host, settings.port, timeout=settings.timeout
            ) as smtp:
                if settings.username and settings.password:
                    smtp.login(settings.username, settings.password)
                smtp.sendmail(settings.from_addr, recipients, message.as_string())
        else:
            with smtplib.SMTP(
                settings.host, settings.port, timeout=settings.timeout
            ) as smtp:
                if settings.use_tls:
                    smtp.starttls()
                if settings.username and settings.password:
                    smtp.login(settings.username, settings.password)
                smtp.sendmail(settings.from_addr, recipients, message.as_string())
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
    except (smtplib.SMTPException, OSError) as exc:
        logger.error("smtp deliver failed: %s", exc, exc_info=True)
        return {
            "sent": False,
            "mode": "smtp",
            "error": "邮件发送失败: SMTP/网络错误（详见服务端日志）",
        }
    return {"sent": True, "mode": "smtp"}
