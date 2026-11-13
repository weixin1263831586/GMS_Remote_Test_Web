"""Complete text derivation with bounded memory and atomic publication."""

from __future__ import annotations

import base64
import codecs
import os
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path


TEXT_CHUNK_BYTES = 256 * 1024


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

    Members are (name, opener) pairs. No indexing budget excludes members.
    Failed reads remain explicit and never publish a partial member body.
    """
    relative = f"{snapshot_rel}/derived/{artifact_id}.txt"
    destination = store.resolve_internal(relative)
    included = 0
    errors = []
    with atomic_output(destination) as output:
        for name, opener in members:
            with tempfile.TemporaryFile() as spool:
                try:
                    length = 0
                    with opener() as source:
                        encoding = detect_stream_encoding(source)
                        for text in decoded_chunks(source, encoding):
                            length += len(text)
                            spool.write(text.encode("utf-8"))
                except (OSError, ValueError, RuntimeError, EOFError, UnicodeError, zipfile.BadZipFile) as exc:
                    errors.append(f"成员 {name} 读取失败: {type(exc).__name__}，检索索引不完整")
                    continue
                encoded = base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii").rstrip("=")
                output.write(f"<<<zip-member-v2:{encoded}:{length}>>>\n".encode())
                spool.seek(0)
                shutil.copyfileobj(spool, output, length=TEXT_CHUNK_BYTES)
                output.write(b"\n")
                included += 1
    if not included:
        destination.unlink(missing_ok=True)
        return "", "; ".join(errors)
    return relative, "; ".join(errors)
