"""Complete, streaming text derivation for Redmine ZIP attachments."""

from __future__ import annotations

import zipfile
from collections.abc import Callable
from pathlib import Path

from .evidence import TEXT_KIND_EXTENSIONS
from .evidence_store import EvidenceStore
from .evidence_text import derive_member_index


def _member_priority(filename: str) -> tuple[int, str]:
    """Keep test manifests first, followed by all other text members."""
    name = filename.lower()
    basename = name.rsplit("/", 1)[-1]
    if basename.startswith("test_result") and name.endswith((".xml", ".html")):
        return (0, filename)
    if any(marker in name for marker in ("test_result", "result.xml", "summary.xml")):
        return (1, filename)
    return (2, filename)


def extract_zip_derived_text(
    store: EvidenceStore,
    write_file: Callable[[Path, bytes], None],
    artifact_id: str,
    snapshot_rel: str,
    source: Path,
) -> tuple[str, str]:
    """Read every text member and atomically publish its complete index.

    write_file remains accepted for existing callers. Streaming publication
    uses temporary-file/rename semantics without a whole-archive bytes value.
    """
    try:
        with zipfile.ZipFile(source) as archive:
            members = sorted(
                (info for info in archive.infolist()
                 if not info.is_dir() and Path(info.filename).suffix.lower() in TEXT_KIND_EXTENSIONS),
                key=lambda info: _member_priority(info.filename),
            )
            return derive_member_index(
                store, artifact_id, snapshot_rel,
                ((info.filename, lambda info=info: archive.open(info)) for info in members),
            )
    except (zipfile.BadZipFile, OSError) as exc:
        return "", f"zip 文本提取失败: {type(exc).__name__}，内容尚未完整提取"
