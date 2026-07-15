"""Completion notifications for automation runs.

Fires when a run reaches a terminal state. Transports are individually gated by
the profile's ``reporting`` block and silently skipped when unconfigured, so a
missing SMTP server or Gerrit credential never breaks the pipeline.

Email, Gerrit review and Redmine notes all reuse their feature-owned clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any


logger = logging.getLogger(__name__)


def _run_async(coro: Any) -> Any:
    """Run *coro* to completion, even when a loop is already running.

    The automation worker calls ``notify_run_completion`` from a background
    thread that usually has no event loop, so ``asyncio.run`` works.  When the
    notifier is invoked from an async context (e.g. during tests or an
    in-process worker), ``asyncio.run`` raises *RuntimeError*.  Detect that
    situation and run the coroutine on a private loop in a helper thread.
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is None:
        return asyncio.run(coro)

    result: dict[str, Any] = {}

    def _runner() -> None:
        result["value"] = asyncio.run(coro)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    return result.get("value")


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
        f"Worker: {run.get('worker_id') or '-'}",
        f"Cluster Job: {run.get('cluster_job_id') or '-'}",
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
            manager=config_manager.for_owner(
                str(run.get("created_by") or "automation")
            ),
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
    try:
        from features.gerrit import gerrit_config_manager, post_gerrit_review

        cfg = gerrit_config_manager.for_owner(
            str(run.get("created_by") or "automation")
        ).get_gerrit_dashboard_config()
        verified = None
        if reporting.get("gerrit_verified_label"):
            verified = 1 if run.get("status") == "completed" else -1
        result = _run_async(post_gerrit_review(
            cfg,
            change_id=str(change_id),
            patchset=str(run.get("gerrit_patchset") or ""),
            message=_summarize(run) if reporting.get("gerrit_comment") else "",
            verified=verified,
        ))
        return {"transport": "gerrit", **result}
    except Exception as exc:
        logger.exception("gerrit notification failed")
        return {"transport": "gerrit", "sent": False, "error": str(exc)}


def _send_redmine(run: dict[str, Any], reporting: dict[str, Any]) -> dict[str, Any]:
    try:
        plan = json.loads(run.get("test_plan_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        plan = {}
    redmine = plan.get("redmine") if isinstance(plan.get("redmine"), dict) else {}
    issue_id = (
        reporting.get("redmine_issue_id")
        or plan.get("redmine_issue_id")
        or redmine.get("issue_id")
        or ""
    )
    if not issue_id:
        return {"transport": "redmine", "sent": False, "reason": "not configured"}
    try:
        from features.redmine import RedmineClient, config_manager

        manager = config_manager.for_owner(
            str(run.get("created_by") or "automation")
        )
        redmine_config = manager.get_redmine_config()
        credentials = manager.load_redmine_credentials()
        if not credentials.get("username") or not credentials.get("password"):
            return {
                "transport": "redmine",
                "sent": False,
                "error": "Redmine credentials are not configured for the ATS owner",
            }

        async def publish() -> None:
            client = RedmineClient(
                redmine_config.get("base_url", ""),
                credentials.get("username", ""),
                credentials.get("password", ""),
            )
            try:
                await client.update_issue(str(issue_id), notes=_summarize(run))
            finally:
                await client.close()

        _run_async(publish())
        return {"transport": "redmine", "sent": True, "issue_id": str(issue_id)}
    except Exception as exc:
        logger.exception("redmine notification failed")
        return {"transport": "redmine", "sent": False, "error": str(exc)}


def notify_run_completion(run: dict[str, Any]) -> dict[str, Any]:
    """Dispatch enabled notifications for a terminal run. Never raises."""
    reporting = _run_reporting(run)
    if not reporting:
        return {"sent": [], "reason": "no reporting config"}
    transports = reporting.get("transports") or []
    if isinstance(transports, str):
        transports = [t.strip() for t in transports.split(",") if t.strip()]
    if not transports:
        if reporting.get("email_to"):
            transports.append("email")
        if reporting.get("gerrit_comment") or reporting.get("gerrit_verified_label"):
            transports.append("gerrit")
        if reporting.get("redmine_issue_id"):
            transports.append("redmine")
        try:
            plan = json.loads(run.get("test_plan_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            plan = {}
        if plan.get("redmine_issue_id") and "redmine" not in transports:
            transports.append("redmine")
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
    required = reporting.get("required_transports") or []
    if reporting.get("required") is True:
        required = list(transports)
    if isinstance(required, str):
        required = [item.strip() for item in required.split(",") if item.strip()]
    failed_required = [
        item.get("transport") for item in results
        if item.get("transport") in required and not item.get("sent")
    ]
    return {"sent": results, "ok": not failed_required, "failed_required": failed_required}
