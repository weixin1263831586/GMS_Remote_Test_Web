"""通用 SMTP 发送管线：校验 → 组装 → 传输 → 台账，三条链路统一收口。

分层（本包内各文件，均为仓库依赖方向的叶子）：

- ``parsing``    纯函数：地址拆分/校验、MIME 头折叠清洗；
- ``config``     ``resolve_email_config`` 合并规则与 SMTP 密码读取；
- ``message``    MIME 报文与信封组装（附件白名单）；
- ``transport``  唯一 ``smtplib`` 使用点，错误映射为面向用户的文案；
- ``store``      发件台账（email_outbox.sqlite3，每封邮件一行，可追溯）；
- ``service``    本文件：发送管线与对外契约。

``send_email`` 是三条链路（手动/API、自动化流水线通知、运维告警）
的唯一出口：每次调用——包括被全局开关/未配置/SMTP 错误拒绝的尝试——
都在台账留一行记录（channel/owner/ref 回链触发业务），正文只存
500 字符预览。台账写入失败只降级为不记录，绝不影响发送结果。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from features.email.config import (
    email_password,
    load_email_config,
    resolve_email_config,
)
from features.email.message import build_mime_message
from features.email.parsing import normalize_email_addresses, safe_header_text, split_emails
from features.email.store import get_email_outbox_store
from features.email.transport import SmtpSettings, smtp_deliver


logger = logging.getLogger(__name__)

# 对外兼容导出：老调用方（redmine 透传、旧测试 patch 目标）继续可用。
__all__ = [
    "normalize_email_addresses",
    "resolve_email_config",
    "send_email",
    "split_emails",
]


def _email_password(email_cfg: dict[str, Any]) -> str:
    """兼容别名：历史测试与调用方通过 service 引用密码读取。"""
    return email_password(email_cfg)


def _safe_header_text(value: Any) -> str:
    """兼容别名：MIME 头折叠清洗。"""
    return safe_header_text(value)


def _resolve_allowed_attachment(path: str, allowed_roots):
    """兼容别名：附件白名单解析（历史上从 service 导出过）。"""
    from features.email.message import resolve_allowed_attachment

    return resolve_allowed_attachment(path, allowed_roots)


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
    channel: str = "manual",
    owner_id: str = "",
    username: str = "",
    ref_type: str = "",
    ref_id: str = "",
) -> dict[str, Any]:
    """发送一封邮件并写入发件台账。

    返回 ``{"sent": bool, "mode": str, "error": str | None, ...}``：

    - ``to`` / ``cc``：字符串（逗号/分号分隔）或列表。
    - ``is_html``：True 时 body 作为 HTML 正文。
    - ``attachment_paths``：附件路径列表；默认拒绝读取。只有落在
      ``allowed_attachment_roots`` 下的普通文件才会作为附件发送。
    - ``sender_name``：发件人昵称；缺省用配置里的 username 或 from_addr。
    - ``manager``：可选的 owner config manager；省略时使用全局 SMTP 默认
      配置（``resolve_email_config`` 合并规则），各 owner 无需重复配置账号。
    - ``channel``：触发通道标识（``manual`` / ``automation`` / ``ops`` /
      ``redmine_reminder``），台账按通道过滤展示；
    - ``owner_id`` / ``username``：资源所有者（ADR 0010
      ``resource_owner_id``）与操作者显示名，owner 视图按 owner_id 过滤；
    - ``ref_type`` / ``ref_id``：业务回链（如 ``automation_run`` + run id、
      ``ops_event`` + 事件 id），从台账可跳回触发业务。

    失败也记录：mode 为 ``disabled``（全局关闭）、``unconfigured``（缺
    SMTP 配置）、``invalid``（收件人/主题非法）、``smtp``（发送错误）。
    成功时附带 ``to/cc/recipients/from_addr`` 与附件缺失/拦截清单。
    """
    result = _send_pipeline(
        to,
        subject,
        body,
        is_html=is_html,
        cc=cc,
        attachment_paths=attachment_paths,
        allowed_attachment_roots=allowed_attachment_roots,
        sender_name=sender_name,
        manager=manager,
    )
    _record_outbox(
        result,
        to=to,
        cc=cc,
        subject=subject,
        body=body,
        is_html=is_html,
        attachment_paths=attachment_paths,
        channel=channel,
        owner_id=owner_id,
        username=username,
        ref_type=ref_type,
        ref_id=ref_id,
    )
    return result


def _send_pipeline(
    to: str | list[str],
    subject: str,
    body: str,
    *,
    is_html: bool,
    cc: str | list[str] | None,
    attachment_paths: list[str] | None,
    allowed_attachment_roots: list[str | os.PathLike[str]] | None,
    sender_name: str | None,
    manager,
) -> dict[str, Any]:
    """执行一次发送，返回结果字典（不发台账）。"""
    # —— 阶段 1：配置解析与全局开关 ——
    email_cfg = load_email_config(manager)
    # 全局总开关：`email.enabled=false`（或 GMS_EMAIL_ENABLED=0）关闭全部
    # 出站邮件——Redmine 提醒、ATS 运行通知、运维告警、通用发送 API 都
    # 走这个单一收口；默认开启（缺配置 ≠ 关闭）。
    env_enabled = os.getenv("GMS_EMAIL_ENABLED")
    if env_enabled is not None:
        enabled = env_enabled.strip().lower() not in ("0", "false", "no", "off")
    else:
        enabled = email_cfg.get("enabled", True) is not False
    if not enabled:
        return {
            "sent": False,
            "mode": "disabled",
            "error": "邮件功能已全局关闭（email.enabled=false 或 GMS_EMAIL_ENABLED=0）",
        }

    smtp_host = str(email_cfg.get("smtp_host") or "").strip()
    default_from = str(email_cfg.get("default_from_addr") or "").strip()
    from_addr = str(email_cfg.get("from_addr") or email_cfg.get("username") or default_from).strip()
    if not smtp_host:
        return {
            "sent": False,
            "mode": "unconfigured",
            "error": "SMTP 未配置：请在 Redmine 看板「设置 → SMTP」保存，或由管理员通过 POST /api/email/smtp 写入全局配置",
        }

    # —— 阶段 2：输入校验（收件人/主题/凭据） ——
    username = str(email_cfg.get("username") or "").strip()
    password = email_password(email_cfg)
    is_qiye_163 = smtp_host.lower().endswith("qiye.163.com")
    # 163 企业邮要求发件人与登录账号一致；缺省（default_from）或与账号不符时，强制对齐
    if is_qiye_163 and username and (not from_addr or from_addr == default_from or from_addr != username):
        from_addr = username
    use_ssl = bool(email_cfg.get("use_ssl", int(email_cfg.get("smtp_port") or 465) == 465))
    use_tls = bool(email_cfg.get("use_tls", not use_ssl and int(email_cfg.get("smtp_port") or 465) != 465))
    timeout = int(email_cfg.get("timeout") or 10)

    # 163 企业邮必须使用独立 SMTP 授权码。
    if is_qiye_163 and (not username or not password):
        return {
            "sent": False,
            "mode": "unconfigured",
            "error": "163 企业邮箱 SMTP 需要用户名和授权码（注意：是邮箱 SMTP 授权码，不是 Redmine 登录密码），请在 Redmine 看板「设置 → SMTP」中填写",
        }

    final_to_list = normalize_email_addresses(to)
    final_cc_list = normalize_email_addresses(cc)
    if final_to_list is None or final_cc_list is None:
        return {"sent": False, "mode": "invalid", "error": "收件人邮箱地址无效"}
    if not final_to_list:
        return {"sent": False, "mode": "unconfigured", "error": "收件人列表为空"}
    safe_from = normalize_email_addresses(from_addr)
    if not safe_from or len(safe_from) != 1:
        return {"sent": False, "mode": "unconfigured", "error": "发件人邮箱地址无效"}
    from_addr = safe_from[0]
    subject = safe_header_text(subject)
    if not subject:
        return {"sent": False, "mode": "invalid", "error": "邮件主题为空"}

    # —— 阶段 3：MIME 组装（含附件白名单） ——
    message, envelope = build_mime_message(
        from_addr=from_addr,
        display_name=safe_header_text(sender_name or email_cfg.get("sender_name")) or None,
        to_list=final_to_list,
        cc_list=final_cc_list,
        subject=subject,
        body=body,
        is_html=is_html,
        attachment_paths=attachment_paths,
        allowed_attachment_roots=allowed_attachment_roots,
    )

    # —— 阶段 4：SMTP 传输 ——
    settings = SmtpSettings(
        host=smtp_host,
        port=int(email_cfg.get("smtp_port") or 465),
        from_addr=from_addr,
        username=username,
        password=password,
        use_ssl=use_ssl,
        use_tls=use_tls,
        timeout=timeout,
    )
    delivered = smtp_deliver(settings, message, envelope["recipients"])

    result: dict[str, Any] = dict(delivered)
    result.setdefault("error", None)
    result["to"] = envelope["to"]
    result["cc"] = envelope["cc"]
    result["recipients"] = envelope["recipients"]
    result["from_addr"] = envelope["from_addr"]
    if envelope["attachments_missing"]:
        result["attachments_missing"] = envelope["attachments_missing"]
    if envelope["attachments_blocked"]:
        result["attachments_blocked"] = envelope["attachments_blocked"]
    return result


def _record_outbox(
    result: dict[str, Any],
    *,
    to: str | list[str],
    cc: str | list[str] | None,
    subject: str,
    body: str,
    is_html: bool,
    attachment_paths: list[str] | None,
    channel: str,
    owner_id: str,
    username: str,
    ref_type: str,
    ref_id: str,
) -> None:
    """把本次发送尝试写入台账；审计增强，绝不影响发送结果。"""
    try:
        get_email_outbox_store().record(
            {
                "channel": channel,
                "owner_id": owner_id,
                "username": username,
                "ref_type": ref_type,
                "ref_id": ref_id,
                "from_addr": str(result.get("from_addr") or ""),
                "to_addrs": normalize_email_addresses(to) or [],
                "cc_addrs": normalize_email_addresses(cc) or [],
                "subject": subject,
                "body_preview": str(body or ""),
                "is_html": bool(is_html),
                "attachments": [
                    os.path.basename(str(path)) for path in attachment_paths or []
                ],
                "sent": bool(result.get("sent")),
                "mode": str(result.get("mode") or ""),
                "error": str(result.get("error") or ""),
            }
        )
    except Exception:
        # 台账是审计增强，不是发送路径的一部分：任何异常都不能改变
        # 发送结果或让调用方看到额外错误。
        logger.warning("email outbox ledger write failed", exc_info=True)
