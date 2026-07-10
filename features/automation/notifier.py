"""Completion notifications for automation runs.

Fires when a run reaches a terminal state. Transports are individually gated by
the profile's ``reporting`` block and silently skipped when unconfigured, so a
missing SMTP server or Gerrit credential never breaks the pipeline.

Currently implemented:
- email: reuses the public ``features.email.send_email`` API.

Reserved (wired but no-op until their clients are wired in):
- gerrit_comment / gerrit_verified_label
- redmine_note
"""

from __future__ import annotations

import json
import logging
from typing import Any


logger = logging.getLogger(__name__)


def _run_reporting(run: dict[str, Any]) -> dict[str, Any]:
    try:
        plan = json.loads(run.get("test_plan_json") or "{}")
    except json.JSONDecodeError:
        return {}
    reporting = plan.get("reporting") if isinstance(plan.get("reporting"), dict) else {}
    return reporting or {}


def _summarize(run: dict[str, Any]) -> str:
    profile = run.get("profile_id") or run.get("id", "")
    status = run.get("status", "")
    artifact = run.get("artifact_path") or run.get("artifact_url") or "-"
    report = run.get("report_timestamp") or "-"
    error = run.get("error") or ""
    lines = [
        "GMS ATS 运行完成",
        f"Profile: {profile}",
        f"状态: {status}",
        f"固件: {artifact}",
        f"报告: {report}",
    ]
    if error:
        lines.append(f"错误: {error}")
    return "\n".join(lines)


def _send_email(run: dict[str, Any], reporting: dict[str, Any]) -> dict[str, Any]:
    to = reporting.get("email_to") or ""
    if not to:
        return {"transport": "email", "sent": False, "reason": "no email_to"}
    try:
        from features.email import send_email
        from features.redmine import config_manager

        result = send_email(
            to,
            f"[GMS ATS] {run.get('profile_id', 'run')} {run.get('status', '')}",
            _summarize(run),
            manager=config_manager,
        )
        return {"transport": "email", **result}
    except Exception as exc:
        logger.exception("email notification failed")
        return {"transport": "email", "sent": False, "error": str(exc)}


def _send_gerrit(run: dict[str, Any], reporting: dict[str, Any]) -> dict[str, Any]:
    change_id = run.get("gerrit_change_id") or ""
    if not change_id or not (
        reporting.get("gerrit_comment") or reporting.get("gerrit_verified_label")
    ):
        return {"transport": "gerrit", "sent": False, "reason": "not configured"}
    # Gerrit review/comment transport (SSH `gerrit review`) is intentionally a
    # no-op here until a shared comment helper is extracted from the Gerrit
    # module. Logged so operators know it was attempted.
    logger.info("gerrit notification requested for change %s (transport pending)", change_id)
    return {"transport": "gerrit", "sent": False, "reason": "transport not wired"}


def _send_redmine(run: dict[str, Any], reporting: dict[str, Any]) -> dict[str, Any]:
    issue_id = reporting.get("redmine_issue_id") or ""
    if not issue_id:
        return {"transport": "redmine", "sent": False, "reason": "not configured"}
    # Redmine update is async and the notifier runs in a worker thread without
    # an event loop. Reserved until a sync adapter is added; logged as pending.
    logger.info("redmine notification requested for issue %s (transport pending)", issue_id)
    return {"transport": "redmine", "sent": False, "reason": "transport not wired"}


def notify_run_completion(run: dict[str, Any]) -> dict[str, Any]:
    """Dispatch enabled notifications for a terminal run. Never raises."""
    reporting = _run_reporting(run)
    if not reporting:
        return {"sent": [], "reason": "no reporting config"}
    transports = reporting.get("transports") or ["email"]
    if isinstance(transports, str):
        transports = [t.strip() for t in transports.split(",") if t.strip()]
    handlers = {"email": _send_email, "gerrit": _send_gerrit, "redmine": _send_redmine}
    results = []
    for transport in transports:
        handler = handlers.get(transport)
        if handler is None:
            results.append({"transport": transport, "sent": False, "reason": "unknown transport"})
            continue
        try:
            results.append(handler(run, reporting))
        except Exception as exc:
            results.append({"transport": transport, "sent": False, "error": str(exc)})
    return {"sent": results}
