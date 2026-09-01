from __future__ import annotations

import io
import struct

import pytest

from features.firmware import sparse_image


def _sparse_image() -> bytes:
    block_size = 4
    chunks = [
        struct.pack("<HHII", sparse_image.CHUNK_TYPE_RAW, 0, 1, 16) + b"ABCD",
        struct.pack("<HHII", sparse_image.CHUNK_TYPE_DONT_CARE, 0, 2, 12),
        struct.pack("<HHII", sparse_image.CHUNK_TYPE_FILL, 0, 1, 16)
        + b"\x11\x22\x33\x44",
    ]
    header = struct.pack(
        "<IHHHHIIII",
        sparse_image.SPARSE_MAGIC,
        1,
        0,
        sparse_image.SPARSE_HEADER_SIZE,
        sparse_image.CHUNK_HEADER_SIZE,
        block_size,
        4,
        len(chunks),
        0,
    )
    return header + b"".join(chunks)


def test_zero_patch_writes_only_original_dont_care_chunks() -> None:
    output = io.BytesIO()

    replacements = sparse_image.write_dont_care_zero_patch(
        io.BytesIO(_sparse_image()), output,
    )

    result = output.getvalue()
    assert replacements == 1
    assert len(result) < len(_sparse_image())
    first_chunk = sparse_image.SPARSE_HEADER_SIZE
    assert struct.unpack_from("<HHII", result, first_chunk) == (
        sparse_image.CHUNK_TYPE_DONT_CARE,
        0,
        1,
        12,
    )
    second_chunk = first_chunk + 12
    assert struct.unpack_from("<HHII", result, second_chunk) == (
        sparse_image.CHUNK_TYPE_FILL,
        0,
        2,
        16,
    )
    assert result[second_chunk + 12:second_chunk + 16] == b"\0\0\0\0"


def test_zero_filled_sparse_preserves_data_and_replaces_holes() -> None:
    output = io.BytesIO()

    replacements = sparse_image.write_zero_filled_sparse_image(
        io.BytesIO(_sparse_image()), output,
    )

    result = output.getvalue()
    assert replacements == 1
    first_chunk = sparse_image.SPARSE_HEADER_SIZE
    assert struct.unpack_from("<HHII", result, first_chunk) == (
        sparse_image.CHUNK_TYPE_RAW,
        0,
        1,
        16,
    )
    assert result[first_chunk + 12:first_chunk + 16] == b"ABCD"
    second_chunk = first_chunk + 16
    assert struct.unpack_from("<HHII", result, second_chunk) == (
        sparse_image.CHUNK_TYPE_FILL,
        0,
        2,
        16,
    )
    assert result[second_chunk + 12:second_chunk + 16] == b"\0\0\0\0"
    third_chunk = second_chunk + 16
    assert struct.unpack_from("<HHII", result, third_chunk) == (
        sparse_image.CHUNK_TYPE_FILL,
        0,
        1,
        16,
    )
    assert result[third_chunk + 12:third_chunk + 16] == b"\x11\x22\x33\x44"


def test_inconsistent_sparse_chunk_size_is_rejected() -> None:
    malformed = bytearray(_sparse_image())
    struct.pack_into("<I", malformed, sparse_image.SPARSE_HEADER_SIZE + 8, 15)

    with pytest.raises(sparse_image.SparseImageError, match="inconsistent"):
        sparse_image.write_dont_care_zero_patch(
            io.BytesIO(malformed), io.BytesIO(),
        )


def test_source_crc_chunk_is_omitted_from_partial_zero_patch() -> None:
    source = bytearray(_sparse_image())
    struct.pack_into("<I", source, 20, 4)
    source.extend(struct.pack(
        "<HHIII", sparse_image.CHUNK_TYPE_CRC32, 0, 0, 16, 0x12345678,
    ))
    output = io.BytesIO()

    sparse_image.write_dont_care_zero_patch(io.BytesIO(source), output)

    result = output.getvalue()
    assert struct.unpack_from("<I", result, 20)[0] == 3
    assert struct.pack("<H", sparse_image.CHUNK_TYPE_CRC32) not in result[28:]


def test_segment_sparse_image_covers_range_and_declares_full_extent() -> None:
    block_size = 4
    total = 12
    raw = bytes(range(total))
    output = io.BytesIO()

    payload = sparse_image.write_segment_sparse_image(
        io.BytesIO(raw),
        output,
        total_size=total,
        block_size=block_size,
        start=4,
        end=8,
    )

    result = output.getvalue()
    (
        magic, _major, _minor, _fh, _ch, _out_block, total_blocks, chunk_count,
        _checksum,
    ) = struct.unpack_from("<IHHHHIIII", result, 0)
    assert magic == sparse_image.SPARSE_MAGIC
    assert (block_size, total_blocks, chunk_count) == (block_size, 3, 3)
    assert payload == 4

    offset = sparse_image.SPARSE_HEADER_SIZE
    first = struct.unpack_from("<HHII", result, offset)
    assert first[0] == sparse_image.CHUNK_TYPE_DONT_CARE
    assert first[2] == 1
    offset += first[3]
    second = struct.unpack_from("<HHII", result, offset)
    assert second[0] == sparse_image.CHUNK_TYPE_RAW
    assert second[2] == 1
    offset += sparse_image.CHUNK_HEADER_SIZE
    assert result[offset:offset + 4] == raw[4:8]
    offset += 4
    third = struct.unpack_from("<HHII", result, offset)
    assert third[0] == sparse_image.CHUNK_TYPE_DONT_CARE
    assert third[2] == 1


def test_segment_sparse_image_rejects_misaligned_bounds() -> None:
    with pytest.raises(sparse_image.SparseImageError, match="aligned"):
        sparse_image.write_segment_sparse_image(
            io.BytesIO(b"\0" * 16),
            io.BytesIO(),
            total_size=12,
            block_size=4,
            start=2,
            end=8,
        )


def _large_super_sparse() -> bytes:
    """A 6-block sparse super: 2 RAW + 2 FILL + 2 DONT_CARE tail."""
    block_size = 4
    chunks = [
        struct.pack("<HHII", sparse_image.CHUNK_TYPE_RAW, 0, 2, 20) + b"WXYZABCD",
        struct.pack(
            "<HHII", sparse_image.CHUNK_TYPE_FILL, 0, 2, 16
        ) + b"\x00\x00\x00\x00",
        struct.pack("<HHII", sparse_image.CHUNK_TYPE_DONT_CARE, 0, 2, 12),
    ]
    header = struct.pack(
        "<IHHHHIIII",
        sparse_image.SPARSE_MAGIC,
        1,
        0,
        sparse_image.SPARSE_HEADER_SIZE,
        sparse_image.CHUNK_HEADER_SIZE,
        block_size,
        6,
        len(chunks),
        0,
    )
    return header + b"".join(chunks)


def test_truncate_clamps_declared_size_and_drops_only_tail() -> None:
    output = io.BytesIO()

    kept = sparse_image.write_truncated_sparse_image(
        io.BytesIO(_large_super_sparse()), output, max_blocks=4,
    )

    result = output.getvalue()
    (
        _magic, _major, _minor, _fh, _ch, block_size, total_blocks,
        chunk_count, _crc,
    ) = struct.unpack_from("<IHHHHIIII", result, 0)
    assert kept == 4
    assert (block_size, total_blocks) == (4, 4)
    assert chunk_count == 2  # RAW(2) + FILL(2)，尾部 DONT_CARE(2) 被丢弃

    offset = sparse_image.SPARSE_HEADER_SIZE
    first = struct.unpack_from("<HHII", result, offset)
    assert first[0] == sparse_image.CHUNK_TYPE_RAW
    offset += first[3]
    second = struct.unpack_from("<HHII", result, offset)
    assert second[0] == sparse_image.CHUNK_TYPE_FILL


def test_truncate_rejects_data_blocks_beyond_capacity() -> None:
    # 尾部是 FILL 数据块（不是 DONT_CARE），裁剪会丢数据 → 必须拒绝。
    block_size = 4
    chunks = [
        struct.pack("<HHII", sparse_image.CHUNK_TYPE_RAW, 0, 2, 20) + b"WXYZABCD",
        struct.pack(
            "<HHII", sparse_image.CHUNK_TYPE_FILL, 0, 2, 16
        ) + b"\x00\x00\x00\x00",
    ]
    header = struct.pack(
        "<IHHHHIIII",
        sparse_image.SPARSE_MAGIC, 1, 0,
        sparse_image.SPARSE_HEADER_SIZE, sparse_image.CHUNK_HEADER_SIZE,
        block_size, 4, len(chunks), 0,
    )
    source = header + b"".join(chunks)

    with pytest.raises(sparse_image.SparseImageError, match="beyond the partition"):
        sparse_image.write_truncated_sparse_image(
            io.BytesIO(source), io.BytesIO(), max_blocks=3,
        )


def test_truncate_rejects_max_blocks_larger_than_image() -> None:
    with pytest.raises(sparse_image.SparseImageError, match="out of range"):
        sparse_image.write_truncated_sparse_image(
            io.BytesIO(_large_super_sparse()), io.BytesIO(), max_blocks=10,
        )
