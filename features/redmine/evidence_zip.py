"""Bounded, searchable text derivation for Redmine ZIP attachments."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from pathlib import Path

from .evidence import (
    TEXT_KIND_EXTENSIONS,
    ZIP_MEMBER_DERIVED_TOTAL_MAX_BYTES,
    ZIP_MEMBER_TEXT_MAX_BYTES,
    ZIP_MEMBER_TEXT_MAX_MEMBERS,
    _detect_text_encoding,
    zip_member_block,
)
from .evidence_store import EvidenceStore


def extract_zip_derived_text(
    store: EvidenceStore,
    write_file: Callable[[Path, bytes], None],
    artifact_id: str,
    snapshot_rel: str,
    data: bytes,
) -> tuple[str, str]:
    """Build a bounded framed index without extracting member files."""

    chunks: list[str] = []
    total = 0
    included = 0
    skipped_large = 0
    limit_note = ""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.is_dir() or Path(info.filename).suffix.lower() not in TEXT_KIND_EXTENSIONS:
                    continue
                if included >= ZIP_MEMBER_TEXT_MAX_MEMBERS:
                    limit_note = (
                        f"zip 文本成员数超过上限 {ZIP_MEMBER_TEXT_MAX_MEMBERS}，"
                        "检索索引不完整"
                    )
                    break
                if info.file_size > ZIP_MEMBER_TEXT_MAX_BYTES:
                    skipped_large += 1
                    continue
                if total + info.file_size > ZIP_MEMBER_DERIVED_TOTAL_MAX_BYTES:
                    limit_note = (
                        f"zip 派生文本超过总量上限 "
                        f"{ZIP_MEMBER_DERIVED_TOTAL_MAX_BYTES} 字节，检索索引不完整"
                    )
                    break
                try:
                    with archive.open(info) as member_file:
                        raw = member_file.read(ZIP_MEMBER_TEXT_MAX_BYTES + 1)
                except (zipfile.BadZipFile, OSError, RuntimeError):
                    continue
                if len(raw) > ZIP_MEMBER_TEXT_MAX_BYTES:
                    skipped_large += 1
                    continue
                encoding = _detect_text_encoding(raw) or "utf-8"
                block = zip_member_block(
                    info.filename, raw.decode(encoding, errors="replace")
                )
                block_size = len(block.encode("utf-8"))
                if total + block_size > ZIP_MEMBER_DERIVED_TOTAL_MAX_BYTES:
                    limit_note = (
                        f"zip 派生文本超过总量上限 "
                        f"{ZIP_MEMBER_DERIVED_TOTAL_MAX_BYTES} 字节，检索索引不完整"
                    )
                    break
                chunks.append(block)
                total += block_size
                included += 1
    except (zipfile.BadZipFile, OSError) as exc:
        return "", f"zip 解包失败: {type(exc).__name__}"

    notes = []
    if skipped_large:
        notes.append(f"跳过 {skipped_large} 个超阈值 zip 成员，检索索引不完整")
    if limit_note:
        notes.append(limit_note)
    note = "; ".join(notes)
    if not included:
        return "", note
    derived_rel = f"{snapshot_rel}/derived/{artifact_id}.txt"
    try:
        write_file(
            store.resolve_internal(derived_rel),
            ("\n".join(chunks) + "\n").encode("utf-8"),
        )
    except OSError as exc:
        return "", f"派生 zip 文本失败: {type(exc).__name__}"
    return derived_rel, note
