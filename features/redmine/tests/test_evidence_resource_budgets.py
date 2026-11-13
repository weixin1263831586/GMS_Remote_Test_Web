"""资源边界（evidence.py 预算常量）的定向回归。

全文取证能力保留（test_evidence_full_coverage.py 验证大附件仍完整索引），
这里验证触顶行为：磁盘预算、派生字符预算、RAR/7z 进程时长预算、OCR
时长预算全部显性化——未覆盖的部分绝不能当作不存在。
"""

from __future__ import annotations

import zipfile
from unittest.mock import Mock, patch

import pytest

from features.redmine.evidence import (
    DERIVED_TEXT_MAX_CHARS,
    EXTRACT_TIMEOUT_SECONDS,
    MAX_ATTACHMENT_BYTES,
    OCR_MAX_IMAGE_PIXELS,
    OCR_TIMEOUT_SECONDS,
    _env_limit,
)
from features.redmine.evidence_archives import extract_system_archive_text
from features.redmine.evidence_store import EvidenceStore
from features.redmine.evidence_text import derive_member_index


def test_resource_budgets_are_env_overridable(monkeypatch):
    monkeypatch.setenv("GMS_EVIDENCE_MAX_ATTACHMENT_BYTES", "1024")
    monkeypatch.setenv("GMS_EVIDENCE_DERIVED_TEXT_MAX_CHARS", "0")
    monkeypatch.setenv("GMS_EVIDENCE_EXTRACT_TIMEOUT_SECONDS", "junk")
    limits = {
        "attachment": _env_limit("GMS_EVIDENCE_MAX_ATTACHMENT_BYTES", 1),
        "derived": _env_limit("GMS_EVIDENCE_DERIVED_TEXT_MAX_CHARS", 1),
        "extract": _env_limit("GMS_EVIDENCE_EXTRACT_TIMEOUT_SECONDS", 1),
    }
    assert limits == {"attachment": 1024, "derived": 1, "extract": 1}


def test_default_budgets_exceed_full_coverage_fixture_sizes():
    # 全覆盖测试构造 256MiB+ 下载与 34MiB 派生文本；默认预算必须更高，
    # 否则正常大附件会被预算截断成 partial。
    assert MAX_ATTACHMENT_BYTES >= 2 * 1024**3
    assert DERIVED_TEXT_MAX_CHARS >= 200_000_000
    assert EXTRACT_TIMEOUT_SECONDS >= 300
    assert OCR_TIMEOUT_SECONDS >= 1
    assert OCR_MAX_IMAGE_PIXELS >= 16_000_000


def test_member_over_derived_text_budget_is_dropped_with_explicit_note(tmp_path):
    store = EvidenceStore(tmp_path)
    snapshot = store.create_snapshot(issue_id=1, download_policy="all")
    snapshot_rel = f"1/{snapshot['snapshot_id']}"
    source = tmp_path / "small.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("a.txt", "first")
    with zipfile.ZipFile(source) as archive, patch("features.redmine.evidence_text.DERIVED_TEXT_MAX_CHARS", 4):
        derived, note = derive_member_index(
            store, "art1", snapshot_rel,
            [("a.txt", lambda: archive.open("a.txt"))],
        )
    assert not derived and "超出" in note and "预算" in note


def test_member_budget_stop_keeps_prefix_partial_and_never_silent(tmp_path):
    store = EvidenceStore(tmp_path)
    snapshot = store.create_snapshot(issue_id=1, download_policy="all")
    snapshot_rel = f"1/{snapshot['snapshot_id']}"
    source = tmp_path / "two.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("a.txt", "aaaa")
        archive.writestr("b.txt", "bbbb")
    with zipfile.ZipFile(source) as archive:
        derived, note = derive_member_index(
            store, "art2", snapshot_rel,
            [
                ("a.txt", lambda: archive.open("a.txt")),
                ("b.txt", lambda: (_ for _ in ()).throw(OSError("boom"))),
            ],
        )
    assert derived and "读取失败" in note  # 读失败显性化，索引 partial
    text = store.resolve_internal(derived).read_text(encoding="utf-8")
    assert "aaaa" in text and "bbbb" not in text


def test_rar7z_extraction_runs_under_a_time_budget(tmp_path):
    store = EvidenceStore(tmp_path)
    source = tmp_path / "evil.7z"
    source.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 16)
    with patch(
        "features.redmine.evidence_archives.extract_system_archive"
    ) as extract:
        extract_system_archive_text(store, None, "art3", "1/s1", source)
    _args, kwargs = extract.call_args
    assert kwargs["timeout"] == EXTRACT_TIMEOUT_SECONDS > 0


def test_attachment_stream_budget_aborts_download(tmp_path):
    import asyncio

    from features.redmine.evidence_fetch import EvidenceFetcher

    class Content:
        async def iter_chunked(self, _size):
            yield b"x" * 16

    class Response:
        content = Content()

    fetcher = EvidenceFetcher.__new__(EvidenceFetcher)
    with patch(
        "features.redmine.evidence_fetch.MAX_ATTACHMENT_BYTES", 8
    ), pytest.raises(Exception, match="预算"):
        asyncio.run(
            fetcher._stream_to_disk(Response(), tmp_path / "out.part", Mock())
        )
    # .part 清理由真实调用方 _download_attachment 的 except 分支负责；
    # 此处只验证下载流在触顶时中止。
