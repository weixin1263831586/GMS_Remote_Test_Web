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
from features.redmine.evidence_zip import extract_zip_derived_text


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


def test_member_budget_includes_frame_headers_and_newlines(tmp_path):
    import io
    import json

    store = EvidenceStore(tmp_path)
    # 首成员占 35 字符；剩余 2 字符能容纳 b 的正文，放不下其帧头。
    with patch("features.redmine.evidence_text.DERIVED_TEXT_MAX_CHARS", 37):
        derived, note = derive_member_index(store, "art_frames", "1/s1", [
            ("a.txt", lambda: io.BytesIO(b"aaaa")),
            ("b.txt", lambda: io.BytesIO(b"b")),
        ])
    assert derived and "预算" in note
    path = store.resolve_internal(derived)
    text = path.read_text(encoding="utf-8")
    assert len(text) == 35
    sidecar = json.loads(path.with_name(path.name + ".idx.json").read_text())
    assert sidecar["total_chars"] == len(text)
    assert [member["name"] for member in sidecar["members"]] == ["a.txt"]


def test_rar7z_extraction_runs_under_a_time_budget(tmp_path):
    store = EvidenceStore(tmp_path)
    source = tmp_path / "evil.7z"
    source.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 16)
    with patch(
        "features.redmine.evidence_archives.extract_system_archive"
    ) as extract:
        extract_system_archive_text(store, "art3", "1/s1", source)
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


def _counting_opener_log(opener):
    """包装 opener：记录实际从解压流读出的字节数（预算封顶观测点）。"""
    reads: list[int] = []

    def wrapped():
        stream = opener()

        class _Counting:
            def read(self, n=-1):
                data = stream.read(n)
                reads.append(len(data))
                return data

            def seek(self, *args):
                return stream.seek(*args)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return stream.__exit__(*exc)

        return _Counting()

    return wrapped, reads


def test_encoding_prescan_is_bounded_by_char_budget(tmp_path):
    """编码预扫描与派生共用同一字符预算（2026-11-14 审核意见）。

    旧实现 detect_stream_encoding 先全量扫描整个成员（1MB 有效 UTF-8
    也全读全解码），预算拦得住写入、拦不住预扫描的解压 CPU。新实现
    单遍解码触顶即停：1MB 成员 + 10 字符预算，实际读取远小于成员全量。
    """
    import io

    payload = b"a" * 1_000_000
    wrapped, reads = _counting_opener_log(lambda: io.BytesIO(payload))
    with patch("features.redmine.evidence_text.DERIVED_TEXT_MAX_CHARS", 10):
        derived, note = derive_member_index(
            EvidenceStore(tmp_path), "art_scan2", "1/s1",
            [("big.txt", wrapped)],
        )
    assert not derived and "超出" in note
    total_read = sum(reads)
    # 旧实现两次全量扫描 = 2MB；新实现第一块 256KB 内触顶即停。
    assert total_read < 400_000, total_read


def test_zip_declared_size_pre_rejection(tmp_path):
    """ZIP 声明大小超预算的成员不解压（元数据提前拒绝）。"""
    store = EvidenceStore(tmp_path)
    snapshot = store.create_snapshot(issue_id=1, download_policy="all")
    snapshot_rel = f"1/{snapshot['snapshot_id']}"
    source = tmp_path / "mix.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("small.txt", "ok-text")
        archive.writestr("bomb.txt", "z" * 500)
    with zipfile.ZipFile(source) as archive, patch(
        "features.redmine.evidence_zip.DERIVED_TEXT_MAX_CHARS", 100
    ):
        derived, note = extract_zip_derived_text(
            store, "art_zip", snapshot_rel, source
        )
    assert derived and "ok-text" in store.resolve_internal(derived).read_text(encoding="utf-8")
    assert ("声明大小" in note and "bomb.txt" not in note.split("；")[-1]) or "1 个成员" in note
    # 纯炸弹包：没有任何成员可提取时也要显性化，不能返回"干净空"。
    source2 = tmp_path / "bomb.zip"
    with zipfile.ZipFile(source2, "w") as archive:
        archive.writestr("bomb.txt", "z" * 500)
    with zipfile.ZipFile(source2) as archive, patch(
        "features.redmine.evidence_zip.DERIVED_TEXT_MAX_CHARS", 100
    ):
        derived2, note2 = extract_zip_derived_text(
            store, "art_zip2", snapshot_rel, source2
        )
    assert not derived2 and "声明大小" in note2


def test_gb18030_member_survives_candidate_retry(tmp_path):
    """候选编码重试后 spool 必须干净：GB18030 成员全文正确落盘。"""
    import io

    store = EvidenceStore(tmp_path)
    snapshot = store.create_snapshot(issue_id=1, download_policy="all")
    snapshot_rel = f"1/{snapshot['snapshot_id']}"
    text = ("复现步骤：连接设备后触发异常。" * 20)
    payload = text.encode("gb18030")
    derived, note = derive_member_index(store, "art_gb", snapshot_rel, [
        ("cn.txt", lambda: io.BytesIO(payload)),
    ])
    assert derived and not note
    body = store.resolve_internal(derived).read_text(encoding="utf-8")
    assert "复现步骤：连接设备后触发异常。" in body
    sidecar = store.resolve_internal(derived)
    index = sidecar.with_name(sidecar.name + ".idx.json")
    assert index.is_file()
