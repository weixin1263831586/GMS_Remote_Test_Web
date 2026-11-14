"""Complete, streaming text derivation for Redmine ZIP attachments."""

from __future__ import annotations

import zipfile
from pathlib import Path

from .evidence import DERIVED_TEXT_MAX_CHARS, TEXT_KIND_EXTENSIONS
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


def _size_budget_note(count: int) -> str:
    return (
        f"{count} 个成员按压缩包声明大小超过 {DERIVED_TEXT_MAX_CHARS} "
        "字符派生预算，未提取"
    )


def extract_zip_derived_text(
    store: EvidenceStore,
    artifact_id: str,
    snapshot_rel: str,
    source: Path,
) -> tuple[str, str]:
    """Read every text member and atomically publish its complete index.

    Streaming publication uses temporary-file/rename semantics without a
    whole-archive bytes value. ZIP 声明大小（file_size）超过派生总预算的
    成员先被拒绝（2026-11-14 审核意见：元数据可提前拒绝的炸弹不解压）；
    声明撒谎（file_size 偏小）的成员由字符预算在解码流上硬停。
    """
    try:
        with zipfile.ZipFile(source) as archive:
            selected: list[zipfile.ZipInfo] = []
            oversized = 0
            for info in archive.infolist():
                if info.is_dir() or Path(info.filename).suffix.lower() not in TEXT_KIND_EXTENSIONS:
                    continue
                if info.file_size > DERIVED_TEXT_MAX_CHARS:
                    oversized += 1
                    continue
                selected.append(info)
            members = sorted(selected, key=lambda info: _member_priority(info.filename))
            derived, note = derive_member_index(
                store, artifact_id, snapshot_rel,
                ((info.filename, lambda info=info: archive.open(info)) for info in members),
            )
            if oversized:
                note = "; ".join(x for x in (note, _size_budget_note(oversized)) if x)
            return derived, note
    except (zipfile.BadZipFile, OSError) as exc:
        return "", f"zip 文本提取失败: {type(exc).__name__}，内容尚未完整提取"
