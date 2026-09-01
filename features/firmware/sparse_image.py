"""Build small zero-write patches for Android sparse-image holes."""

from __future__ import annotations

import os
import struct
import sys
from typing import BinaryIO


SPARSE_MAGIC = 0xED26FF3A
SPARSE_HEADER_SIZE = 28
CHUNK_HEADER_SIZE = 12
CHUNK_TYPE_RAW = 0xCAC1
CHUNK_TYPE_FILL = 0xCAC2
CHUNK_TYPE_DONT_CARE = 0xCAC3
CHUNK_TYPE_CRC32 = 0xCAC4
_COPY_BUFFER_SIZE = 1024 * 1024


class SparseImageError(ValueError):
    pass


def _discard_exact(source: BinaryIO, size: int) -> None:
    try:
        current = source.tell()
        end = source.seek(0, os.SEEK_END)
        if current + size > end:
            raise SparseImageError("sparse chunk payload is truncated")
        source.seek(current + size)
        return
    except (AttributeError, OSError):
        pass
    while size:
        chunk = source.read(min(size, _COPY_BUFFER_SIZE))
        if not chunk:
            raise SparseImageError("sparse chunk payload is truncated")
        size -= len(chunk)


def _copy_exact(source: BinaryIO, target: BinaryIO, size: int) -> None:
    while size:
        chunk = source.read(min(size, _COPY_BUFFER_SIZE))
        if not chunk:
            raise SparseImageError("sparse chunk payload is truncated")
        target.write(chunk)
        size -= len(chunk)


def _rewrite_dont_care_chunks(
    source: BinaryIO,
    target: BinaryIO,
    *,
    preserve_source_data: bool,
) -> int:
    header = bytearray(source.read(SPARSE_HEADER_SIZE))
    if len(header) != SPARSE_HEADER_SIZE:
        raise SparseImageError("sparse header is truncated")
    (
        magic,
        major,
        _minor,
        file_header_size,
        chunk_header_size,
        block_size,
        total_blocks,
        total_chunks,
        _checksum,
    ) = struct.unpack("<IHHHHIIII", header)
    if magic != SPARSE_MAGIC or major != 1:
        raise SparseImageError("unsupported Android sparse image")
    if file_header_size < SPARSE_HEADER_SIZE or chunk_header_size < CHUNK_HEADER_SIZE:
        raise SparseImageError("invalid sparse header sizes")
    if block_size <= 0 or total_chunks <= 0:
        raise SparseImageError("invalid sparse geometry")

    # The patch has different expanded content, so it must not claim the
    # source image's optional whole-image checksum.
    struct.pack_into("<I", header, 24, 0)
    target.write(header)
    header_extra = source.read(file_header_size - SPARSE_HEADER_SIZE)
    if len(header_extra) != file_header_size - SPARSE_HEADER_SIZE:
        raise SparseImageError("sparse extended header is truncated")
    target.write(header_extra)
    written_blocks = 0
    replacements = 0
    skipped_crc_chunks = 0
    for _index in range(total_chunks):
        chunk_header = source.read(CHUNK_HEADER_SIZE)
        if len(chunk_header) != CHUNK_HEADER_SIZE:
            raise SparseImageError("sparse chunk header is truncated")
        chunk_type, reserved, chunk_blocks, total_size = struct.unpack(
            "<HHII", chunk_header
        )
        header_extra_size = chunk_header_size - CHUNK_HEADER_SIZE
        header_extra = source.read(header_extra_size)
        if len(header_extra) != header_extra_size:
            raise SparseImageError("sparse extended chunk header is truncated")

        if chunk_type == CHUNK_TYPE_RAW:
            payload_size = chunk_blocks * block_size
        elif chunk_type == CHUNK_TYPE_FILL:
            payload_size = 4
        elif chunk_type == CHUNK_TYPE_DONT_CARE:
            payload_size = 0
        elif chunk_type == CHUNK_TYPE_CRC32:
            payload_size = 4
            if chunk_blocks != 0:
                raise SparseImageError("invalid sparse CRC32 chunk block count")
        else:
            raise SparseImageError(f"unknown sparse chunk type 0x{chunk_type:04x}")
        if total_size != chunk_header_size + payload_size:
            raise SparseImageError("sparse chunk size is inconsistent")

        # CRC chunks describe the source image's expanded content. They are
        # optional and cannot describe this deliberately partial write, so
        # omit them and correct the output header's chunk count below.
        if chunk_type == CHUNK_TYPE_CRC32:
            _discard_exact(source, payload_size)
            skipped_crc_chunks += 1
            continue

        if chunk_type == CHUNK_TYPE_DONT_CARE:
            target.write(struct.pack(
                "<HHII",
                CHUNK_TYPE_FILL,
                reserved,
                chunk_blocks,
                chunk_header_size + 4,
            ))
            target.write(header_extra)
            target.write(b"\0\0\0\0")
            replacements += 1
        elif preserve_source_data:
            target.write(chunk_header)
            target.write(header_extra)
            _copy_exact(source, target, payload_size)
        else:
            target.write(struct.pack(
                "<HHII",
                CHUNK_TYPE_DONT_CARE,
                reserved,
                chunk_blocks,
                chunk_header_size,
            ))
            target.write(header_extra)
            _discard_exact(source, payload_size)
        if chunk_type != CHUNK_TYPE_CRC32:
            written_blocks += chunk_blocks

    if written_blocks != total_blocks:
        raise SparseImageError("sparse chunks do not match the declared block count")
    if source.read(1):
        raise SparseImageError("sparse image has trailing data")
    if skipped_crc_chunks:
        output_end = target.tell()
        target.seek(20)
        target.write(struct.pack("<I", total_chunks - skipped_crc_chunks))
        target.seek(output_end)
    return replacements


def write_dont_care_zero_patch(source: BinaryIO, target: BinaryIO) -> int:
    """Write a tiny sparse image that zeroes only the source image's holes."""
    return _rewrite_dont_care_chunks(
        source, target, preserve_source_data=False,
    )


def write_zero_filled_sparse_image(source: BinaryIO, target: BinaryIO) -> int:
    """Preserve source data and explicitly zero every sparse image hole."""
    return _rewrite_dont_care_chunks(
        source, target, preserve_source_data=True,
    )


def create_zero_patch_file(source_path: str, target_path: str) -> int:
    with open(source_path, "rb") as source, open(target_path, "wb") as target:
        replacements = write_dont_care_zero_patch(source, target)
        target.flush()
        os.fsync(target.fileno())
    return replacements


def create_zero_filled_sparse_file(source_path: str, target_path: str) -> int:
    with open(source_path, "rb") as source, open(target_path, "wb") as target:
        replacements = write_zero_filled_sparse_image(source, target)
        target.flush()
        os.fsync(target.fileno())
    return replacements


def write_truncated_sparse_image(
    source: BinaryIO,
    target: BinaryIO,
    *,
    max_blocks: int,
) -> int:
    """Copy a sparse image, clamping its declared size to ``max_blocks``.

    Rockchip parameter.txt occasionally declares a ``super`` partition smaller
    than the image's expanded size; fastbootd rejects such a flash up front.
    Truncating the header's ``total_blocks`` keeps every chunk whose payload
    ends within the limit and drops the overflowing tail, so the image fits
    the actual partition while preserving all populated filesystem blocks
    (the tail of a sparse super image is unallocated DONT_CARE/FILL space).
    Returns the kept block count.
    """
    header = bytearray(source.read(SPARSE_HEADER_SIZE))
    if len(header) != SPARSE_HEADER_SIZE:
        raise SparseImageError("sparse header is truncated")
    (
        magic,
        major,
        _minor,
        file_header_size,
        chunk_header_size,
        block_size,
        total_blocks,
        total_chunks,
        _checksum,
    ) = struct.unpack("<IHHHHIIII", header)
    if magic != SPARSE_MAGIC or major != 1:
        raise SparseImageError("unsupported Android sparse image")
    if file_header_size < SPARSE_HEADER_SIZE or chunk_header_size < CHUNK_HEADER_SIZE:
        raise SparseImageError("invalid sparse header sizes")
    if block_size <= 0 or total_blocks <= 0:
        raise SparseImageError("invalid sparse geometry")
    if max_blocks <= 0 or max_blocks > total_blocks:
        raise SparseImageError(
            f"max_blocks {max_blocks} out of range (image has {total_blocks})"
        )

    struct.pack_into("<I", header, 16, max_blocks)
    struct.pack_into("<I", header, 24, 0)
    target.write(header)
    header_extra = source.read(file_header_size - SPARSE_HEADER_SIZE)
    if len(header_extra) != file_header_size - SPARSE_HEADER_SIZE:
        raise SparseImageError("sparse extended header is truncated")
    target.write(header_extra)

    kept_blocks = 0
    kept_chunks = 0
    truncated = False
    for _index in range(total_chunks):
        chunk_header = source.read(CHUNK_HEADER_SIZE)
        if len(chunk_header) != CHUNK_HEADER_SIZE:
            raise SparseImageError("sparse chunk header is truncated")
        chunk_type, reserved, chunk_blocks, total_size = struct.unpack(
            "<HHII", chunk_header
        )
        if chunk_type not in {
            CHUNK_TYPE_RAW, CHUNK_TYPE_FILL,
            CHUNK_TYPE_DONT_CARE, CHUNK_TYPE_CRC32,
        }:
            raise SparseImageError(
                f"unknown sparse chunk type 0x{chunk_type:04x}"
            )
        payload_size = {
            CHUNK_TYPE_RAW: chunk_blocks * block_size,
            CHUNK_TYPE_FILL: 4,
            CHUNK_TYPE_DONT_CARE: 0,
            CHUNK_TYPE_CRC32: 4,
        }[chunk_type]
        header_extra_size = chunk_header_size - CHUNK_HEADER_SIZE
        header_extra = source.read(header_extra_size)
        if len(header_extra) != header_extra_size:
            raise SparseImageError("sparse extended chunk header is truncated")
        payload = b""
        if payload_size:
            payload = source.read(payload_size)
            if len(payload) != payload_size:
                raise SparseImageError("sparse chunk payload is truncated")
        if truncated:
            continue
        if kept_blocks + chunk_blocks > max_blocks:
            if chunk_type != CHUNK_TYPE_DONT_CARE and chunk_blocks:
                raise SparseImageError(
                    "sparse tail contains data beyond the partition capacity"
                )
            truncated = True
            continue
        kept_blocks += chunk_blocks
        if chunk_type == CHUNK_TYPE_CRC32 and truncated:
            continue
        target.write(chunk_header)
        target.write(header_extra)
        if payload:
            target.write(payload)
        kept_chunks += 1

    if kept_blocks != max_blocks:
        raise SparseImageError(
            f"sparse chunks cover {kept_blocks} blocks, expected {max_blocks}"
        )
    output_end = target.tell()
    target.seek(20)
    target.write(struct.pack("<I", kept_chunks))
    target.seek(output_end)
    return kept_blocks


def create_truncated_sparse_file(
    source_path: str,
    target_path: str,
    *,
    max_blocks: int,
) -> int:
    with open(source_path, "rb") as source, open(target_path, "wb") as target:
        kept = write_truncated_sparse_image(
            source, target, max_blocks=max_blocks,
        )
        target.flush()
        os.fsync(target.fileno())
    return kept


def write_segment_sparse_image(
    source: BinaryIO,
    target: BinaryIO,
    *,
    total_size: int,
    block_size: int,
    start: int,
    end: int,
) -> int:
    """Emit one single-download sparse segment of a raw image.

    The header still declares the full image size, so fastbootd keeps the
    logical partition at its full extent.  Blocks outside ``[start, end)`` are
    DONT_CARE chunks (fastbootd leaves them untouched), and only the segment's
    blocks are RAW, so the packed file always fits in one
    ``max-download-size`` transfer.  Returns the raw payload byte count.
    """
    if total_size <= 0 or total_size % block_size:
        raise SparseImageError("segment total size must be a block multiple")
    if not 0 <= start < end <= total_size:
        raise SparseImageError("invalid segment range")
    if start % block_size or end % block_size:
        raise SparseImageError("segment bounds must be block aligned")
    total_blocks = total_size // block_size
    start_block = start // block_size
    end_block = end // block_size
    chunks: list[tuple[int, int]] = []  # (blocks, is_raw)
    if start_block:
        chunks.append((start_block, False))
    chunks.append((end_block - start_block, True))
    if end_block < total_blocks:
        chunks.append((total_blocks - end_block, False))

    target.write(struct.pack(
        "<IHHHHIIII",
        SPARSE_MAGIC,
        1,
        0,
        SPARSE_HEADER_SIZE,
        CHUNK_HEADER_SIZE,
        block_size,
        total_blocks,
        len(chunks),
        0,
    ))
    payload_bytes = 0
    for blocks, is_raw in chunks:
        if is_raw:
            size = blocks * block_size
            target.write(struct.pack(
                "<HHII", CHUNK_TYPE_RAW, 0, blocks, CHUNK_HEADER_SIZE + size,
            ))
            remaining = size
            source.seek(start)
            while remaining:
                piece = source.read(min(remaining, _COPY_BUFFER_SIZE))
                if not piece:
                    raise SparseImageError("raw image is truncated")
                target.write(piece)
                remaining -= len(piece)
            payload_bytes += size
        else:
            target.write(struct.pack(
                "<HHII", CHUNK_TYPE_DONT_CARE, 0, blocks, CHUNK_HEADER_SIZE,
            ))
    return payload_bytes


def create_segment_sparse_file(
    source_path: str,
    target_path: str,
    *,
    total_size: int,
    block_size: int,
    start: int,
    end: int,
) -> int:
    with open(source_path, "rb") as source, open(target_path, "wb") as target:
        payload = write_segment_sparse_image(
            source,
            target,
            total_size=total_size,
            block_size=block_size,
            start=start,
            end=end,
        )
        target.flush()
        os.fsync(target.fileno())
    return payload


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    full = bool(args and args[0] == "--full")
    if full:
        args.pop(0)
    segment = bool(args and args[0] == "--segment")
    if segment:
        args.pop(0)
    truncate = bool(args and args[0] == "--truncate")
    if truncate:
        args.pop(0)
    if segment:
        if len(args) != 6:
            print(
                "usage: sparse_image.py --segment RAW TOTAL BLOCK START END TARGET",
                file=sys.stderr,
            )
            return 2
        try:
            payload = create_segment_sparse_file(
                args[0],
                args[5],
                total_size=int(args[1]),
                block_size=int(args[2]),
                start=int(args[3]),
                end=int(args[4]),
            )
        except (OSError, SparseImageError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"segment_bytes={payload}")
        return 0
    if truncate:
        if len(args) != 3:
            print(
                "usage: sparse_image.py --truncate SOURCE MAX_BLOCKS TARGET",
                file=sys.stderr,
            )
            return 2
        try:
            kept = create_truncated_sparse_file(
                args[0], args[2], max_blocks=int(args[1]),
            )
        except (OSError, SparseImageError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"kept_blocks={kept}")
        return 0
    if len(args) != 2:
        print("usage: sparse_image.py [--full] SOURCE TARGET", file=sys.stderr)
        return 2
    try:
        replacements = (
            create_zero_filled_sparse_file(args[0], args[1])
            if full else create_zero_patch_file(args[0], args[1])
        )
    except (OSError, SparseImageError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    label = "zero_filled_chunks" if full else "zero_patch_chunks"
    print(f"{label}={replacements}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
