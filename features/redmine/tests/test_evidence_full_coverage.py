"""Regressions for lost log tails, old index budgets and read coordinates."""

import bz2
import gzip
import io
import lzma
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from features.redmine.evidence import split_zip_derived_text
from features.redmine.evidence_archives import extract_system_archive_text
from features.redmine.evidence_search_api import _search_all_evidence
from features.redmine.evidence_store import EvidenceStore
from features.redmine.evidence_text import derive_text_file
from features.redmine.evidence_zip import extract_zip_derived_text


def _archive_artifact(store, snapshot, source):
    artifact = store.create_artifact(
        snapshot_id=snapshot["snapshot_id"], attachment_id="42", filename=source.name,
        original_filename=source.name, content_type="application/zip", kind="archive",
    )
    relative = source.relative_to(store.evidence_dir).as_posix()
    store.update_artifact(artifact["artifact_id"], status="ready", stored_path=relative)
    return artifact


def test_zip_over_old_member_frame_and_total_limits_has_searchable_tails(tmp_path):
    store = EvidenceStore(tmp_path)
    snapshot = store.create_snapshot(issue_id=661066, download_policy="all")
    source = store.snapshot_dir(snapshot) / "attachments" / "art_zip" / "logs.zip"
    source.parent.mkdir(parents=True)
    prefix = "普通日志\r\n" + "x" * (17 * 1024 * 1024) + "\r\n"
    marker = "lowmemorykiller lmkd killed CtsPerfettoTestCases"
    names = ["inv_mcts/DLTVG0~D.TXT", "inv_static_xts/DULOMV~V.TXT"]
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, prefix + marker + "\r\n")
        for index in range(55):
            archive.writestr(f"other/{index}.txt", f"log {index}")
    artifact = _archive_artifact(store, snapshot, source)
    raw_read = zipfile.ZipExtFile.read

    def bounded_read(handle, size=-1):
        assert 0 <= size <= 256 * 1024, "derivation must not load whole members"
        return raw_read(handle, size)

    with patch.object(zipfile.ZipExtFile, "read", bounded_read), patch.object(
        Path, "read_bytes", side_effect=AssertionError("whole-file reads are forbidden"),
    ):
        derived, note = extract_zip_derived_text(
            store, None, artifact["artifact_id"], f"661066/{snapshot['snapshot_id']}", source,
        )
    assert not note
    store.update_artifact(artifact["artifact_id"], derived_text_path=derived, derived_status="ready")
    data = _search_all_evidence(store, snapshot, ["lmkd", "missing signature"], 1, 0)
    assert data["index_complete"] and data["coverage_complete"]
    assert data["total_bytes"] > 32 * 1024 * 1024
    assert data["scanned_bytes"] == data["total_bytes"]
    row, absent = data["queries"]
    assert row["total"] == 2 and absent["total"] == 0
    assert row["next_offset"] == 1
    hit = row["matches"][0]
    assert hit["line"] == 3 and hit["zip_member"] == names[0]
    with store.resolve_internal(derived).open(encoding="utf-8", newline="") as handle:
        handle.read(hit["artifact_char_index"])
        assert handle.read(4) == "lmkd"
    page = _search_all_evidence(store, snapshot, ["lmkd"], 1, 1)["queries"][0]
    assert page["matches"][0]["zip_member"] == names[1] and page["next_offset"] is None


@pytest.mark.parametrize("compression", ["tar", "gzip", "bz2", "xz"])
def test_compressed_log_tails_are_indexed(compression, tmp_path):
    payload = "x" * (2 * 1024 * 1024 + 3) + "\n尾部 lmkd\n"
    source = tmp_path / f"logs.{compression}"
    if compression == "tar":
        with tarfile.open(source, "w") as archive:
            info = tarfile.TarInfo("DULOMV~V.TXT")
            raw = payload.encode()
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    else:
        compressor = {"gzip": gzip, "bz2": bz2, "xz": lzma}[compression]
        with compressor.open(source, "wb") as handle:
            handle.write(payload.encode())
    store = EvidenceStore(tmp_path / "owner")
    derived, note = extract_system_archive_text(store, None, "art_test", "1/snap", source)
    assert not note
    members = split_zip_derived_text(store.resolve_internal(derived).read_text())
    assert len(members) == 1 and members[0][1] == payload


def test_large_non_utf8_text_keeps_encoded_tail(tmp_path):
    store = EvidenceStore(tmp_path / "owner")
    source = tmp_path / "large.txt"
    with source.open("wb") as handle:
        for _ in range(65):
            handle.write(b"x" * (1024 * 1024))
        handle.write("\n低内存 lmkd".encode("gb18030"))
    derived, note = derive_text_file(store, "art_test", "1/snap", source)
    assert not note and derived
    with store.resolve_internal(derived).open("rb") as handle:
        handle.seek(-len("低内存 lmkd".encode()), 2)
        assert handle.read().decode() == "低内存 lmkd"


def test_search_repairs_legacy_skipped_member_index_from_saved_original(tmp_path):
    store = EvidenceStore(tmp_path)
    snapshot = store.create_snapshot(issue_id=1, download_policy="all")
    source = store.snapshot_dir(snapshot) / "attachments" / "art_zip" / "logs.zip"
    source.parent.mkdir(parents=True)
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("DLTVG0~D.TXT", "lmkd killed target")
    artifact = _archive_artifact(store, snapshot, source)
    store.update_artifact(artifact["artifact_id"], derived_status="missing",
                          error="跳过 1 个超阈值 zip 成员，检索索引不完整")
    store.update_snapshot(snapshot["snapshot_id"], error_json=[
        {"stage": "derive", "message": "attachment 42: 跳过 1 个超阈值 zip 成员"},
    ])
    data = _search_all_evidence(store, snapshot, ["lmkd"], 10, 0)
    assert data["index_complete"] and data["queries"][0]["total"] == 1
    assert not store.get_snapshot(snapshot["snapshot_id"])["errors"]


def test_legacy_index_search_does_not_stop_at_one_megabyte(tmp_path):
    store = EvidenceStore(tmp_path)
    snapshot = store.create_snapshot(issue_id=1)
    source = store.resolve_internal("legacy.txt")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("<<<zip-member:device.log>>>\n" + "x" * (2 * 1024 * 1024) + "\nlmkd\n")
    artifact = _archive_artifact(store, snapshot, source)
    store.update_artifact(artifact["artifact_id"], derived_text_path="legacy.txt")
    data = _search_all_evidence(store, snapshot, ["lmkd"], 10, 0)
    assert data["index_complete"] and data["queries"][0]["total"] == 1
    hit = data["queries"][0]["matches"][0]
    assert "zip_member" not in hit and hit["artifact_char_index"] > 1024 * 1024


@pytest.mark.asyncio
async def test_download_stream_exceeds_former_256_mib_limit_without_losing_tail(tmp_path):
    import hashlib

    from features.redmine.evidence_fetch import EvidenceFetcher

    block = b"x" * (256 * 1024)
    tail = b"lmkd at the end"
    digest = hashlib.sha256()
    expected = hashlib.sha256()

    class Content:
        async def iter_chunked(self, _size):
            for _ in range(1025):
                expected.update(block)
                yield block
            expected.update(tail)
            yield tail

    class Response:
        content = Content()

    source = tmp_path / "complete.part"
    fetcher = EvidenceFetcher.__new__(EvidenceFetcher)
    size, header = await fetcher._stream_to_disk(Response(), source, digest)
    assert size == 1025 * len(block) + len(tail) and size > 256 * 1024 * 1024
    assert header == block[:8] and digest.digest() == expected.digest()
    with source.open("rb") as handle:
        handle.seek(-len(tail), 2)
        assert handle.read() == tail
