"""Ops alert notification transports: email and webhook (IM robots).

Never raises — alerting sits inside maintenance/watchdog paths and must not
break them (same contract as ``features.automation.notifier``). Webhook
payloads are HMAC-SHA256 signed with a token from the ops-alerts config so a
receiver can authenticate the sender (secret stored under ``configs/secrets/``
per ADR 0007; ``configs/local/config.json`` only references it).

Formats: ``generic`` (JSON body), ``wecom`` / ``dingtalk`` / ``feishu``
(IM robot markdown envelopes).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from foundation.error_model import record_internal_error


logger = logging.getLogger(__name__)

WEBHOOK_FORMATS = ("generic", "wecom", "dingtalk", "feishu")
WEBHOOK_TIMEOUT_SECONDS = 10.0


def load_ops_alerts_config() -> dict[str, Any]:
    """Read the ``ops_alerts`` block with environment overrides.

    数值字段转换放在各自 try 块内：单个坏值只回退默认值，不让一处
    配置错误拖垮整个告警通道。
    """
    import os

    from foundation.config import config_manager

    raw = config_manager.load_config().get("ops_alerts", {})
    cfg = raw if isinstance(raw, dict) else {}

    def _env_int(env_key: str, cfg_key: str, default: int, minimum: int) -> int:
        try:
            return max(minimum, int(os.getenv(env_key, str(cfg.get(cfg_key, default)))))
        except (TypeError, ValueError):
            logger.warning(
                "ops_alerts.%s is not an integer (%r); using default %s",
                cfg_key, cfg.get(cfg_key), default,
            )
            return default

    def _env_float(env_key: str, cfg_key: str, default: float) -> float:
        try:
            return max(0.0, float(os.getenv(env_key, str(cfg.get(cfg_key, default)))))
        except (TypeError, ValueError):
            logger.warning(
                "ops_alerts.%s is not a number (%r); using default %s",
                cfg_key, cfg.get(cfg_key), default,
            )
            return default

    return {
        "enabled": str(
            os.getenv("GMS_OPS_ALERTS_ENABLED", str(cfg.get("enabled", False)))
        ).lower() not in ("0", "false", "no", "off"),
        "webhook_url": os.getenv("GMS_OPS_ALERTS_WEBHOOK_URL", str(cfg.get("webhook_url", ""))),
        "webhook_format": os.getenv(
            "GMS_OPS_ALERTS_WEBHOOK_FORMAT", str(cfg.get("webhook_format", "generic"))
        ),
        "webhook_token_file": os.getenv(
            "GMS_OPS_ALERTS_TOKEN_FILE", str(cfg.get("webhook_token_file", ""))
        ),
        "email_to": os.getenv("GMS_OPS_ALERTS_EMAIL_TO", str(cfg.get("email_to", ""))),
        "suppress_minutes": _env_int(
            "GMS_OPS_ALERTS_SUPPRESS_MINUTES", "suppress_minutes", 60, 0
        ),
        "consecutive_failure_threshold": _env_int(
            "GMS_OPS_ALERTS_CONSECUTIVE_FAILURES",
            "consecutive_failure_threshold", 3, 1,
        ),
        "min_free_gb": _env_float(
            "GMS_OPS_ALERTS_MIN_FREE_GB", "min_free_gb", 20
        ),
    }


def _incident_text(incident: dict[str, Any]) -> str:
    severity = str(incident.get("severity") or "info").upper()
    kind = str(incident.get("kind") or "incident")
    subject = str(incident.get("subject") or "-")
    message = str(incident.get("message") or "")
    return f"[{severity}] {kind} · {subject}: {message}"


def _wecom_payload(text: str) -> dict[str, Any]:
    return {"msgtype": "markdown", "markdown": {"content": text}}


def _dingtalk_payload(text: str) -> dict[str, Any]:
    return {"msgtype": "markdown", "markdown": {"title": "GMS ATS 运维告警", "text": text}}


def _feishu_payload(text: str) -> dict[str, Any]:
    return {"msg_type": "text", "content": {"text": text}}


def _webhook_payload(incident: dict[str, Any], fmt: str) -> dict[str, Any]:
    text = _incident_text(incident)
    if fmt == "wecom":
        return _wecom_payload(text)
    if fmt == "dingtalk":
        return _dingtalk_payload(text)
    if fmt == "feishu":
        return _feishu_payload(text)
    return {"source": "gms-ats-ops", "incident": incident}


def _load_webhook_token(token_file: str) -> bytes:
    if not token_file:
        return b""
    try:
        from pathlib import Path

        return Path(token_file).read_bytes()
    except OSError:
        return b""


def _validate_webhook_url(url: str) -> str | None:
    """Basic outbound guard: only https (or http for local test rigs).

    返回 None 表示合法；否则返回拒绝原因。仓库其余出站访问同样只允许
    https/受限 http；告警 webhook 不做内网豁免，避免配置把告警引流到
    未预期的端点。
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return "unparsable webhook_url"
    if parsed.scheme not in ("https", "http"):
        return f"unsupported scheme: {parsed.scheme!r}"
    if not parsed.hostname:
        return "webhook_url has no host"
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
        return "plain http is only allowed for localhost"
    return None


def send_webhook(incident: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    url = str(cfg.get("webhook_url") or "")
    if not url:
        return {"transport": "webhook", "sent": False, "reason": "no webhook_url"}
    rejected = _validate_webhook_url(url)
    if rejected:
        logger.warning("ops webhook_url rejected: %s", rejected)
        return {"transport": "webhook", "sent": False, "reason": rejected}
    fmt = str(cfg.get("webhook_format") or "generic")
    if fmt not in WEBHOOK_FORMATS:
        fmt = "generic"
    body = json.dumps(_webhook_payload(incident, fmt), ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = _load_webhook_token(str(cfg.get("webhook_token_file") or ""))
    if token:
        digest = hmac.new(token.strip(), body, hashlib.sha256).hexdigest()
        headers["X-GMS-Ops-Signature"] = f"sha256={digest}"
    try:
        import httpx

        # follow_redirects=False：告警端点不应被 3xx 引流到其他主机。
        response = httpx.post(
            url, content=body, headers=headers,
            timeout=WEBHOOK_TIMEOUT_SECONDS, follow_redirects=False,
        )
        # 3xx 在禁用重定向时意味着告警没有真正送达接收端（跳转未被跟随），
        # 只把 2xx 视为已送达，否则失败投递会被标记 sent 而不再重试。
        sent = 200 <= response.status_code < 300
        return {
            "transport": "webhook",
            "sent": sent,
            "status_code": response.status_code,
        }
    except Exception:
        message = record_internal_error(
            logger, "运维告警 webhook 发送", "ops webhook notification failed"
        )
        return {"transport": "webhook", "sent": False, "error": message}


def send_email(incident: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    to = str(cfg.get("email_to") or "")
    if not to:
        return {"transport": "email", "sent": False, "reason": "no email_to"}
    try:
        from features.email import send_email as send_email_message

        # 不传 manager：走全局 SMTP 默认配置，无需 per-owner 凭据。
        # 台账回链：channel=ops + ref 指向触发本告警的 incident。
        result = send_email_message(
            to,
            f"[GMS ATS 运维告警] {incident.get('kind', 'incident')} {incident.get('subject', '')}".strip(),
            _incident_text(incident),
            channel="ops",
            ref_type="ops_event",
            ref_id=str(incident.get("id") or ""),
        )
        return {"transport": "email", **result}
    except Exception:
        message = record_internal_error(
            logger, "运维告警邮件发送", "ops email notification failed"
        )
        return {"transport": "email", "sent": False, "error": message}


def notify_incident(incident: dict[str, Any], *, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dispatch one incident to the enabled transports. Never raises."""
    effective = cfg or load_ops_alerts_config()
    if not effective.get("enabled"):
        return {"sent": [], "reason": "ops alerts disabled"}
    results = []
    if effective.get("webhook_url"):
        results.append(send_webhook(incident, effective))
    if effective.get("email_to"):
        results.append(send_email(incident, effective))
    return {"sent": results, "ok": all(item.get("sent") for item in results) if results else True}
