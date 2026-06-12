"""RedmineAgent: nightly Redmine triage and report generation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from core.config import config_manager
from core.redmine_agent_db import RESOLVED_STATUS_NAMES as RESOLVED_STATUSES, RedmineAgentDB
from core.redmine_client import RedmineAttachment, RedmineClient
from core.report_analyzer import ReportAnalyzer
from core.settings import PROJECT_ROOT
from core.universal_ai import UniversalAIAnalyzer

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


def _load_agent_config() -> Dict[str, Any]:
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


def _truncate(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


class RedmineAgent:
    """Batch agent for assigned Redmine issue triage."""

    def __init__(self, db: Optional[RedmineAgentDB] = None):
        self.db = db or RedmineAgentDB()
        self.attachments_dir = Path(PROJECT_ROOT) / "data" / "redmine_agent_attachments"
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        self._ai_config_cache: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Full sync — build / update complete issue database
    # ------------------------------------------------------------------

    async def sync_all_assigned_issues(
        self,
        analyze_new: bool = True,
        max_analyze: int = 20,
        run_id: str = "",
    ) -> Dict[str, Any]:
        """Fetch ALL assigned issues and store them. Optionally analyze unanalyzed ones."""
        run_id = run_id or self._generate_run_id("sync-")
        client = self._make_client()

        current_user = await client.get_current_user()
        assigned_to = self._get_user_name(current_user)

        self.db.create_run(run_id, "sync", _now_iso(), _now_iso(), max_analyze)
        self.db.update_run(run_id, assigned_to=assigned_to)

        try:
            _agent_cfg = _load_agent_config()
            all_issues = await client.fetch_all_assigned_issues(status_id="*", limit=_agent_cfg["sync_max_issues"])
            logger.info("[RedmineAgent] sync fetched %d assigned issues", len(all_issues))

            new_count = 0
            updated_count = 0
            detail_refreshed = 0
            to_analyze: List[list] = []

            for issue_stub in all_issues:
                issue_id = int(getattr(issue_stub, "id"))
                existing = self.db.get_issue(issue_id)
                stub_data = self._stub_to_dict(issue_stub, run_id)
                status_name = stub_data["status_name"]
                stub_data["is_resolved"] = 1 if status_name in RESOLVED_STATUSES else 0

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
                        stub_data["is_resolved"] = 1 if stub_data.get("status_name") in RESOLVED_STATUSES else 0
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

    # ------------------------------------------------------------------
    # Timed scan (daily midnight run)
    # ------------------------------------------------------------------

    async def run(
        self,
        hours: int = 24,
        max_issues: int = 20,
        run_id: Optional[str] = None,
        mode: str = "manual",
    ) -> Dict[str, Any]:
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
                issue_id = int(getattr(issue_stub, "id"))
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

    async def analyze_issue(self, client: RedmineClient, issue_id: int, run_id: str = "") -> Dict[str, Any]:
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

        # Detect category from subject/description
        issue_payload["category"] = self._detect_category(issue_payload.get("subject", ""), issue_payload.get("description", ""))

        # Track status
        status_name = _obj_name(getattr(issue, "status", None))
        issue_payload["is_resolved"] = 1 if status_name in RESOLVED_STATUSES else 0

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

    async def fetch_issue_snapshot(self, client: RedmineClient, issue_id: int, run_id: str = "") -> Dict[str, Any]:
        """Fetch issue metadata, journals, and attachment names without heavy analysis."""
        issue = await client.get_issue(str(issue_id), include=["attachments", "journals"])
        attachments = await client.list_issue_attachments(str(issue_id))
        journals = self._extract_journals(getattr(issue, "journals", []))
        attachment_records = [
            {
                "attachment_id": attachment.id,
                "filename": attachment.filename,
                "content_type": attachment.content_type,
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
        issue_payload["is_resolved"] = 1 if status_name in RESOLVED_STATUSES else 0
        issue_payload["category"] = self._detect_category(issue_payload.get("subject", ""), issue_payload.get("description", ""))
        issue_payload["analysis_status"] = "pending"
        return issue_payload

    @staticmethod
    def _preserve_existing_analysis_fields(payload: Dict[str, Any], existing: Dict[str, Any]) -> None:
        for key in SYNC_PRESERVE_FIELDS:
            if payload.get(key) in (None, "", [], {}) and existing.get(key) not in (None, "", [], {}):
                payload[key] = existing.get(key)
        for key in ("journals_json", "attachments_json"):
            if payload.get(key) in (None, [], {}) and existing.get(key):
                payload[key] = existing.get(key)
        if not payload.get("category") and existing.get("category"):
            payload["category"] = existing.get("category")

    @staticmethod
    def _merge_attachment_analysis(existing_items: List[Dict[str, Any]], fresh_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        existing_by_id = {
            str(item.get("attachment_id") or item.get("id") or ""): item
            for item in existing_items
            if isinstance(item, dict)
        }
        merged: List[Dict[str, Any]] = []
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
        redmine_config = config_manager.get_redmine_config()
        creds = config_manager.load_redmine_credentials()
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
    def _stub_to_dict(issue_stub: Any, run_id: str = "") -> Dict[str, Any]:
        """Extract common fields from a Redmine issue stub into a dict."""
        subject = str(getattr(issue_stub, "subject") or "")
        description = str(getattr(issue_stub, "description") or "")
        fixed_version = _obj_name(getattr(issue_stub, "fixed_version", None))
        return {
            "issue_id": int(getattr(issue_stub, "id")),
            "run_id": run_id,
            "subject": subject,
            "status_name": _obj_name(getattr(issue_stub, "status", None)),
            "priority_name": _obj_name(getattr(issue_stub, "priority", None)),
            "project_name": _obj_name(getattr(issue_stub, "project", None)),
            "tracker_name": _obj_name(getattr(issue_stub, "tracker", None)),
            "author_name": _obj_name(getattr(issue_stub, "author", None)),
            "assigned_to_name": _obj_name(getattr(issue_stub, "assigned_to", None)),
            "created_on": _iso(getattr(issue_stub, "created_on")),
            "updated_on": _iso(getattr(issue_stub, "updated_on")),
            "description": description,
            "fixed_version": fixed_version,
            "component": RedmineAgent._extract_custom_field(issue_stub, "Component_fae"),
            "soc_platform": RedmineAgent._parse_soc_platform(subject, description, fixed_version),
            "android_version": RedmineAgent._parse_android_version(subject, description, fixed_version),
            "start_date": _iso(getattr(issue_stub, "start_date")),
            "due_date": _iso(getattr(issue_stub, "due_date")),
            "closed_on": _iso(getattr(issue_stub, "closed_on")),
            "done_ratio": int(getattr(issue_stub, "done_ratio", 0) or 0),
        }

    # ------------------------------------------------------------------
    # Category detection
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_category(subject: str, description: str) -> str:
        text = f"{subject} {description}".upper()
        if "VTS" in text:
            return "VTS"
        if "GTS" in text:
            return "GTS"
        if "CTS" in text:
            return "CTS"
        if "GSI" in text:
            return "GSI"
        if "GMS" in text:
            return "GMS认证"
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
                return str(getattr(field, "value") or "")
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

    # ------------------------------------------------------------------
    # Attachment processing
    # ------------------------------------------------------------------

    async def _process_attachment(self, client: RedmineClient, issue_id: int, attachment: RedmineAttachment) -> Dict[str, Any]:
        issue_dir = self.attachments_dir / str(issue_id)
        issue_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", attachment.filename or f"attachment_{attachment.id}")
        local_path = issue_dir / f"{attachment.id}-{safe_name}"
        status = "skipped"
        error = ""
        analysis: Dict[str, Any] = {"filename": attachment.filename, "failures": []}

        if PROCESS_ATTACHMENT_RE.search(attachment.filename or ""):
            try:
                await client.download_attachment(attachment.id, str(local_path), attachment.content_url)
                analysis = await asyncio.to_thread(self._analyze_local_attachment, str(local_path))
                status = "done" if analysis else "unparsed"
            except Exception as exc:
                error = str(exc)
                status = "failed"
                logger.warning("[RedmineAgent] attachment %s failed: %s", attachment.id, exc)
        item = {
            "issue_id": issue_id,
            "attachment_id": attachment.id,
            "filename": attachment.filename,
            "content_type": attachment.content_type,
            "filesize": attachment.filesize,
            "local_path": str(local_path) if local_path.exists() else "",
            "analysis_json": analysis or {},
            "status": status,
            "error": error,
        }
        self.db.insert_attachment(item)
        return item

    def _analyze_local_attachment(self, path: str) -> Dict[str, Any]:
        lower_path = path.lower()
        if lower_path.endswith((".txt", ".log")):
            return self._analyze_text_attachment(path)
        if IMAGE_ATTACHMENT_RE.search(lower_path):
            return self._analyze_image_attachment(path)
        if lower_path.endswith(".docx"):
            return self._analyze_docx_attachment(path)
        analyzer = ReportAnalyzer(temp_dir=tempfile.mkdtemp(prefix="redmine_agent_report_"))
        result = analyzer.analyze_file(path)
        if not result:
            return {"filename": os.path.basename(path), "failures": [], "summary": {}, "details": {}, "parsed": False}
        failures = []
        for item in (result.get("failures") or [])[:20]:
            failures.append({
                "name": item.get("name") or item.get("test_name") or "",
                "module": item.get("module") or "",
                "reason": _truncate(item.get("reason") or item.get("error_message") or "", 1200),
                "stack_trace": _truncate(item.get("stack_trace") or "", 1800),
            })
        return {
            "filename": os.path.basename(path),
            "parsed": True,
            "summary": result.get("summary") or {},
            "details": result.get("details") or {},
            "failures": failures,
        }

    def _analyze_text_attachment(self, path: str) -> Dict[str, Any]:
        try:
            content = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            content = ""
        error_blocks = self._extract_error_blocks(content)
        interesting_count = len(self._extract_failure_like_lines(content))
        failures = []
        if error_blocks:
            failures.append({
                "name": "text-log-analysis",
                "module": "log",
                "reason": _truncate("\n".join(error_blocks), 1200),
                "stack_trace": "",
            })
        return {
            "filename": os.path.basename(path),
            "parsed": True,
            "summary": {"interesting_lines": interesting_count, "error_blocks": len(error_blocks)},
            "details": {"type": "text"},
            "text_excerpt": _truncate(content, 3000),
            "failures": failures,
        }

    def _analyze_image_attachment(self, path: str) -> Dict[str, Any]:
        details: Dict[str, Any] = {"type": "image"}
        try:
            from PIL import Image

            with Image.open(path) as image:
                details.update({
                    "width": image.width,
                    "height": image.height,
                    "format": image.format or "",
                    "mode": image.mode or "",
                })
        except Exception as exc:
            details["error"] = str(exc)

        return {
            "filename": os.path.basename(path),
            "parsed": "error" not in details,
            "summary": details,
            "details": details,
            "text_excerpt": "",
            "failures": [],
        }

    def _analyze_docx_attachment(self, path: str) -> Dict[str, Any]:
        content = ""
        try:
            from docx import Document

            document = Document(path)
            paragraphs = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
            content = "\n".join(paragraphs)
        except Exception:
            try:
                with zipfile.ZipFile(path) as archive:
                    xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
                content = re.sub(r"<[^>]+>", " ", xml)
                content = re.sub(r"\s+", " ", content)
            except Exception:
                content = ""
        interesting = self._extract_failure_like_lines(content)
        failures = []
        if interesting:
            failures.append({
                "name": "docx-analysis",
                "module": "document",
                "reason": _truncate("\n".join(interesting), 1200),
                "stack_trace": "",
            })
        return {
            "filename": os.path.basename(path),
            "parsed": bool(content),
            "summary": {"interesting_lines": len(interesting), "characters": len(content)},
            "details": {"type": "docx"},
            "text_excerpt": _truncate(content, 3000),
            "failures": failures,
        }

    # ------------------------------------------------------------------
    # Error extraction
    # ------------------------------------------------------------------

    def _extract_failure_like_lines(self, content: str, limit: int = MAX_FAILURE_LINES) -> List[str]:
        """Extract individual error lines from content."""
        return [
            line.strip()
            for line in str(content or "").splitlines()
            if _ERROR_LINE_RE.search(line)
        ][:limit]

    def _extract_error_blocks(self, content: str, max_blocks: int = MAX_ERROR_BLOCKS) -> List[str]:
        """Group consecutive error lines into logical blocks."""
        current_block: List[str] = []
        blocks = []
        for line in str(content or "").splitlines():
            stripped = line.strip()
            if _ERROR_LINE_RE.search(stripped):
                current_block.append(stripped)
            elif current_block:
                blocks.append("\n".join(current_block))
                current_block = []
                if len(blocks) >= max_blocks:
                    break
        if current_block and len(blocks) < max_blocks:
            blocks.append("\n".join(current_block))
        return blocks

    # ------------------------------------------------------------------
    # Issue payload
    # ------------------------------------------------------------------

    def _issue_payload(self, issue: Any, run_id: str, journals: List[Dict[str, Any]], attachment_records: List[Dict[str, Any]], failures: List[Dict[str, Any]]) -> Dict[str, Any]:
        fixed_version = _obj_name(getattr(issue, "fixed_version", None))
        return {
            "issue_id": int(getattr(issue, "id")),
            "run_id": run_id,
            "subject": str(getattr(issue, "subject") or ""),
            "status_name": _obj_name(getattr(issue, "status", None)),
            "priority_name": _obj_name(getattr(issue, "priority", None)),
            "project_name": _obj_name(getattr(issue, "project", None)),
            "tracker_name": _obj_name(getattr(issue, "tracker", None)),
            "author_name": _obj_name(getattr(issue, "author", None)),
            "assigned_to_name": _obj_name(getattr(issue, "assigned_to", None)),
            "created_on": _iso(getattr(issue, "created_on")),
            "updated_on": _iso(getattr(issue, "updated_on")),
            "description": str(getattr(issue, "description") or ""),
            "fixed_version": fixed_version,
            "component": self._extract_custom_field(issue, "Component_fae"),
            "soc_platform": self._parse_soc_platform(
                str(getattr(issue, "subject") or ""),
                str(getattr(issue, "description") or ""),
                fixed_version,
            ),
            "android_version": self._parse_android_version(
                str(getattr(issue, "subject") or ""),
                str(getattr(issue, "description") or ""),
                fixed_version,
            ),
            "start_date": _iso(getattr(issue, "start_date")),
            "due_date": _iso(getattr(issue, "due_date")),
            "closed_on": _iso(getattr(issue, "closed_on")),
            "done_ratio": int(getattr(issue, "done_ratio", 0) or 0),
            "journals_json": journals,
            "attachments_json": attachment_records,
            "failures_json": failures[:50],
        }

    def _extract_journals(self, journals: List[Any]) -> List[Dict[str, Any]]:
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
                "created_on": _iso(getattr(item, "created_on", "")),
                "notes": _truncate(str(getattr(item, "notes", "")), 2000),
                "details": details,
            })
        return result

    # ------------------------------------------------------------------
    # Similar reference finding with multi-dimension similarity scoring
    # ------------------------------------------------------------------

    async def _find_similar_references(self, client: RedmineClient, issue_payload: Dict[str, Any], failures: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        terms = self._similar_terms(issue_payload, failures)
        candidates: Dict[int, Dict[str, Any]] = {}

        # 1. Local DB FTS search
        for term in terms[:8]:
            for row in self.db.search_similar(term, int(issue_payload["issue_id"]), limit=8):
                ref_id = int(row["issue_id"])
                score, reason, details = self._score_reference(issue_payload, failures, row)
                if score <= 0:
                    continue
                current = candidates.get(ref_id)
                if not current or score > current["score"]:
                    candidates[ref_id] = {
                        "issue_id": ref_id,
                        "subject": row.get("subject") or "",
                        "score": round(score, 2),
                        "reason": reason,
                        "match_details": details,
                        "source": "local_db",
                    }

        # 2. Redmine subject search
        for term in self._redmine_search_terms(issue_payload, failures):
            try:
                rows = await client.search_issues_by_subject(term, project_id="fae", limit=10, status_id="*")
            except Exception as exc:
                logger.warning("[RedmineAgent] Redmine subject search failed for %s: %s", term, exc)
                continue
            for row in rows:
                ref_id = int(row.get("issue_id") or 0)
                if not ref_id or ref_id == int(issue_payload["issue_id"]):
                    continue
                search_row = {**row, "source": "redmine_subject", "matched_term": term}
                score, reason, details = self._score_reference(issue_payload, failures, search_row)
                score += 25  # bonus for Redmine direct match
                reason = "；".join(dict.fromkeys([reason, f"Redmine主题搜索命中 {term}"]))[:500]
                current = candidates.get(ref_id)
                if not current or score > current["score"]:
                    candidates[ref_id] = {
                        "issue_id": ref_id,
                        "subject": row.get("subject") or "",
                        "score": round(score, 2),
                        "reason": reason,
                        "match_details": details,
                        "source": "redmine_subject",
                        "updated_on": row.get("updated_on") or "",
                    }

        # 3. AI semantic similarity for top candidates (optional, limited to save API calls)
        top_candidates = sorted(candidates.values(), key=lambda item: item["score"], reverse=True)[:TOP_CANDIDATES_FOR_AI]
        if top_candidates and failures:
            semantic_scores = await self._ai_semantic_similarity(issue_payload, top_candidates)
            for ref in top_candidates:
                ref_id = ref["issue_id"]
                ai_score = semantic_scores.get(ref_id, 0)
                if ai_score > 0:
                    old_score = candidates[ref_id]["score"]
                    # Blend: weighted average of rule-based (80%) + AI semantic (20%)
                    blended = old_score * 0.8 + ai_score * 20  # ai_score 0-1 → 0-20
                    candidates[ref_id]["score"] = round(blended, 2)
                    candidates[ref_id]["match_details"]["ai_semantic_score"] = round(ai_score, 3)
                    candidates[ref_id]["reason"] = (candidates[ref_id].get("reason") or "") + f" | AI语义相似 {ai_score:.2f}"

        # 4. Assign similarity levels
        for ref in candidates.values():
            s = ref["score"]
            if s >= SIMILARITY_THRESHOLD_HIGH:
                ref["similarity_level"] = "high"
            elif s >= SIMILARITY_THRESHOLD_MEDIUM:
                ref["similarity_level"] = "medium"
            else:
                ref["similarity_level"] = "low"
            # Filter out low similarity
        return sorted(
            [ref for ref in candidates.values() if ref["similarity_level"] != "low"],
            key=lambda item: item["score"],
            reverse=True,
        )[:MAX_REFERENCES]

    def _similar_terms(self, issue_payload: Dict[str, Any], failures: List[Dict[str, Any]]) -> List[str]:
        terms = [issue_payload.get("subject", "")]
        for failure in failures[:5]:
            for key in ("module", "name"):
                if failure.get(key):
                    terms.append(str(failure[key]))
            reason = str(failure.get("reason") or "")
            tokens = re.findall(r"[A-Za-z0-9_.#$-]{8,}", reason)
            if tokens:
                terms.append(" ".join(tokens[:5]))
        desc_tokens = re.findall(r"[A-Za-z0-9_.#$-]{8,}", issue_payload.get("description") or "")
        if desc_tokens:
            terms.append(" ".join(desc_tokens[:8]))
        return [term for term in terms if term.strip()]

    def _redmine_search_terms(self, issue_payload: Dict[str, Any], failures: List[Dict[str, Any]]) -> List[str]:
        terms: List[str] = []
        source_text = " ".join([
            issue_payload.get("subject") or "",
            issue_payload.get("description") or "",
            " ".join(str(failure.get(key) or "") for failure in failures[:5] for key in ("module", "name", "reason")),
        ])
        patterns = [
            r"\bCts[A-Za-z0-9_]+TestCases\b",
            r"\b[A-Za-z0-9_]*HostTest\b",
            r"\b[A-Za-z0-9_]*Test\b",
            r"\b[A-Za-z0-9_]*ManagedProfile[A-Za-z0-9_]*\b",
            r"\bconfig_[A-Za-z0-9_]+\b",
        ]
        for pattern in patterns:
            for match in re.findall(pattern, source_text):
                if len(match) >= 8:
                    terms.append(match)
        for failure in failures[:5]:
            name = str(failure.get("name") or "")
            if "." in name:
                parts = [part for part in re.split(r"[.#]", name) if len(part) >= 8]
                terms.extend(parts[-3:])
            module = str(failure.get("module") or "")
            if module:
                terms.append(module)
        deduped = []
        seen = set()
        for term in terms:
            term = term.strip("._- ")
            if not term or term.lower() in seen:
                continue
            if term.islower() and "_" not in term:
                continue
            seen.add(term.lower())
            deduped.append(term)
        return deduped[:10]

    def _score_reference(self, issue_payload: Dict[str, Any], failures: List[Dict[str, Any]], row: Dict[str, Any]) -> tuple:
        """Multi-dimension similarity scoring (total 100).

        Returns (score, reason, match_details).
        """
        score = 0.0
        reasons = []
        details: Dict[str, Any] = {}
        row_text = " ".join(str(row.get(key) or "") for key in ("subject", "description", "summary", "doc_content"))

        # Dimension 1: Same test case name (0-30)
        test_case_score = 0.0
        for failure in failures[:5]:
            name = failure.get("name") or ""
            if name and name in row_text:
                test_case_score = 30
                reasons.append(f"同失败用例 {name[:60]}")
                break
        score += test_case_score
        details["same_test_case"] = test_case_score > 0

        # Dimension 2: Same module (0-20)
        module_score = 0.0
        for failure in failures[:5]:
            module = failure.get("module") or ""
            if module and module in row_text:
                module_score = 20
                reasons.append(f"同模块 {module[:60]}")
                break
        score += module_score
        details["same_module"] = module_score > 0

        # Dimension 3: Error keyword overlap (0-15)
        keyword_score = 0.0
        matched_keywords = []
        for failure in failures[:5]:
            for token in re.findall(r"[A-Za-z0-9_.#$-]{12,}", failure.get("reason") or "")[:5]:
                if token in row_text:
                    keyword_score += 3
                    matched_keywords.append(token[:40])
        keyword_score = min(keyword_score, 15)
        score += keyword_score
        details["keyword_overlap"] = round(keyword_score / 15, 2) if keyword_score > 0 else 0
        if matched_keywords:
            reasons.append(f"错误关键词 {', '.join(matched_keywords[:3])}")

        # Dimension 4: Title keyword Jaccard similarity (0-15)
        subject_words = set(re.findall(r"[A-Za-z0-9_一-鿿]{2,}", issue_payload.get("subject") or ""))
        ref_words = set(re.findall(r"[A-Za-z0-9_一-鿿]{2,}", row.get("subject") or ""))
        if subject_words and ref_words:
            intersection = subject_words & ref_words
            union = subject_words | ref_words
            jaccard = len(intersection) / len(union) if union else 0
            title_score = round(jaccard * 15, 1)
        else:
            common = subject_words & ref_words
            title_score = min(15, len(common) * 4)
        score += title_score
        details["title_similarity"] = round(title_score / 15, 2) if title_score > 0 else 0
        if title_score > 0:
            reasons.append("标题关键词相似")

        return score, "；".join(dict.fromkeys(reasons))[:500], details

    async def _ai_semantic_similarity(self, issue_payload: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Dict[int, float]:
        """Ask the local model to score semantic similarity between the issue and each candidate.

        Returns {issue_id: score_0_to_1}.
        """
        config = self._load_ai_config()
        analyzer = UniversalAIAnalyzer(config)
        provider_name = analyzer.get_primary_provider()
        if not provider_name:
            return {}
        provider = config.get("providers", {}).get(provider_name, {})
        if not provider.get("base_url") or not provider.get("model"):
            return {}

        issue_desc = _truncate(issue_payload.get("description") or issue_payload.get("subject") or "", 500)
        issue_failures = _truncate(
            "\n".join(str(f.get("name", "")) + ": " + str(f.get("reason", ""))[:200] for f in (issue_payload.get("failures_json") or [])[:3]),
            500,
        )

        results: Dict[int, float] = {}
        # Batch candidates into a single prompt to reduce API calls
        candidate_descriptions = []
        for i, c in enumerate(candidates[:5]):
            candidate_descriptions.append(f"[{i}] #{c['issue_id']} {c.get('subject', '')}")

        prompt = f"""评估以下 Redmine 问题之间的相似度（0-1分）。

当前问题: #{issue_payload.get('issue_id')} {issue_payload.get('subject')}
描述摘要: {issue_desc}
关键失败: {issue_failures}

候选参考单:
{chr(10).join(candidate_descriptions)}

请返回纯JSON（不要markdown标记）:
{{"scores": [{",".join(f'{{"id": {c["issue_id"]}, "score": 0.XX}}' for c in candidates[:5])}]}}

评分标准:
- 0.8-1.0: 同模块同失败原因
- 0.5-0.7: 同模块不同失败或相似问题
- 0.2-0.4: 同大类问题但不同模块
- 0.0-0.1: 基本不相关
"""

        try:
            resp_text = await asyncio.to_thread(self._call_model_raw, analyzer, provider_name, provider, prompt)
            match = re.search(r'\{.*\}', resp_text, re.S)
            if match:
                parsed = json.loads(match.group(0))
                for item in parsed.get("scores", []):
                    results[int(item.get("id", 0))] = min(1.0, max(0.0, float(item.get("score", 0))))
        except Exception as exc:
            logger.warning("[RedmineAgent] AI semantic similarity failed: %s", exc)

        return results

    # ------------------------------------------------------------------
    # AI model interaction
    # ------------------------------------------------------------------

    async def _summarize_with_model(self, issue_payload: Dict[str, Any], failures: List[Dict[str, Any]], references: List[Dict[str, Any]]) -> Dict[str, Any]:
        config = self._load_ai_config()
        analyzer = UniversalAIAnalyzer(config)
        provider_name = analyzer.get_primary_provider()
        if not provider_name:
            return {"success": False, "error": "AI model not configured"}
        provider = config.get("providers", {}).get(provider_name, {})
        prompt = self._build_ai_prompt(issue_payload, failures, references)
        try:
            return await asyncio.to_thread(self._call_model, analyzer, provider_name, provider, prompt)
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def _load_ai_config(self) -> Dict[str, Any]:
        if self._ai_config_cache is not None:
            return self._ai_config_cache

        config = config_manager.load_config().get("ai_models", {}) or {}
        env_base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
        env_model = os.getenv("ANTHROPIC_MODEL", "").strip()
        env_token = os.getenv("ANTHROPIC_AUTH_TOKEN", "").strip()
        if not (env_base_url or env_model or env_token):
            self._ai_config_cache = config
            return config

        provider = dict((config.get("providers") or {}).get(config.get("primary_provider") or "", {}))
        provider.update({
            "name": "GLM-5.1 Local",
            "enabled": True,
            "api_format": "anthropic",
        })
        if env_base_url:
            provider["base_url"] = env_base_url
        if env_model:
            provider["model"] = env_model
        if env_token:
            provider["api_key"] = env_token

        result = {
            **config,
            "enabled": True,
            "primary_provider": "env_anthropic",
            "providers": {"env_anthropic": provider},
        }
        self._ai_config_cache = result
        return result

    def _call_model(self, analyzer: UniversalAIAnalyzer, provider_name: str, provider: Dict[str, Any], prompt: str) -> Dict[str, Any]:
        """Call the AI model and parse the structured seven-field JSON response."""
        raw = self._call_model_raw(analyzer, provider_name, provider, prompt)
        try:
            match = re.search(r"\{.*\}", raw, re.S)
            result = json.loads(match.group(0) if match else raw)
            if isinstance(result, str):
                result = json.loads(result)
            # Handle nested summary in old format
            if isinstance(result.get("summary"), str) and result["summary"].lstrip().startswith("{"):
                nested = json.loads(result["summary"])
                nested["success"] = True
                nested["provider"] = provider_name
                return nested
            result["success"] = True
            result["provider"] = provider_name
            return result
        except Exception:
            return {"success": False, "provider": provider_name, "summary": raw[:1200], "reply_draft": ""}

    def _call_model_raw(self, analyzer: UniversalAIAnalyzer, provider_name: str, provider: Dict[str, Any], prompt: str) -> str:
        """Call the AI model and return raw text response.

        Reuses UniversalAIAnalyzer's HTTP request logic to avoid duplicating
        endpoint construction, header setup, and response parsing.
        """
        api_key = provider.get("api_key") or ""
        base_url = provider.get("base_url") or ""
        model = provider.get("model") or ""
        if not base_url or not model:
            return ""
        api_format = analyzer._get_api_format(provider_name, provider)

        if api_format == analyzer.API_FORMAT_ANTHROPIC:
            url = f"{base_url}/v1/messages" if not base_url.endswith("/messages") else base_url
            headers = {"x-api-key": api_key, "Content-Type": "application/json", "anthropic-version": "2023-06-01"}
            data = {"model": model, "max_tokens": AI_MODEL_MAX_TOKENS, "messages": [{"role": "user", "content": prompt}]}
        else:
            url = f"{base_url}/v1/chat/completions" if not base_url.endswith(("/chat/completions", "/completions")) else base_url
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            data = {"model": model, "temperature": 0.2, "max_tokens": AI_MODEL_MAX_TOKENS, "messages": [{"role": "user", "content": prompt}]}

        try:
            resp = requests.post(url, headers=headers, json=data, timeout=AI_MODEL_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            logger.error("[RedmineAgent] model request failed: %s", exc)
            return ""
        if resp.status_code != 200:
            logger.error("[RedmineAgent] model HTTP %s: %s", resp.status_code, resp.text[:300])
            return ""

        # Delegate response parsing to the analyzer's robust parser
        return analyzer._parse_response_raw(resp.json(), api_format)

    # ------------------------------------------------------------------
    # AI Prompt — structured seven-field output
    # ------------------------------------------------------------------

    def _build_ai_prompt(self, issue_payload: Dict[str, Any], failures: List[Dict[str, Any]], references: List[Dict[str, Any]]) -> str:
        journals = issue_payload.get("journals_json") or []
        journals_text = "\n".join(
            f"[{j.get('created_on', '')}] {j.get('user', '')}: {j.get('notes', '')}"
            for j in journals[-5:]
        )[:2000]

        ref_text = "\n".join(
            f"- #{r.get('issue_id')} ({r.get('similarity_level', '')} 相似度{r.get('score', 0)}) {r.get('subject', '')} | {r.get('reason', '')}"
            for r in (references or [])[:5]
        )

        return f"""你是 Rockchip FAE 团队的 Android GMS/CTS/VTS/GTS 问题分析专家。
请分析以下 Redmine 问题并返回结构化 JSON。

## 问题信息
Redmine: #{issue_payload.get('issue_id')} {issue_payload.get('subject')}
描述:
{_truncate(issue_payload.get('description') or '', 3000)}

## 附件报告失败项
{json.dumps(failures[:10], ensure_ascii=False)[:8000]}

## 历史参考单
{ref_text or '暂无'}

## 历史沟通记录
{journals_text or '暂无'}

---

请严格按以下JSON格式返回（不要markdown标记，不要解释文字，直接返回JSON）：

{{
  "title": "简洁中文标题（含平台、Android版本、模块名、失败用例）",
  "problem_description": "问题现象的中文描述：客户报告了什么、什么场景下触发、什么设备/平台上",
  "error_info": "提取的核心报错信息，保留原始英文。长报错堆栈用```代码块包裹，包含异常类型、关键错误消息、堆栈中的失败位置",
  "error_analysis": "根因分析：为什么会触发此错误，底层机制是什么，与什么配置或代码相关",
  "solution": "具体的解决步骤（编号列表），包含文件路径和验证命令。shell命令用```shell代码块包裹（命令行$前缀），配置修改用```xml或```diff代码块",
  "patch_direction": "补丁方向。如涉及文件修改，必须用unified diff格式包裹在```diff代码块中，示例:\\n```diff\\n--- a/path/to/file\\n+++ b/path/to/file\\n@@ -1,4 +1,4 @@\\n-旧内容\\n+新内容\\n```\\n如涉及shell命令，用```shell代码块（$前缀）。XML配置用```xml代码块",
  "reference_redmine": [
    {{"issue_id": 12345, "reason": "同模块同失败原因，已解决"}}
  ]
}}

分析要点：
1. 从失败项的 module 和 name 中识别测试模块和用例
2. 从 reason/stack_trace 中提取实际的异常类型和触发点
3. 结合问题描述中客户提到的文件路径（如 config_user_types.xml）分析
4. 从参考单中找出同模块或同失败模式的已解决问题
5. patch_direction 要给出具体的文件路径和修改内容，不要笼统说"修改配置"
6. reference_redmine 从上面的历史参考单中选择确实相关的，并说明为什么相关
7. 代码块格式：diff内容用```diff（必须有--- a/file, +++ b/file, @@...@@, -/+行），shell命令用```shell（$前缀），XML配置用```xml，纯报错堆栈用```
"""

    # ------------------------------------------------------------------
    # Code block formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_code_block(text: str, lang: str = "") -> str:
        """Wrap text in a markdown code block if not already wrapped."""
        text = str(text or "").strip()
        if not text or text.startswith("```"):
            return text
        return f"```{lang}\n{text}\n```"

    @staticmethod
    def _wrap_patch_direction(text: str) -> str:
        """Intelligently wrap patch_direction in the appropriate code block."""
        text = str(text or "").strip()
        if not text or text.startswith("```"):
            return text
        # Detect unified diff patterns
        if re.search(r"^---\s+[ab]/", text, re.M) or re.search(r"^\+\+\+\s+[ab]/", text, re.M):
            return f"```diff\n{text}\n```"
        # Detect shell command patterns ($ prefix)
        if re.search(r"^\$\s+", text, re.M):
            return f"```shell\n{text}\n```"
        # Detect XML content
        if re.search(r"<\?xml|<[\w:-]+\s+[^>]*>", text):
            return f"```xml\n{text}\n```"
        # Default: wrap as generic code
        return f"```\n{text}\n```"

    @staticmethod
    def _html_code_block(text: str, lang: str = "") -> str:
        """Wrap text in <pre><code class="lang"> HTML block for Redmine display."""
        text = str(text or "").strip()
        if not text:
            return ""
        cls = f' class="{lang}"' if lang else ""
        return f"<pre><code{cls}>\n{text}\n</code></pre>"

    @staticmethod
    def _detect_code_lang(text: str) -> str:
        """Detect code language from content patterns."""
        text = str(text or "")
        if re.search(r"^---\s+[ab]/", text, re.M) or re.search(r"^\+\+\+\s+[ab]/", text, re.M):
            return "diff"
        if re.search(r"^\$\s+", text, re.M):
            return "shell"
        if re.search(r"<\?xml|<[\w:-]+\s+[^>]*>", text):
            return "xml"
        return ""

    @staticmethod
    def _extract_patch_from_journals(journals: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        """Extract existing <pre><code class="diff"> patches from journal notes."""
        patches = []
        for journal in journals:
            notes = str(journal.get("notes") or "")
            user = journal.get("user") or ""
            # Extract <pre><code class="diff">...</code></pre> blocks
            diff_blocks = re.findall(
                r'<pre><code\s+class="diff">\s*(.*?)\s*</code></pre>',
                notes,
                re.S,
            )
            for block in diff_blocks:
                patches.append({"user": user, "patch": block.strip()})
            # Also extract bare <pre>...</pre> blocks that look like diffs
            if not diff_blocks:
                pre_blocks = re.findall(r"<pre>\s*(.*?)\s*</pre>", notes, re.S)
                for block in pre_blocks:
                    if re.search(r"^diff\s|--\s+a/|\+\+\+\s+b/", block, re.M):
                        patches.append({"user": user, "patch": block.strip()})
        return patches

    @staticmethod
    def _detect_confirmed_in_journals(journals: List[Dict[str, Any]]) -> Optional[str]:
        """Detect if the issue was confirmed resolved in journal comments."""
        confirm_patterns = ["测试ok", "测试通过", "验证ok", "验证通过", "已解决", "问题已解决", "可以关闭"]
        for journal in reversed(journals):
            notes = str(journal.get("notes") or "").lower()
            user = journal.get("user") or ""
            for pattern in confirm_patterns:
                if pattern in notes:
                    return f"{user}: {str(journal.get('notes', ''))[:100]}"
        return None

    @staticmethod
    def _analyze_resolution_from_journals(journals: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Analyze journal history to determine the correct resolution for a closed issue.

        Returns a structured resolution summary:
        {
            "has_resolution": bool,
            "status": "verified" | "resolved" | "unclear",
            "provider": str,   # who provided the solution
            "provider_time": str,
            "confirmer": str,  # who confirmed the solution
            "confirmer_time": str,
            "confirm_note": str,
            "patches": [...],  # extracted diff/patch blocks
            "solution_text": str,  # plain text solution from provider
            "summary": str,    # one-line resolution summary
        }
        """
        patches = RedmineAgent._extract_patch_from_journals(journals)
        confirmed = RedmineAgent._detect_confirmed_in_journals(journals)

        # Walk journals forward to find: provider (has code/diff) -> confirmer (says "ok")
        provider = ""
        provider_time = ""
        provider_notes = ""
        confirmer = ""
        confirmer_time = ""
        confirm_note = ""

        # Patterns indicating someone is providing a solution
        solution_indicators = ["<pre>", "<code", "diff --git", "git diff", "patch", "修改方法", "解决方案", "修改如下", "改法"]
        confirm_indicators = ["测试ok", "测试通过", "验证ok", "验证通过", "已解决", "可以关闭", "没问题了"]

        for journal in journals:
            notes = str(journal.get("notes") or "")
            user = journal.get("user") or ""
            created = journal.get("created_on") or ""
            notes_lower = notes.lower()

            # Detect if this journal is providing a solution
            is_solution = False
            if any(ind in notes_lower for ind in solution_indicators):
                is_solution = True
            if any(ind in notes for ind in ["<pre><code", "diff --git"]):
                is_solution = True

            if is_solution and not provider:
                provider = user
                provider_time = created
                # Extract plain text before the first <pre> as solution description
                pre_idx = notes.find("<pre>")
                provider_notes = notes[:pre_idx].strip() if pre_idx > 0 else notes[:500].strip()

            # Detect if this journal confirms the solution
            is_confirm = any(ind in notes_lower for ind in confirm_indicators)
            if is_confirm and not confirmer and provider and user != provider:
                confirmer = user
                confirmer_time = created
                confirm_note = notes.strip()

        # Determine resolution status
        has_resolution = bool(patches) or bool(provider)
        if has_resolution and confirmed:
            status = "verified"
        elif has_resolution:
            status = "resolved"
        else:
            status = "unclear"

        # Build summary
        summary = ""
        if status == "verified":
            summary = f"✅ 已验证: {provider} 提供方案，{confirmer} 确认通过 ({confirm_note[:50]})"
        elif status == "resolved":
            summary = f"✓ 已解决: {provider} 提供方案（未经客户确认）"
        else:
            summary = "⚠ 未找到明确的解决方案"

        return {
            "has_resolution": has_resolution,
            "status": status,
            "provider": provider,
            "provider_time": provider_time,
            "confirmer": confirmer,
            "confirmer_time": confirmer_time,
            "confirm_note": confirm_note,
            "patches": patches,
            "solution_text": provider_notes,
            "summary": summary,
        }

    @staticmethod
    def _detect_version_type(fixed_version: str) -> str:
        """Detect version type (GMS/SDK) from fixed_version string."""
        text = str(fixed_version or "").upper()
        if "GMS" in text:
            return "GMS"
        if "SDK" in text or "SSI" in text:
            return "SDK"
        return "-"

    # ------------------------------------------------------------------
    # Structured field extraction
    # ------------------------------------------------------------------

    def _extract_structured_fields(
        self,
        ai_result: Dict[str, Any],
        issue_payload: Dict[str, Any],
        failures: List[Dict[str, Any]],
        references: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Map AI output + rule-based extraction to the seven display fields."""
        subject = issue_payload.get("subject") or ""
        journals = issue_payload.get("journals_json") or []
        is_resolved = bool(issue_payload.get("is_resolved"))

        # Analyze resolution from journals (for closed/resolved issues)
        resolution = self._analyze_resolution_from_journals(journals)

        # 1. title
        title = ai_result.get("title") or subject

        # 2. problem_description
        problem_description = ai_result.get("problem_description") or ""
        if not problem_description:
            problem_description = _truncate(issue_payload.get("description") or subject, 500)

        # 3. error_info
        error_info = ai_result.get("error_info") or ""
        if not error_info and failures:
            error_info = self.extract_error_from_failures(failures)
        if error_info and not str(error_info).lstrip().startswith("```"):
            error_info = self._ensure_code_block(error_info, "")

        # 4. error_analysis
        error_analysis = ai_result.get("error_analysis") or ai_result.get("root_cause_guess") or ""
        if not error_analysis:
            error_analysis = self._rule_error_analysis(issue_payload, failures, references)

        # 5. solution
        # Priority: AI result > journal resolution > rule-based
        solution = ai_result.get("solution") or ""
        if not solution:
            if resolution["has_resolution"]:
                parts = [resolution["summary"]]
                if resolution.get("solution_text"):
                    parts.append(f"方案说明: {resolution['solution_text']}")
                solution = "\n".join(parts)
            else:
                solution = self._rule_solution(issue_payload, failures, references)

        # 6. patch_direction
        # Priority: AI result > journal patches > rule-based
        patch_direction = ai_result.get("patch_direction") or ai_result.get("risk") or ""
        if not patch_direction:
            if resolution["patches"]:
                patch_parts = []
                for jp in resolution["patches"]:
                    patch_parts.append(f'<pre><code class="diff">{jp["patch"]}</code></pre>')
                patch_direction = "\n\n".join(patch_parts)
            else:
                patch_direction = self._rule_patch_direction(issue_payload, failures, references)
        else:
            # AI returned patch_direction — but for verified closed issues, prefer journal patches
            if resolution["status"] == "verified" and resolution["patches"]:
                # Verified resolution from journals takes priority over AI-generated patch
                patch_parts = []
                for jp in resolution["patches"]:
                    patch_parts.append(f'<pre><code class="diff">{jp["patch"]}</code></pre>')
                patch_direction = "\n\n".join(patch_parts)
            else:
                patch_direction = self._markdown_to_html_code_blocks(
                    self._wrap_patch_direction(patch_direction)
                )

        # 7. reference_redmine (formatted)
        ai_refs = ai_result.get("reference_redmine") or []
        if not ai_refs:
            reference_redmine = "; ".join(f"#{r.get('issue_id')}" for r in references[:3]) if references else ""
        else:
            reference_redmine = "; ".join(f"#{r.get('issue_id')}({r.get('reason', '')[:30]})" for r in ai_refs)

        # summary and reply_draft (keep for backward compat)
        summary = ai_result.get("summary") or self._rule_summary(issue_payload, failures, references)
        reply_draft = ai_result.get("reply_draft") or self._reply_draft(issue_payload, failures, references, solution, patch_direction)

        return {
            "title": title,
            "problem_description": problem_description,
            "error_info": error_info,
            "error_analysis": error_analysis,
            "solution": solution,
            "patch_direction": patch_direction,
            "reference_redmine": reference_redmine,
            "summary": summary,
            "reply_draft": reply_draft,
            "resolution_json": resolution if resolution["has_resolution"] else None,
            "references_json": references,
            "ai_json": ai_result,
        }

    def _rule_summary(self, issue_payload: Dict[str, Any], failures: List[Dict[str, Any]], references: List[Dict[str, Any]]) -> str:
        if failures:
            first = failures[0]
            return f"{first.get('module') or '未知模块'} / {first.get('name') or '未知用例'} 失败：{_truncate(first.get('reason') or '', 180)}"
        return _truncate(issue_payload.get("description") or issue_payload.get("subject") or "未提取到描述", 240)

    def _rule_solution(self, issue_payload: Dict[str, Any], failures: List[Dict[str, Any]], references: List[Dict[str, Any]]) -> str:
        lines = []
        if failures:
            first = failures[0]
            lines.append(f"1. 失败模块: {first.get('module') or '-'}")
            lines.append(f"2. 失败用例: {first.get('name') or '-'}")
        lines.append("3. 待进一步分析确认解决方案。")
        if references:
            ref_ids = ", ".join(f"#{r.get('issue_id')}" for r in references[:3])
            lines.append(f"4. 可参考历史单: {ref_ids}")
        return "\n".join(lines)

    def _rule_error_analysis(self, issue_payload: Dict[str, Any], failures: List[Dict[str, Any]], references: List[Dict[str, Any]]) -> str:
        """Rule-based error analysis when AI is unavailable."""
        parts = []
        if failures:
            first = failures[0]
            module = first.get("module") or ""
            name = first.get("name") or ""
            reason = str(first.get("reason") or "")
            parts.append(f"失败模块: {module}")
            parts.append(f"失败用例: {name}")
            # Extract key error type from reason
            error_type_match = re.search(r"(\w+(?:Exception|Error))", reason)
            if error_type_match:
                parts.append(f"异常类型: {error_type_match.group(1)}")
            # Extract key error message (first line of reason)
            first_reason_line = reason.split("\n")[0].strip()[:200] if reason else ""
            if first_reason_line:
                parts.append(f"关键报错: {first_reason_line}")
        # Check references for similar resolved issues
        if references:
            high_refs = [r for r in references if r.get("similarity_level") == "high"]
            if high_refs:
                ref_ids = ", ".join(f"#{r.get('issue_id')}" for r in high_refs[:3])
                parts.append(f"高度相似的历史单: {ref_ids}（可参考其解决方案）")
        if not parts:
            parts.append("暂无分析结果")
        return "\n".join(parts)

    def _rule_patch_direction(self, issue_payload: Dict[str, Any], failures: List[Dict[str, Any]], references: List[Dict[str, Any]]) -> str:
        """Rule-based patch direction when AI is unavailable."""
        desc = str(issue_payload.get("description") or "")
        parts = []
        # Extract file paths mentioned in description
        file_paths = re.findall(r"[\./]?[\w/]+(?:config_user_types|config\.xml|\.xml|\.java|\.kt|\.prop|\.mk|\.cfg)[\w/.-]*", desc)
        if file_paths:
            unique_paths = list(dict.fromkeys(file_paths))[:5]
            parts.append("涉及文件:")
            for fp in unique_paths:
                parts.append(f"  - {fp}")
        # Suggest checking references
        if references:
            ref_ids = ", ".join(f"#{r.get('issue_id')}" for r in references[:3])
            parts.append(f"建议参考历史单 {ref_ids} 中的补丁方案")
        if not parts:
            parts.append("需要进一步分析具体日志和源码")
        return "\n".join(parts)

    def _reply_draft(self, issue_payload: Dict[str, Any], failures: List[Dict[str, Any]], references: List[Dict[str, Any]], solution: str = "", patch_direction: str = "") -> str:
        lines = [
            "Hi，问题已收到，初步分析如下：",
            "",
            f"- Redmine: #{issue_payload.get('issue_id')} {issue_payload.get('subject')}",
        ]
        if failures:
            first = failures[0]
            reason_text = _truncate(first.get("reason") or "", 300)
            reason_lang = self._detect_code_lang(reason_text)
            lines.extend([
                f"- 失败模块: {first.get('module') or '-'}",
                f"- 失败用例: {first.get('name') or '-'}",
                f"- 关键报错: {self._html_code_block(reason_text, reason_lang)}",
            ])
        if references:
            lines.append("- 可参考历史单: " + ", ".join(f"#{ref['issue_id']}" for ref in references[:3]))

        # Add solution
        if solution:
            # Convert any markdown code blocks in solution to HTML
            solution_html = self._markdown_to_html_code_blocks(solution)
            lines.extend(["", "解决方法:", solution_html])

        # Add patch direction
        if patch_direction and patch_direction != "需要进一步分析具体日志和源码":
            patch_html = self._markdown_to_html_code_blocks(patch_direction)
            lines.extend(["", "补丁方向:", patch_html])

        lines.extend(["", "我会继续结合日志和历史修改记录确认根因，并同步后续处理结论。"])
        return "\n".join(lines)

    @staticmethod
    def _markdown_to_html_code_blocks(text: str) -> str:
        """Convert markdown ```lang``` code blocks to <pre><code class="lang"> HTML blocks."""
        def _replace(match):
            lang = match.group(1) or ""
            code = match.group(2) or ""
            cls = f' class="{lang}"' if lang else ""
            return f"<pre><code{cls}>\n{code}</code></pre>"
        return re.sub(r"```(\w*)\n([\s\S]*?)```", _replace, str(text or ""))

    # ------------------------------------------------------------------
    # Document generation
    # ------------------------------------------------------------------

    def _build_issue_document(self, item: Dict[str, Any]) -> str:
        failures = item.get("failures_json") or []
        references = item.get("references_json") or []
        attachments = item.get("attachments_json") or []
        journals = item.get("journals_json") or []

        lines = [
            f"# Redmine #{item.get('issue_id')} - {item.get('subject')}",
            "",
            "## 基本信息",
            f"- 状态: {item.get('status_name') or '-'}",
            f"- 优先级: {item.get('priority_name') or '-'}",
            f"- 项目: {item.get('project_name') or '-'}",
            f"- 分类: {item.get('category') or '-'}",
            f"- 指派: {item.get('assigned_to_name') or '-'}",
            f"- 创建: {item.get('created_on') or '-'}",
            f"- 更新: {item.get('updated_on') or '-'}",
            "",
            "## 平台与版本",
            "| SoC | Android | 类型 | Component | 指派 | 创建 |",
            "|-----|---------|------|-----------|------|------|",
            f"| {item.get('soc_platform') or '-'} | {item.get('android_version') or '-'} | {self._detect_version_type(item.get('fixed_version') or '')} | {item.get('component') or '-'} | {item.get('assigned_to_name') or '-'} | {(item.get('created_on') or '-')[:10]} |",
            "",
            "## 问题描述",
            item.get("problem_description") or _truncate(item.get("description") or "", 4000) or "-",
            "",
            "## 报错信息",
            self._ensure_code_block(item.get("error_info") or "-", ""),
            "",
            "## 报错分析",
            item.get("error_analysis") or "-",
            "",
        ]

        # Add resolution assessment section for resolved/closed issues
        resolution_json = item.get("resolution_json")
        if resolution_json and resolution_json.get("has_resolution"):
            status_icon = {"verified": "✅", "resolved": "✓", "unclear": "⚠"}.get(resolution_json.get("status", ""), "")
            lines.extend([
                f"## {status_icon} 解决方案判定",
                f"- 判定结果: **{resolution_json.get('summary', '-')}**",
                f"- 方案提供者: {resolution_json.get('provider') or '-'} ({(resolution_json.get('provider_time') or '-')[:10]})",
            ])
            if resolution_json.get("confirmer"):
                lines.append(f"- 确认者: {resolution_json.get('confirmer')} ({(resolution_json.get('confirmer_time') or '-')[:10]}) — {resolution_json.get('confirm_note', '')[:80]}")
            lines.append("")

        lines.extend([
            "## 解决方法",
            item.get("solution") or "-",
            "",
            "## 解决补丁",
            item.get("patch_direction") or "-",
            "",
            "## 参考Redmine",
        ])

        if references:
            for ref in references:
                level = ref.get("similarity_level") or ""
                score = ref.get("score") or 0
                level_label = {"high": "🔴 高", "medium": "🟡 中"}.get(level, "⚪")
                lines.append(f"- {level_label} #{ref.get('issue_id')} (相似度: {score}) {ref.get('reason', '')} | {ref.get('subject', '')}")
        else:
            lines.append("- 暂无参考单")

        lines.extend(["", "## 附件分析"])
        if attachments:
            for att in attachments:
                analysis = att.get("analysis_json") or {}
                details = analysis.get("details") or {}
                attachment_id = att.get("attachment_id") or att.get("id") or "-"
                lines.append(
                    f"- {att.get('filename') or '-'} (id={attachment_id}, size={att.get('filesize') or 0}, status={att.get('status') or '-'})"
                )
                if details.get("type") == "image":
                    size_text = f"{details.get('width') or '-'}x{details.get('height') or '-'}"
                    lines.append(f"  - 截图信息: {size_text}, format={details.get('format') or '-'}, mode={details.get('mode') or '-'}")
                elif details.get("type"):
                    lines.append(f"  - 解析类型: {details.get('type')}")
                text_excerpt = analysis.get("text_excerpt") or ""
                if text_excerpt:
                    lines.append(f"  - 文本摘录: {_truncate(text_excerpt, 500)}")
                if att.get("error"):
                    lines.append(f"  - 错误: {att.get('error')}")
        else:
            lines.append("- 无附件")

        lines.extend(["", "## 报告失败项"])
        if failures:
            for idx, failure in enumerate(failures[:20], 1):
                lines.extend([
                    f"### {idx}. {failure.get('name') or 'Unknown'}",
                    f"- 模块: {failure.get('module') or '-'}",
                    f"- 原因:\n{self._ensure_code_block(_truncate(failure.get('reason') or '', 1200), '')}",
                    "",
                ])
        else:
            lines.append("- 未解析到报告失败项")

        lines.extend(["", "## 历史记录"])
        for journal in journals[-15:]:
            lines.extend([
                f"### {journal.get('created_on')} {journal.get('user')}",
                journal.get("notes") or "-",
                "",
            ])

        # Reply draft at the end
        lines.extend(["", "## 建议回复草稿", item.get("reply_draft") or "", ""])

        return "\n".join(lines).strip() + "\n"

    def _build_run_report(self, run_id: str, issues: List[Dict[str, Any]]) -> str:
        lines = [
            f"# RedmineAgent 日报 {run_id}",
            "",
            f"- 生成时间: {_now_iso()}",
            f"- 处理问题数: {len(issues)}",
            "",
            "## 概览",
        ]

        high_priority = [i for i in issues if "紧急" in (i.get("priority_name") or "") or "Urgent" in (i.get("priority_name") or "") or "高" in (i.get("priority_name") or "")]
        medium_priority = [i for i in issues if "正常" in (i.get("priority_name") or "") or "Normal" in (i.get("priority_name") or "")]

        lines.extend([
            f"- 高优先级: {len(high_priority)}",
            f"- 普通: {len(medium_priority)}",
            f"- 其他: {len(issues) - len(high_priority) - len(medium_priority)}",
            "",
        ])

        lines.append("## 新增问题")
        if not issues:
            lines.append("- 过去 24 小时没有扫描到 assigned_to 我的新增 Redmine 单")
        for item in issues:
            refs = item.get("references_json") or []
            ref_display = ", ".join(f'#{r.get("issue_id")}({r.get("similarity_level", "")})' for r in refs[:3]) if refs else "暂无"
            lines.extend([
                f"### #{item.get('issue_id')} {item.get('subject')}",
                f"- 优先级: {item.get('priority_name') or '-'}",
                f"- 分类: {item.get('category') or '-'}",
                f"- 报错信息:\n{self._ensure_code_block(_truncate(item.get('error_info') or '-', 200), '')}",
                f"- 报错分析: {_truncate(item.get('error_analysis') or '-', 200)}",
                f"- 解决方向: {_truncate(item.get('solution') or '-', 200)}",
                f"- 参考单: {ref_display}",
                "",
            ])

        if high_priority:
            lines.extend(["", "## 待关注（高优先级）"])
            for item in high_priority:
                lines.append(f"- #{item.get('issue_id')} {item.get('subject')} — {item.get('error_analysis') or '-'}")

        return "\n".join(lines).strip() + "\n"

    # ------------------------------------------------------------------
    # Shared helpers (used by router for field enrichment)
    # ------------------------------------------------------------------

    @staticmethod
    def extract_error_from_failures(failures: list) -> str:
        """Format failure items into a readable error summary."""
        if not failures:
            return ""
        parts = []
        for f in failures[:5]:
            name = f.get("name") or ""
            reason = f.get("reason") or ""
            module = f.get("module") or ""
            parts.append(f"[{module}] {name}: {reason[:200]}")
        return "\n".join(parts)

    @staticmethod
    def extract_description(issue: dict) -> str:
        """Extract a short description from an issue record."""
        desc = issue.get("description") or ""
        if desc:
            return desc[:500]
        return issue.get("subject") or ""
