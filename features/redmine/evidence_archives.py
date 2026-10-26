"""Derive searchable RAR/7z text through the audited report extractor."""

from __future__ import annotations

import shutil
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
    artifact_id: str, snapshot_rel: str, source: Path,
) -> tuple[str, str]:
    # Reuse the existing archive preflight, path/symlink checks, expansion
    # limits and subprocess boundary; attachment contents are never executed.
    # 输入是磁盘原件路径：整条链路（复制/解包/成员筛选）保持磁盘到磁盘，
    # 不在进程内整包持有字节（附件可接近快照总下载预算）。
    with source.open("rb") as handle:
        head = handle.read(8)
    if head.startswith(b"Rar!\x1a\x07"):
        extension = ".rar"
    elif head.startswith(b"7z\xbc\xaf\x27\x1c"):
        extension = ".7z"
    else:
        return "", "压缩附件格式暂不支持，内容尚未提取；不能据此判断日志缺失。"
    try:
        with tempfile.TemporaryDirectory(prefix="gms-evidence-archive-") as directory:
            root = Path(directory)
            attachment = root / ("attachment" + extension)
            shutil.copyfile(source, attachment)
            destination = root / "members"
            destination.mkdir()
            extract_system_archive(str(attachment), str(destination))
            members = sorted(
                (path for path in destination.rglob("*")
                 if path.is_file() and path.suffix.lower() in TEXT_KIND_EXTENSIONS),
                key=lambda path: _member_priority(path.relative_to(destination).as_posix()),
            )
            selected_path = root / "selected.zip"
            total = 0
            included = 0
            skipped = 0
            with zipfile.ZipFile(selected_path, "w") as archive:
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
                store, write_file, artifact_id, snapshot_rel, selected_path,
            )
            if skipped:
                note = "; ".join(filter(None, [note, f"跳过 {skipped} 个超阈值压缩成员，检索索引不完整"]))
            if not included:
                note = note or "压缩附件中未提取到支持的文本成员。"
            return derived, note
    except (OSError, ValueError):
        return "", "压缩附件文本提取失败，内容尚未提取。"
