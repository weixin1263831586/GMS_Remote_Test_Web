from __future__ import annotations

from pathlib import Path


TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".log",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".css",
    ".js",
    ".ts",
    ".py",
    ".java",
    ".kt",
    ".c",
    ".cpp",
    ".h",
    ".sh",
}


def parse_file(path: str, filename: str = "") -> dict[str, str]:
    file_path = Path(path)
    suffix = (file_path.suffix or Path(filename).suffix).lower()
    if suffix in TEXT_SUFFIXES:
        return {"text": file_path.read_text(encoding="utf-8", errors="replace"), "source": suffix.lstrip(".") or "txt"}
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(file_path))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            return {"text": text, "source": "pdf"}
        except Exception:
            return {"text": "", "source": "pdf"}
    if suffix == ".docx":
        try:
            import docx

            doc = docx.Document(str(file_path))
            return {"text": "\n".join(p.text for p in doc.paragraphs), "source": "docx"}
        except Exception:
            return {"text": "", "source": "docx"}
    return {"text": "", "source": suffix.lstrip(".") or "file"}
