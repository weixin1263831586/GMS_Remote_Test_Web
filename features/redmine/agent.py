"""RedmineAgent: nightly Redmine triage and report generation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from features.redmine.analysis_ai import AiAnalysisMixin
from features.redmine.analysis_attachments import IMAGE_ATTACHMENT_RE, PROCESS_ATTACHMENT_RE, AttachmentAnalysisMixin
from features.redmine.analysis_issue import IssueAnalysisMixin
from features.redmine.analysis_reporting import ReportingAnalysisMixin
from features.redmine.analysis_resolution import ResolutionAnalysisMixin
from features.redmine.analysis_similarity import SimilarityAnalysisMixin
from features.redmine.client import RedmineClient
from features.redmine.config import config_manager
from features.redmine.repository import (
    RESOLVED_STATUS_NAMES as RESOLVED_STATUSES,
)
from features.redmine.repository import (
    RedmineAgentDB,
)
from foundation.config import settings


logger = logging.getLogger(__name__)


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
    """Normalize a Redmine timestamp to ISO 8601 with a 'T' separator.

    Redmine/python-redmine may hand back either a ``datetime`` or a string like
    ``"2026-06-27 09:08:50"`` (space separator). Normalizing on store keeps the
    DB consistent so list views sort and compare timestamps uniformly.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    text = str(value).strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).isoformat(timespec="seconds")
        except ValueError:
            continue
    # Last resort: collapse a space separator to 'T' if it looks like a timestamp.
    return text.replace(" ", "T", 1) if len(text) >= 10 and text[4:5] == "-" else text


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

class RedmineAgent(
    AttachmentAnalysisMixin,
    IssueAnalysisMixin,
    SimilarityAnalysisMixin,
    AiAnalysisMixin,
    ResolutionAnalysisMixin,
    ReportingAnalysisMixin,
):
    """Batch agent for assigned Redmine issue triage."""

    def __init__(
        self,
        db: RedmineAgentDB | None = None,
        *,
        redmine_config_manager: Any | None = None,
        attachments_dir: Any | None = None,
        report_analyzer_factory: Callable[..., Any] | None = None,
        ai_analyzer_factory: Callable[..., Any] | None = None,
    ):
        self.db = db or RedmineAgentDB()
        self.config_manager = redmine_config_manager or config_manager
        self.attachments_dir = attachments_dir or settings.data_root / "redmine/attachments"
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        self._ai_config_cache: dict[str, Any] | None = None
        self.report_analyzer_factory = report_analyzer_factory
        self.ai_analyzer_factory = ai_analyzer_factory

    # ------------------------------------------------------------------
    # Full sync — build / update complete issue database
    # ------------------------------------------------------------------

    async def sync_all_assigned_issues(
        self,
        analyze_new: bool = True,
        max_analyze: int = 20,
        run_id: str = "",
        assignee_id: int | None = None,
        assignee_name: str = "",
    ) -> dict[str, Any]:
        """Fetch ALL assigned issues and store them. Optionally analyze unanalyzed ones.

        ``assignee_id``/``assignee_name`` restricts the sync to issues assigned to
        that specific Redmine user. When both are omitted the authenticated user
        (``assigned_to_id="me"``) is used for backwards compatibility.
        """
        run_id = run_id or self._generate_run_id("sync-")
        client = self._make_client()

        current_user = await client.get_current_user()
        current_user_name = self._get_user_name(current_user)

        resolved_assignee_id, resolved_assignee_name = await self._resolve_assignee(
            client,
            assignee_id=assignee_id,
            assignee_name=assignee_name,
            current_user=current_user,
        )
        assigned_to = resolved_assignee_name or current_user_name

        self.db.create_run(run_id, "sync", _now_iso(), _now_iso(), max_analyze)
        self.db.update_run(run_id, assigned_to=assigned_to)

        try:
            _agent_cfg = _load_agent_config()
            if resolved_assignee_id is not None:
                all_issues = await client.fetch_issues_by_assignee(
                    resolved_assignee_id,
                    status_id="*",
                    limit=_agent_cfg["sync_max_issues"],
                )
            else:
                all_issues = await client.fetch_all_assigned_issues(status_id="*", limit=_agent_cfg["sync_max_issues"])
            logger.info("[RedmineAgent] sync fetched %d assigned issues for %s", len(all_issues), assigned_to)

            new_count = 0
            updated_count = 0
            detail_refreshed = 0
            to_analyze: list[list] = []

            for issue_stub in all_issues:
                issue_id = int(issue_stub.id)
                existing = self.db.get_issue(issue_id)
                stub_data = self._stub_to_dict(issue_stub, run_id)
                status_name = stub_data["status_name"]
                stub_data["is_resolved"] = int(status_name in RESOLVED_STATUSES)

                if stub_data["is_resolved"] == 0 and detail_refreshed < _agent_cfg["detail_sync_limit"]:
                    try:
                        detail_data = await self.fetch_issue_snapshot(client, issue_id, run_id)
                        if existing:
                            detail_data["attachments_json"] = self._merge_attachment_analysis(
                                existing.get("attachments_json") or [],
                                detail_data.get("attachments_json") or [],
                            )
                        stub_data.update({
                            key: value
                            for key, value in detail_data.items()
                            if value not in (None, "", [], {})
                        })
                        stub_data["is_resolved"] = int(stub_data.get("status_name") in RESOLVED_STATUSES)
                        detail_refreshed += 1
                    except Exception as exc:
                        logger.warning("[RedmineAgent] sync detail refresh failed for %s: %s", issue_id, exc)

                if existing:
                    old_status = existing.get("status_name") or ""
                    if old_status != status_name:
                        self.db.record_status_change(issue_id, old_status, status_name)
                    stub_data["analysis_status"] = existing.get("analysis_status") or "pending"
                    stub_data["scan_count"] = (existing.get("scan_count") or 0) + 1
                    self._preserve_existing_analysis_fields(stub_data, existing)
                    self.db.upsert_issue(stub_data)
                    updated_count += 1
                    if existing.get("analysis_status") in (None, "pending", ""):
                        to_analyze.append((issue_stub, issue_id))
                else:
                    stub_data["analysis_status"] = "pending"
                    stub_data["scan_count"] = 1
                    self.db.upsert_issue(stub_data)
                    new_count += 1
                    to_analyze.append((issue_stub, issue_id))

            # Analyze unanalyzed issues
            processed = 0
            failed = 0
            if analyze_new:
                for issue_stub, issue_id in to_analyze[:max_analyze]:
                    try:
                        await self.analyze_issue(client, issue_id, run_id)
                        processed += 1
                    except Exception as exc:
                        failed += 1
                        logger.error("[RedmineAgent] sync analyze issue %s failed: %s", issue_id, exc, exc_info=True)
                        self.db.upsert_issue({
                            "issue_id": issue_id,
                            "run_id": run_id,
                            "subject": str(getattr(issue_stub, "subject", "")),
                            "analysis_status": "failed",
                            "error": str(exc),
                        })

            summary = {
                "assigned_to": assigned_to,
                "total_fetched": len(all_issues),
                "new_count": new_count,
                "updated_count": updated_count,
                "detail_refreshed": detail_refreshed,
                "analyzed_count": processed,
                "failed_count": failed,
            }
            self.db.update_run(
                run_id,
                status="done",
                finished_at=_now_iso(),
                issue_count=len(all_issues),
                processed_count=processed,
                failed_count=failed,
                summary_json=json.dumps(summary, ensure_ascii=False),
            )
            return {"run_id": run_id, "status": "done", **summary}

        except Exception as exc:
            logger.error("[RedmineAgent] sync failed: %s", exc, exc_info=True)
            self.db.update_run(run_id, status="failed", finished_at=_now_iso(), error=str(exc))
            return {"run_id": run_id, "status": "failed", "error": str(exc)}
        finally:
            await client.close()

    async def _resolve_assignee(
        self,
        client: RedmineClient,
        assignee_id: int | None,
        assignee_name: str,
        current_user: Any,
    ) -> tuple[int | None, str]:
        """Resolve sync target to (redmine_user_id, display_name)."""
        name = (assignee_name or "").strip()
        if assignee_id is not None and int(assignee_id) > 0:
            user = await asyncio.to_thread(
                lambda: client._redmine.user.get(int(assignee_id))
            )
            return int(assignee_id), self._get_user_name(user)
        if name:
            users = await client.search_users(name, limit=10)
            exact_match: dict[str, Any] | None = None
            for user in users:
                if user.get("name") == name or user.get("login") == name:
                    exact_match = user
                    break
            if not exact_match and users:
                exact_match = users[0]
            if exact_match:
                return int(exact_match["id"]), exact_match.get("name") or self._get_user_name(exact_match)
        return None, self._get_user_name(current_user)

    # ------------------------------------------------------------------
    # Timed scan (daily midnight run)
    # ------------------------------------------------------------------

    async def run(
        self,
        hours: int = 24,
        max_issues: int = 20,
        run_id: str | None = None,
        mode: str = "manual",
    ) -> dict[str, Any]:
        """Run one RedmineAgent scan. Cap is configurable via config.json or REDMINE_AGENT_MAX_ISSUES env."""
        _cap = _load_agent_config()["max_issues_per_run"]
        max_issues = max(1, min(int(max_issues or 20), _cap))
        run_id = run_id or self._generate_run_id()
        window_end_dt = datetime.now()
        window_start_dt = window_end_dt - timedelta(hours=max(1, int(hours or 24)))
        created_from = window_start_dt.strftime("%Y-%m-%d")
        created_to = window_end_dt.strftime("%Y-%m-%d")
        self.db.create_run(run_id, mode, window_start_dt.isoformat(timespec="seconds"), window_end_dt.isoformat(timespec="seconds"), max_issues)

        try:
            client = self._make_client()
            current_user = await client.get_current_user()
            assigned_to = self._get_user_name(current_user)
            self.db.update_run(run_id, assigned_to=assigned_to)

            fetched_issues = await client.search_assigned_issues(created_from, created_to, limit=max(max_issues * 5, 50))
            issues = [
                item for item in fetched_issues
                if self._issue_created_in_window(item, window_start_dt, window_end_dt)
            ][:max_issues]
            self.db.update_run(run_id, issue_count=len(issues))

            processed = 0
            failed = 0
            issue_results = []
            for issue_stub in issues:
                issue_id = int(issue_stub.id)
                try:
                    result = await self.analyze_issue(client, issue_id, run_id)
                    issue_results.append(result)
                    processed += 1
                except Exception as exc:
                    failed += 1
                    logger.error("[RedmineAgent] issue %s failed: %s", issue_id, exc, exc_info=True)
                    self.db.upsert_issue({
                        "issue_id": issue_id,
                        "run_id": run_id,
                        "subject": str(getattr(issue_stub, "subject", "")),
                        "analysis_status": "failed",
                        "error": str(exc),
                    })

            report = self._build_run_report(run_id, issue_results)
            report_path = self.db.write_run_report(run_id, report)
            summary = {
                "assigned_to": assigned_to,
                "issue_count": len(issues),
                "processed_count": processed,
                "failed_count": failed,
                "report_path": report_path,
            }
            self.db.update_run(
                run_id,
                status="done",
                finished_at=_now_iso(),
                processed_count=processed,
                failed_count=failed,
                report_path=report_path,
                summary_json=json.dumps(summary, ensure_ascii=False),
            )
            return {"run_id": run_id, "status": "done", **summary}
        except Exception as exc:
            logger.error("[RedmineAgent] run failed: %s", exc, exc_info=True)
            self.db.update_run(run_id, status="failed", finished_at=_now_iso(), error=str(exc))
            return {"run_id": run_id, "status": "failed", "error": str(exc)}
        finally:
            await client.close()

    # ------------------------------------------------------------------
    # Single issue analysis
    # ------------------------------------------------------------------

    async def analyze_issue(self, client: RedmineClient, issue_id: int, run_id: str = "") -> dict[str, Any]:
        issue = await client.get_issue(str(issue_id), include=["attachments", "journals"])
        attachments = await client.list_issue_attachments(str(issue_id))
        journals = self._extract_journals(getattr(issue, "journals", []))
        attachment_records = []
        failures = []

        for attachment in attachments:
            attachment_result = await self._process_attachment(client, issue_id, attachment)
            attachment_records.append(attachment_result)
            analysis = attachment_result.get("analysis_json") or {}
            if analysis.get("failures"):
                failures.extend(analysis.get("failures") or [])

        issue_payload = self._issue_payload(issue, run_id, journals, attachment_records, failures)

        issue_payload["category"] = self._detect_category(issue_payload.get("subject", ""), issue_payload.get("description", ""))

        # Track status
        status_name = _obj_name(getattr(issue, "status", None))
        issue_payload["is_resolved"] = int(status_name in RESOLVED_STATUSES)

        # Find similar references with similarity scoring
        references = await self._find_similar_references(client, issue_payload, failures)

        # AI structured analysis
        ai_result = await self._summarize_with_model(issue_payload, failures, references)

        # Extract structured seven fields
        structured = self._extract_structured_fields(ai_result, issue_payload, failures, references)
        issue_payload.update(structured)

        # Build document
        document = self._build_issue_document(issue_payload)
        doc_path = self.db.write_issue_doc(issue_id, document)
        issue_payload["doc_path"] = doc_path
        issue_payload["doc_content"] = document
        issue_payload["analysis_status"] = "done"

        self.db.upsert_issue(issue_payload)
        self.db.replace_references(issue_id, references)
        return {**issue_payload, "doc_path": doc_path}

    async def fetch_issue_snapshot(self, client: RedmineClient, issue_id: int, run_id: str = "") -> dict[str, Any]:
        """Fetch issue metadata, journals, and attachment names without heavy analysis."""
        issue = await client.get_issue(str(issue_id), include=["attachments", "journals"])
        attachments = await client.list_issue_attachments(str(issue_id))
        journals = self._extract_journals(getattr(issue, "journals", []))
        attachment_records = [
            {
                "attachment_id": attachment.id,
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "content_url": attachment.content_url,
                "filesize": attachment.filesize,
                "local_path": "",
                "analysis_json": {},
                "status": "metadata",
                "error": "",
            }
            for attachment in attachments
        ]
        issue_payload = self._issue_payload(issue, run_id, journals, attachment_records, [])
        status_name = _obj_name(getattr(issue, "status", None))
        issue_payload["is_resolved"] = int(status_name in RESOLVED_STATUSES)
        issue_payload["category"] = self._detect_category(issue_payload.get("subject", ""), issue_payload.get("description", ""))
        issue_payload["analysis_status"] = "pending"
        return issue_payload

    @staticmethod
    def _preserve_existing_analysis_fields(payload: dict[str, Any], existing: dict[str, Any]) -> None:
        for key in SYNC_PRESERVE_FIELDS:
            if payload.get(key) in (None, "", [], {}) and existing.get(key) not in (None, "", [], {}):
                payload[key] = existing.get(key)
        for key in ("journals_json", "attachments_json"):
            if payload.get(key) in (None, [], {}) and existing.get(key):
                payload[key] = existing.get(key)
        if not payload.get("category") and existing.get("category"):
            payload["category"] = existing.get("category")

    @staticmethod
    def _merge_attachment_analysis(existing_items: list[dict[str, Any]], fresh_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        existing_by_id = {
            str(item.get("attachment_id") or item.get("id") or ""): item
            for item in existing_items
            if isinstance(item, dict)
        }
        merged: list[dict[str, Any]] = []
        for fresh in fresh_items:
            if not isinstance(fresh, dict):
                continue
            attachment_id = str(fresh.get("attachment_id") or fresh.get("id") or "")
            old = existing_by_id.get(attachment_id) or {}
            item = dict(fresh)
            if old.get("analysis_json") and not item.get("analysis_json"):
                item["analysis_json"] = old.get("analysis_json")
            if old.get("local_path") and not item.get("local_path"):
                item["local_path"] = old.get("local_path")
            if old.get("status") and item.get("status") == "metadata":
                item["status"] = old.get("status")
            merged.append(item)
        return merged

    # ------------------------------------------------------------------
    # Client factory
    # ------------------------------------------------------------------

    def _make_client(self) -> RedmineClient:
        redmine_config = self.config_manager.get_redmine_config()
        creds = self.config_manager.load_redmine_credentials()
        if not creds:
            raise RuntimeError("Redmine credentials not configured")
        return RedmineClient(redmine_config["base_url"], creds.get("username", ""), creds.get("password", ""))

    @staticmethod
    def _get_user_name(user: Any) -> str:
        return f"{getattr(user, 'firstname', '')} {getattr(user, 'lastname', '')}".strip() or getattr(user, "login", "")

    @staticmethod
    def _generate_run_id(prefix: str = "") -> str:
        return datetime.now().strftime("%Y%m%d%H%M%S") + "-" + prefix + uuid.uuid4().hex[:8]

    @staticmethod
    def _stub_to_dict(issue_stub: Any, run_id: str = "") -> dict[str, Any]:
        """Extract common fields from a Redmine issue stub into a dict."""
        subject = str(issue_stub.subject or "")
        description = str(issue_stub.description or "")
        fixed_version = _obj_name(getattr(issue_stub, "fixed_version", None))
        return {
            "issue_id": int(issue_stub.id),
            "run_id": run_id,
            "subject": subject,
            "status_name": _obj_name(getattr(issue_stub, "status", None)),
            "priority_name": _obj_name(getattr(issue_stub, "priority", None)),
            "project_name": _obj_name(getattr(issue_stub, "project", None)),
            "tracker_name": _obj_name(getattr(issue_stub, "tracker", None)),
            "author_name": _obj_name(getattr(issue_stub, "author", None)),
            "assigned_to_name": _obj_name(getattr(issue_stub, "assigned_to", None)),
            "created_on": _iso(issue_stub.created_on),
            "updated_on": _iso(issue_stub.updated_on),
            "description": description,
            "fixed_version": fixed_version,
            "component": RedmineAgent._extract_custom_field(issue_stub, "Component_fae"),
            "soc_platform": RedmineAgent._parse_soc_platform(subject, description, fixed_version),
            "android_version": RedmineAgent._parse_android_version(subject, description, fixed_version),
            "start_date": _iso(issue_stub.start_date),
            "due_date": _iso(issue_stub.due_date),
            "closed_on": _iso(issue_stub.closed_on),
            "done_ratio": int(getattr(issue_stub, "done_ratio", 0) or 0),
        }

    # ------------------------------------------------------------------
    # Category detection
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_category(subject: str, description: str) -> str:
        text = f"{subject} {description}".upper()
        for keyword, category in (("VTS", "VTS"), ("GTS", "GTS"), ("CTS", "CTS"), ("GSI", "GSI"), ("GMS", "GMS认证")):
            if keyword in text:
                return category
        return ""

    @staticmethod
    def _parse_soc_platform(subject: str, description: str, fixed_version: str) -> str:
        """Extract SoC platform (e.g. RK3576, RK3399) from subject, description or version."""
        text = f"{subject} {description} {fixed_version}"
        match = re.search(r"(RK\d{4,})", text, re.IGNORECASE)
        return match.group(1).upper() if match else ""

    @staticmethod
    def _parse_android_version(subject: str, description: str, fixed_version: str) -> str:
        """Extract Android version (e.g. Android16, Android14) from text."""
        text = f"{subject} {description} {fixed_version}"
        # Match "Android16", "Android 14", "ANDROID16.0", "ANDROID12_SDK"
        match = re.search(r"android\s*(\d+(?:\.\d+)*)", text, re.IGNORECASE)
        if match:
            return f"Android{match.group(1)}"
        return ""

    @staticmethod
    def _extract_custom_field(issue: Any, field_name: str) -> str:
        """Extract a custom field value from a Redmine issue object."""
        for field in getattr(issue, "custom_fields", []):
            if getattr(field, "name", "") == field_name:
                return str(field.value or "")
        return ""

    # ------------------------------------------------------------------
    # Issue window filter
    # ------------------------------------------------------------------

    @staticmethod
    def _issue_created_in_window(issue: Any, start: datetime, end: datetime) -> bool:
        created = getattr(issue, "created_on", None)
        if isinstance(created, datetime):
            if created.tzinfo is not None:
                created = created.replace(tzinfo=None)
            return start <= created <= end
        return True
