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
    ".htm",
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
    if suffix in {'.html', '.htm'}:
        from lxml import etree, html

        try:
            root = html.fromstring(file_path.read_bytes(), parser=html.HTMLParser(no_network=True))
            for node in root.xpath('//script|//style|//nav|//form|//noscript'):
                if node.getparent() is not None:
                    node.drop_tree()
            text = '\n'.join(' '.join(value.split()) for value in root.itertext() if value.strip())
            return {'text': text, 'source': 'html'}
        except (ValueError, OSError, etree.ParserError):
            return {'text': '', 'source': 'html'}
    if suffix in TEXT_SUFFIXES:
        return {"text": file_path.read_text(encoding="utf-8", errors="replace"), "source": suffix.lstrip(".") or "txt"}
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(file_path))
            if reader.is_encrypted or len(reader.pages) > 500:
                return {"text": "", "source": "pdf"}
            parts = []
            total = 0
            for page in reader.pages:
                text = page.extract_text() or ""
                total += len(text)
                if total > 5_000_000:
                    return {"text": "", "source": "pdf"}
                parts.append(text)
            text = "\n".join(parts)
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
