"""Derive searchable RAR/7z text through the audited report extractor."""

from __future__ import annotations

import io
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path

from foundation.archives import extract_system_archive

from .evidence import (
    TEXT_KIND_EXTENSIONS,
    ZIP_MEMBER_DERIVED_TOTAL_MAX_BYTES,
    ZIP_MEMBER_TEXT_MAX_BYTES,
    ZIP_MEMBER_TEXT_MAX_MEMBERS,
)
from .evidence_store import EvidenceStore
from .evidence_zip import _member_priority, extract_zip_derived_text


def extract_system_archive_text(
    store: EvidenceStore, write_file: Callable[[Path, bytes], None],
    artifact_id: str, snapshot_rel: str, data: bytes,
) -> tuple[str, str]:
    # Reuse the existing archive preflight, path/symlink checks, expansion
    # limits and subprocess boundary; attachment contents are never executed.
    extension = '.rar' if data.startswith(b'Rar!\x1a\x07') else '.7z'
    if extension == '.7z' and not data.startswith(b"7z\xbc\xaf\x27\x1c"):
        return '', '压缩附件格式暂不支持，内容尚未提取；不能据此判断日志缺失。'
    try:
        with tempfile.TemporaryDirectory(prefix='gms-evidence-archive-') as directory:
            root = Path(directory)
            source = root / ('attachment' + extension)
            source.write_bytes(data)
            destination = root / 'members'
            destination.mkdir()
            extract_system_archive(str(source), str(destination))
            members = sorted(
                (path for path in destination.rglob('*')
                 if path.is_file() and path.suffix.lower() in TEXT_KIND_EXTENSIONS),
                key=lambda path: _member_priority(path.relative_to(destination).as_posix()),
            )
            selected = io.BytesIO()
            total = 0
            included = 0
            skipped = 0
            with zipfile.ZipFile(selected, 'w') as archive:
                for path in members:
                    size = path.stat().st_size
                    if (size > ZIP_MEMBER_TEXT_MAX_BYTES
                            or total + size > ZIP_MEMBER_DERIVED_TOTAL_MAX_BYTES
                            or included >= ZIP_MEMBER_TEXT_MAX_MEMBERS):
                        skipped += 1
                        continue
                    payload = path.read_bytes()
                    archive.writestr(path.relative_to(destination).as_posix(), payload)
                    included += 1
                    total += len(payload)
            derived, note = extract_zip_derived_text(
                store, write_file, artifact_id, snapshot_rel, selected.getvalue(),
            )
            if skipped:
                note = '; '.join(filter(None, [note, f'跳过 {skipped} 个超阈值压缩成员，检索索引不完整']))
            if not included:
                note = note or '压缩附件中未提取到支持的文本成员。'
            return derived, note
    except (OSError, ValueError):
        return '', '压缩附件文本提取失败，内容尚未提取。'
