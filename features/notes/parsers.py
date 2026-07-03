"""文件解析：把上传的 PDF/TXT/MD/代码/DOCX/图片转成纯文本。

复用 features/redmine/analysis_attachments.py 的多后端提取逻辑（PDF/DOCX/OCR），
对纯文本与代码文件直接 UTF-8 读取。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 代码/配置文件后缀——直接当文本读，并标注 source='code'。
_CODE_SUFFIXES = {
    ".sh", ".py", ".java", ".kt", ".c", ".cpp", ".h", ".hpp", ".js", ".ts",
    ".json", ".xml", ".yaml", ".yml", ".conf", ".cfg", ".ini", ".mk", ".gradle",
    ".prop", ".properties", ".toml", ".rb", ".go", ".rs", ".php", ".sql", ".gitignore",
}
_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".log", ".csv"}


def detect_source(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "docx"
    if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}:
        return "image"
    if suffix in _CODE_SUFFIXES:
        return "code"
    return "txt"


def parse_file(path: str, filename: str) -> dict[str, Any]:
    """解析文件，返回 {text, source, parsed}。失败时 text='' 不抛异常。"""
    source = detect_source(filename)
    text = ""
    if source == "pdf":
        text = _extract_pdf_text(path)
    elif source == "docx":
        text = _extract_docx_text(path)
    elif source == "image":
        text = _run_ocr(path)
    else:  # txt / md / code
        text = _read_text(path)
    if not text.strip():
        logger.debug("[Notes] 解析为空: %s (%s)", filename, source)
    return {"text": text, "source": source, "parsed": bool(text.strip())}


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        logger.debug("[Notes] 读取文本失败 %s: %s", path, exc)
        return ""


def _extract_pdf_text(path: str) -> str:
    """复用 redmine 的多后端 PDF 提取（fitz → pdfminer → pypdf）。"""
    try:
        from features.redmine.analysis_attachments import AttachmentAnalysisMixin  # type: ignore
        return AttachmentAnalysisMixin._extract_pdf_text(path)  # type: ignore[attr-defined]
    except Exception:
        pass
    # 直接实现回退，避免 redmine 内部结构变化时全失效。
    try:
        import fitz  # type: ignore
        parts: list[str] = []
        with fitz.open(path) as doc:
            for page in doc:
                parts.append(page.get_text() or "")
        return "\n".join(parts)
    except Exception:
        pass
    try:
        from pdfminer.high_level import extract_text  # type: ignore
        return str(extract_text(path) or "")
    except Exception:
        pass
    try:
        try:
            from pypdf import PdfReader  # type: ignore
        except Exception:
            from PyPDF2 import PdfReader  # type: ignore
        reader = PdfReader(path)
        return "\n".join(str(p.extract_text() or "") for p in reader.pages)
    except Exception as exc:
        logger.debug("[Notes] PDF 提取不可用 %s: %s", path, exc)
        return ""


def _extract_docx_text(path: str) -> str:
    try:
        from docx import Document  # type: ignore
        doc = Document(path)
        return "\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip())
    except Exception:
        pass
    try:
        import zipfile
        import re
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", xml))
    except Exception as exc:
        logger.debug("[Notes] DOCX 提取失败 %s: %s", path, exc)
        return ""


def _run_ocr(path: str) -> str:
    """复用 redmine 的 bundled tesseract OCR。"""
    try:
        from features.redmine.analysis_attachments import AttachmentAnalysisMixin  # type: ignore
        return AttachmentAnalysisMixin._run_ocr(path)  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
        with Image.open(path) as image:
            return str(pytesseract.image_to_string(image, lang="chi_sim+eng") or "")
    except Exception as exc:
        logger.debug("[Notes] OCR 不可用 %s: %s", path, exc)
        return ""
