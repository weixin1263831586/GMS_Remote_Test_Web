"""Complete text derivation with bounded memory and atomic publication."""

from __future__ import annotations

import base64
import codecs
import json
import os
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .evidence import DERIVED_TEXT_MAX_CHARS


TEXT_CHUNK_BYTES = 256 * 1024


class _MemberBudgetError(Exception):
    """单个成员解码长度突破派生文本预算的内部信号。"""


def detect_stream_encoding(source) -> str:
    """Validate the entire stream before choosing a decoder, including its tail."""
    source.seek(0)
    head = source.read(4)
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        candidates = ("utf-16",)
    elif head.startswith(codecs.BOM_UTF8):
        candidates = ("utf-8-sig",)
    else:
        candidates = ("utf-8", "gb18030", "utf-16", "latin-1")
    for encoding in candidates:
        source.seek(0)
        decoder = codecs.getincrementaldecoder(encoding)("strict")
        try:
            while raw := source.read(TEXT_CHUNK_BYTES):
                decoder.decode(raw)
            decoder.decode(b"", final=True)
        except UnicodeError:
            continue
        source.seek(0)
        return encoding
    raise UnicodeError("文本编码未识别")


def decoded_chunks(source, encoding: str):
    decoder = codecs.getincrementaldecoder(encoding)("strict")
    while raw := source.read(TEXT_CHUNK_BYTES):
        yield decoder.decode(raw)
    yield decoder.decode(b"", final=True)


@contextmanager
def atomic_output(destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".part", dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            yield output
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def derive_text_file(store, artifact_id: str, snapshot_rel: str, source: Path):
    try:
        with source.open("rb") as handle:
            encoding = detect_stream_encoding(handle)
            if encoding == "utf-8":
                return "", ""
            relative = f"{snapshot_rel}/derived/{artifact_id}.txt"
            with atomic_output(store.resolve_internal(relative)) as output:
                for text in decoded_chunks(handle, encoding):
                    output.write(text.encode("utf-8"))
        return relative, ""
    except (OSError, UnicodeError) as exc:
        return "", f"派生 UTF-8 文本失败: {type(exc).__name__}"


def derive_member_index(store, artifact_id: str, snapshot_rel: str, members):
    """Spool each member to obtain the character length of its v2 frame.

    Members are (name, opener) pairs. A char budget bounds the published
    derived text (压缩炸弹/索引膨胀防护)：超出预算的成员
    不再进入索引，错误显式记录并让调用方把 derived_status 记为 partial
    ——未索引的成员绝不能当作不存在。Failed reads remain explicit and
    never publish a partial member body.

    A sidecar ``<artifact>.txt.idx.json`` records each member's global char
    range in the derived text plus the total char count, so artifact_read can
    map a window (or a search artifact_char_index hit) to its member without
    rescanning frames. Older derived files without a sidecar degrade to
    frame-less reads (no annotation), never to an error.
    """
    relative = f"{snapshot_rel}/derived/{artifact_id}.txt"
    destination = store.resolve_internal(relative)
    included = 0
    errors = []
    index_members: list[dict[str, Any]] = []
    cursor = 0
    with atomic_output(destination) as output:
        for name, opener in members:
            if cursor >= DERIVED_TEXT_MAX_CHARS:
                errors.append(
                    f"成员 {name} 起超出 {DERIVED_TEXT_MAX_CHARS} 字符派生预算，"
                    "其余成员未计入索引"
                )
                break
            with tempfile.TemporaryFile() as spool:
                try:
                    length = 0
                    with opener() as source:
                        encoding = detect_stream_encoding(source)
                        for text in decoded_chunks(source, encoding):
                            length += len(text)
                            if length > DERIVED_TEXT_MAX_CHARS:
                                raise _MemberBudgetError(name)
                            spool.write(text.encode("utf-8"))
                except _MemberBudgetError:
                    errors.append(
                        f"成员 {name} 超出 {DERIVED_TEXT_MAX_CHARS} 字符派生预算，未计入索引"
                    )
                    continue
                except (OSError, ValueError, RuntimeError, EOFError, UnicodeError, zipfile.BadZipFile) as exc:
                    errors.append(f"成员 {name} 读取失败: {type(exc).__name__}，检索索引不完整")
                    continue
                encoded = base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii").rstrip("=")
                output.write(f"<<<zip-member-v2:{encoded}:{length}>>>\n".encode())
                cursor += len(f"<<<zip-member-v2:{encoded}:{length}>>>\n")
                spool.seek(0)
                shutil.copyfileobj(spool, output, length=TEXT_CHUNK_BYTES)
                output.write(b"\n")
                cursor += length + 1
                index_members.append({
                    "name": name,
                    # Global char range of the member BODY inside the derived
                    # text: artifact_char_index values from search fall in
                    # [start, start + length).
                    "start": cursor - length - 1,
                    "length": length,
                })
                included += 1
    if not included:
        destination.unlink(missing_ok=True)
        return "", "; ".join(errors)
    sidecar = destination.parent / f"{destination.name}.idx.json"
    try:
        with atomic_output(sidecar) as output:
            output.write(json.dumps({
                "version": 1,
                "total_chars": cursor,
                "members": index_members,
            }, ensure_ascii=False).encode("utf-8"))
    except OSError as exc:
        # 索引文件只是加速注记；写失败不回收已成功的派生文本。
        errors.append(f"zip 索引 sidecar 写入失败: {type(exc).__name__}")
    return relative, "; ".join(errors)
