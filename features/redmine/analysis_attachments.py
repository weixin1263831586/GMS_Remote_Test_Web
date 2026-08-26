"""RedmineAgent: nightly Redmine triage and report generation."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from features.redmine.cert_rules import detect_certification_errors
from features.redmine.models import RedmineAttachment
from features.redmine.tesseract_finder import (
    bundled_tesseract_cmd,
    configure_bundled_tesseract,
)
from features.redmine.utils import sanitize_attachment_filename, to_iso8601


if TYPE_CHECKING:
    from features.redmine.client import RedmineClient


logger = logging.getLogger(__name__)


# 可处理附件类型的统一匹配规则。
ATTACHMENT_PROCESSABLE_RE = re.compile(r"\.(zip|7z|rar|tar|tgz|gz|xml|txt|log|png|jpg|jpeg|webp|bmp|docx|pdf)$", re.IGNORECASE)
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
    # Delegate to the shared normalizer so all mixins format Redmine
    # timestamps identically (handles datetime + space-separated strings).
    return to_iso8601(value)


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
    # Attachment processing

    async def _process_attachment(self, client: RedmineClient, issue_id: int, attachment: RedmineAttachment) -> dict[str, Any]:
        issue_dir = self.attachments_dir / str(issue_id)
        issue_dir.mkdir(parents=True, exist_ok=True)
        safe_name = sanitize_attachment_filename(attachment.filename, f"attachment_{attachment.id}")
        local_path = issue_dir / f"{attachment.id}-{safe_name}"
        status = "skipped"
        error = ""
        analysis: dict[str, Any] = {"filename": attachment.filename, "failures": []}

        if ATTACHMENT_PROCESSABLE_RE.search(attachment.filename or ""):
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
            "content_url": attachment.content_url,
            "filesize": attachment.filesize,
            "local_path": str(local_path) if local_path.exists() else "",
            "analysis_json": analysis or {},
            "status": status,
            "error": error,
        }
        self.db.insert_attachment(item)
        return item

    def _analyze_local_attachment(self, path: str) -> dict[str, Any]:
        lower_path = path.lower()
        if lower_path.endswith((".txt", ".log")):
            return self._analyze_text_attachment(path)
        if lower_path.endswith(".pdf"):
            return self._analyze_pdf_attachment(path)
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
        # mkdtemp 的目录只作为 analyzer 的 workspace 父目录；analyzer 的
        # finally 只清理它自己创建的 workspace，不清理传入的 temp_dir，
        # 因此这里必须显式清理，否则每次附件分析都会在 /tmp 泄漏一个目录。
        staging_dir = tempfile.mkdtemp(prefix="redmine_agent_report_")
        try:
            analyzer = self.report_analyzer_factory(temp_dir=staging_dir)
            result = analyzer.analyze_file(path)
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)
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

    def _analyze_text_attachment(self, path: str) -> dict[str, Any]:
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

    def _analyze_image_attachment(self, path: str) -> dict[str, Any]:
        metadata = self._read_image_metadata(path)
        details: dict[str, Any] = {"type": "image", **metadata}

        # OCR is optional (Redmine.txt §5.3): use pytesseract if present, else
        # return an empty string. Failure here must never block the main flow.
        ocr_text = self._run_ocr(path)
        detected = detect_certification_errors(ocr_text)
        if detected["errors"]:
            details["ocr_text"] = ocr_text
            details["detected_errors"] = detected["errors"]
            details["detected_partitions"] = detected["partitions"]
            details["certification_type"] = detected["certification_type"]

        return {
            "filename": os.path.basename(path),
            "parsed": "error" not in metadata,
            "summary": details,
            "details": details,
            "text_excerpt": ocr_text[:3000],
            "failures": detected["failures"],
        }

    @staticmethod
    def _read_image_metadata(path: str) -> dict[str, Any]:
        try:
            from PIL import Image

            with Image.open(path) as image:
                return {
                    "width": image.width,
                    "height": image.height,
                    "format": image.format or "",
                    "mode": image.mode or "",
                }
        except Exception as exc:
            return {"error": str(exc)}

    @staticmethod
    def _run_ocr(path: str) -> str:
        """Best-effort OCR via pytesseract. Returns '' if unavailable or it fails.

        Uses a bundled tesseract binary (tools/tesseract) when the system one is
        not installed, so screenshot analysis works without apt/sudo access.
        """
        try:
            import pytesseract
            from PIL import Image
        except Exception:
            return ""
        try:
            # bundled_tesseract_cmd() and configure_bundled_tesseract() are both
            # cached after the first call, so this per-image check stays cheap.
            cmd = bundled_tesseract_cmd()
            if cmd:
                configure_bundled_tesseract()
                import pytesseract.pytesseract as _pt
                _pt.tesseract_cmd = cmd
            with Image.open(path) as image:
                return str(pytesseract.image_to_string(image, lang="chi_sim+eng") or "")
        except Exception as exc:
            logger.debug("[RedmineAgent] OCR skipped for %s: %s", path, exc)
            return ""

    def _analyze_pdf_attachment(self, path: str) -> dict[str, Any]:
        """Parse a PDF test report (BTS/CTS/VTS). Extracts text via any
        available library (pdfminer / PyMuPDF / pypdf), then runs the same
        error-block + certification-rule detection as text logs.

        Returns empty failures (but never raises) if no PDF backend is installed.
        """
        content = self._extract_pdf_text(path)
        error_blocks = self._extract_error_blocks(content)
        cert_detected = detect_certification_errors(content)
        failures: list[dict[str, Any]] = []
        if error_blocks:
            failures.append({
                "name": "pdf-report-analysis",
                "module": cert_detected.get("certification_type") or "report",
                "reason": _truncate("\n".join(error_blocks), 1200),
                "stack_trace": "",
            })
        # 认证错误也作为明确失败项输出。
        for cert_failure in cert_detected.get("failures") or []:
            failures.append(cert_failure)
        interesting = self._extract_failure_like_lines(content)
        return {
            "filename": os.path.basename(path),
            "parsed": bool(content),
            "summary": {
                "interesting_lines": len(interesting),
                "error_blocks": len(error_blocks),
                "certification_type": cert_detected.get("certification_type") or "",
                "characters": len(content),
            },
            "details": {
                "type": "pdf",
                "detected_errors": cert_detected.get("errors") or [],
                "detected_partitions": cert_detected.get("partitions") or [],
                "certification_type": cert_detected.get("certification_type") or "",
            },
            "text_excerpt": _truncate(content, 3000),
            "failures": failures,
        }

    @staticmethod
    def _extract_pdf_text(path: str) -> str:
        """Best-effort PDF text extraction. Returns '' if no backend available."""
        # PyMuPDF (fitz) — fastest, best layout.
        try:
            import fitz  # type: ignore

            text_parts: list[str] = []
            with fitz.open(path) as doc:
                for page in doc:
                    text_parts.append(page.get_text() or "")
            return "\n".join(text_parts)
        except Exception:
            pass
        # pdfminer.
        try:
            from pdfminer.high_level import extract_text  # type: ignore

            return str(extract_text(path) or "")
        except Exception:
            pass
        # pypdf / PyPDF2.
        try:
            try:
                from pypdf import PdfReader  # type: ignore
            except Exception:
                from PyPDF2 import PdfReader  # type: ignore
            reader = PdfReader(path)
            return "\n".join(str(page.extract_text() or "") for page in reader.pages)
        except Exception as exc:
            logger.debug("[RedmineAgent] PDF text extraction unavailable for %s: %s", path, exc)
            return ""

    def _analyze_docx_attachment(self, path: str) -> dict[str, Any]:
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

    # Error extraction

    def _extract_failure_like_lines(self, content: str, limit: int = MAX_FAILURE_LINES) -> list[str]:
        """Extract individual error lines from content."""
        return [
            line.strip()
            for line in str(content or "").splitlines()
            if _ERROR_LINE_RE.search(line)
        ][:limit]

    def _extract_error_blocks(self, content: str, max_blocks: int = MAX_ERROR_BLOCKS) -> list[str]:
        """Group consecutive error lines into logical blocks."""
        current_block: list[str] = []
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

    # Issue payload
