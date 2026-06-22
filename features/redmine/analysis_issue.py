"""RedmineAgent: nightly Redmine triage and report generation."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Any

from features.redmine.config import config_manager


logger = logging.getLogger(__name__)


PROCESS_ATTACHMENT_RE = re.compile(r"\.(zip|7z|rar|tar|tgz|gz|xml|txt|log|png|jpg|jpeg|webp|bmp|docx)$", re.IGNORECASE)
IMAGE_ATTACHMENT_RE = re.compile(r"\.(png|jpg|jpeg|webp|bmp)$", re.IGNORECASE)

# Enhanced error patterns for structured extraction
_ERROR_LINE_PATTERNS = [
    # Stack traces
    r"at\s+[\w.$]+\([^)]*\.\w+:\d+\)",
    r"Caused by:\s*[\w.]+(?:Exception|Error)",
    r"java\.\w+\.\w+(?:Exception|Error)",
    # JUnit / Android test
    r"junit\.framework\.(?:AssertionFailedError|ComparisonFailure)",
    r"android\.os\.ServiceSpecificException",
    r"android\.hardware\.\w+",
    r"com\.android\.\w+\.(?:Exception|Error)",
    # GMS / certification
    r"not certified|attestation|integrity|KeyMint|RKPD|STRONGBOX",
    r"Cannot add more profiles|config_user_types|config_multiuserMaximumUsers",
    # General
    r"\bFAIL(?:URE)?:\b",
    r"\bASSUMPTION_FAILURE:\b",
    r"\bFATAL\b",
    r"\bdenied\b",
    r"\bError:\s",
]
_ERROR_LINE_RE = re.compile("|".join(_ERROR_LINE_PATTERNS), re.IGNORECASE)

AI_MODEL_TIMEOUT = 120          # seconds for AI model HTTP request
AI_MODEL_MAX_TOKENS = 2400      # max tokens for AI model response
MAX_FAILURE_LINES = 30          # max error lines to extract
MAX_ERROR_BLOCKS = 5            # max grouped error blocks
SIMILARITY_THRESHOLD_HIGH = 70  # score >= this → "high" similarity
SIMILARITY_THRESHOLD_MEDIUM = 40  # score >= this → "medium" similarity
MAX_REFERENCES = 5              # max similar references to return
TOP_CANDIDATES_FOR_AI = 8       # top candidates sent to AI semantic scoring


def _load_agent_config() -> dict[str, Any]:
    """Load redmine_agent section from config.json, with env overrides."""
    cfg = config_manager.load_config().get("redmine_agent", {})
    return {
        "max_issues_per_run": int(os.getenv("REDMINE_AGENT_MAX_ISSUES", cfg.get("max_issues_per_run", 50))),
        "sync_max_issues": int(os.getenv("REDMINE_AGENT_SYNC_MAX_ISSUES", cfg.get("sync_max_issues", 5000))),
        "detail_sync_limit": int(os.getenv("REDMINE_AGENT_DETAIL_SYNC_LIMIT", cfg.get("detail_sync_limit", 5000))),
    }


SYNC_PRESERVE_FIELDS = {
    "failures_json",
    "references_json",
    "ai_json",
    "summary",
    "reply_draft",
    "doc_path",
    "doc_content",
    "error",
    "error_info",
    "error_analysis",
    "solution",
    "patch_direction",
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def _obj_name(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "name", "") or value)


def _obj_email(value: Any) -> str:
    if value is None:
        return ""
    return str(
        getattr(value, "mail", "")
        or getattr(value, "email", "")
        or getattr(value, "login", "")
        or ""
    )


def _truncate(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"



class IssueAnalysisMixin:
    def _issue_payload(self, issue: Any, run_id: str, journals: list[dict[str, Any]], attachment_records: list[dict[str, Any]], failures: list[dict[str, Any]]) -> dict[str, Any]:
        fixed_version = _obj_name(getattr(issue, "fixed_version", None))
        return {
            "issue_id": int(issue.id),
            "run_id": run_id,
            "subject": str(issue.subject or ""),
            "status_name": _obj_name(getattr(issue, "status", None)),
            "priority_name": _obj_name(getattr(issue, "priority", None)),
            "project_name": _obj_name(getattr(issue, "project", None)),
            "tracker_name": _obj_name(getattr(issue, "tracker", None)),
            "author_name": _obj_name(getattr(issue, "author", None)),
            "assigned_to_name": _obj_name(getattr(issue, "assigned_to", None)),
            "created_on": _iso(issue.created_on),
            "updated_on": _iso(issue.updated_on),
            "description": str(issue.description or ""),
            "fixed_version": fixed_version,
            "component": self._extract_custom_field(issue, "Component_fae"),
            "soc_platform": self._parse_soc_platform(
                str(issue.subject or ""),
                str(issue.description or ""),
                fixed_version,
            ),
            "android_version": self._parse_android_version(
                str(issue.subject or ""),
                str(issue.description or ""),
                fixed_version,
            ),
            "start_date": _iso(issue.start_date),
            "due_date": _iso(issue.due_date),
            "closed_on": _iso(issue.closed_on),
            "done_ratio": int(getattr(issue, "done_ratio", 0) or 0),
            "journals_json": journals,
            "attachments_json": attachment_records,
            "failures_json": failures[:50],
        }

    def _extract_journals(self, journals: list[Any]) -> list[dict[str, Any]]:
        result = []
        for item in journals:
            details = []
            for detail in getattr(item, "details", []):
                details.append({
                    "property": str(getattr(detail, "property", "")),
                    "name": str(getattr(detail, "name", "")),
                    "old_value": str(getattr(detail, "old_value", "")),
                    "new_value": str(getattr(detail, "new_value", "")),
                })
            result.append({
                "id": str(getattr(item, "id", "")),
                "user": _obj_name(getattr(item, "user", None)),
                "user_email": _obj_email(getattr(item, "user", None)),
                "created_on": _iso(getattr(item, "created_on", "")),
                "notes": _truncate(str(getattr(item, "notes", "")), 2000),
                "details": details,
            })
        return result

    # ------------------------------------------------------------------
    # Similar reference finding with multi-dimension similarity scoring
    # ------------------------------------------------------------------
