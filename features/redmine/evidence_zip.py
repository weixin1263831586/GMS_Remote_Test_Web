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


def _member_priority(filename: str) -> tuple[int, str]:
    """成员索引进序：测试 manifest 优先，其余按文件名稳定排序。

    ZIP 成员数/字节预算超限时按遍历顺序截断会把 test_result.xml 这类
    关键证据挤出索引（#646220 实测踩坑）。manifest 类成员（测试结果
    XML/HTML、汇总报告）排最前，普通文本次之，保证相同预算下关键证据
    命中率最高。返回值同时承担排序键（先优先级后文件名，deterministic）。
    """
    name = filename.lower()
    basename = name.rsplit("/", 1)[-1]
    if basename.startswith(("test_result",)) and name.endswith((".xml", ".html")):
        return (0, filename)
    if basename in ("test_result.xml", "test_result.html"):
        return (0, filename)
    if any(marker in name for marker in ("test_result", "result.xml", "summary.xml")):
        return (1, filename)
    return (2, filename)


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
            members = sorted(
                (
                    info
                    for info in archive.infolist()
                    if not info.is_dir()
                    and Path(info.filename).suffix.lower() in TEXT_KIND_EXTENSIONS
                ),
                key=lambda info: _member_priority(info.filename),
            )
            for info in members:
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
