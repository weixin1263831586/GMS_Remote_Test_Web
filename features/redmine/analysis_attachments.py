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
from collections.abc import Callable
from typing import Any, Dict, List, Optional

import requests

from features.redmine.config import config_manager
from features.redmine.client import RedmineAttachment, RedmineClient
from features.redmine.repository import (
    RESOLVED_STATUS_NAMES as RESOLVED_STATUSES,
    RedmineAgentDB,
)
from foundation.config import settings

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



class AttachmentAnalysisMixin:
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
        if self.report_analyzer_factory is None:
            return {
                "filename": os.path.basename(path),
                "failures": [],
                "summary": {},
                "details": {},
                "parsed": False,
                "error": "report analyzer is not configured",
            }
        analyzer = self.report_analyzer_factory(
            temp_dir=tempfile.mkdtemp(prefix="redmine_agent_report_")
        )
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
