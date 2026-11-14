"""Complete archive text derivation through streaming and audited extraction."""

from __future__ import annotations

import bz2
import gzip
import lzma
import tarfile
import tempfile
from pathlib import Path

from foundation.archives import extract_system_archive

from .evidence import DERIVED_TEXT_MAX_CHARS, EXTRACT_TIMEOUT_SECONDS, TEXT_KIND_EXTENSIONS
from .evidence_store import EvidenceStore
from .evidence_text import derive_member_index
from .evidence_zip import _member_priority, _size_budget_note


def extract_system_archive_text(
    store: EvidenceStore, artifact_id: str, snapshot_rel: str, source: Path,
) -> tuple[str, str]:
    with source.open("rb") as handle:
        head = handle.read(8)
    try:
        if tarfile.is_tarfile(source):
            # Members are read directly: archive paths are never extracted.
            # 声明大小超过派生总预算的成员提前拒绝（不解压不读取）；
            # 声明撒谎由字符预算在解码流上硬停。
            with tarfile.open(source, "r:*") as archive:
                oversized = 0
                selected = []
                for info in archive:
                    if not info.isfile() or Path(info.name).suffix.lower() not in TEXT_KIND_EXTENSIONS:
                        continue
                    if info.size > DERIVED_TEXT_MAX_CHARS:
                        oversized += 1
                        continue
                    selected.append(info)
                members = sorted(selected, key=lambda info: _member_priority(info.name))
                derived, note = derive_member_index(
                    store, artifact_id, snapshot_rel,
                    ((info.name, lambda info=info: archive.extractfile(info)) for info in members),
                )
                if oversized:
                    note = "; ".join(x for x in (note, _size_budget_note(oversized)) if x)
                return derived, note
        compressor = (gzip if head.startswith(b"\x1f\x8b") else
                      bz2 if head.startswith(b"BZh") else
                      lzma if head.startswith(b"\xfd7zXZ\x00") else None)
        if compressor is not None:
            return derive_member_index(
                store, artifact_id, snapshot_rel,
                [(source.stem, lambda: compressor.open(source, "rb"))],
            )
        if not head.startswith((b"Rar!\x1a\x07", b"7z\xbc\xaf\x27\x1c")):
            return "", "压缩附件格式暂不支持，内容尚未提取；不能据此判断日志缺失。"
        with tempfile.TemporaryDirectory(prefix="gms-evidence-archive-") as directory:
            destination = Path(directory)
            # Keep the shared path/symlink/security preflight. 解包进程受
            # EXTRACT_TIMEOUT_SECONDS 时长预算（外部进程
            # 不得无限运行；foundation 超时会杀整个进程组），文件数与展开
            # 大小仍由 foundation 的解压安全检查兜底。
            extract_system_archive(
                str(source), str(destination), timeout=EXTRACT_TIMEOUT_SECONDS
            )
            members = sorted(
                (path for path in destination.rglob("*")
                 if path.is_file() and path.suffix.lower() in TEXT_KIND_EXTENSIONS),
                key=lambda path: _member_priority(path.relative_to(destination).as_posix()),
            )
            return derive_member_index(
                store, artifact_id, snapshot_rel,
                ((path.relative_to(destination).as_posix(), lambda path=path: path.open("rb"))
                 for path in members),
            )
    except (OSError, ValueError, tarfile.TarError, lzma.LZMAError) as exc:
        return "", f"压缩附件文本提取失败: {type(exc).__name__}，内容尚未提取完整。"
