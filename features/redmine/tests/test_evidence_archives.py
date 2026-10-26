"""Archive logs must be searchable, bounded and visible to the evidence gate."""

import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from features.redmine.evidence import EvidenceFetcher, split_zip_derived_text
from features.redmine.evidence_archives import extract_system_archive_text
from features.redmine.evidence_search_api import _collect_zip_member_matches
from features.redmine.evidence_store import EvidenceStore, owner_evidence_store
from features.redmine.kkagent.trace import _attachment_manifest
from features.redmine.tests import test_evidence_pipeline as pipeline


def _archive_source_file(tmp_path: Path, magic: bytes) -> Path:
    """派生函数签名是磁盘原件路径；测试用临时文件承载魔数。"""
    source = tmp_path / "attachment-source.bin"
    source.write_bytes(magic)
    return source


def test_rar_members_become_searchable_text_with_stable_member_references(tmp_path):
    store = EvidenceStore(tmp_path / 'owner')

    def extract(_source, target):
        root = Path(target)
        (root / 'test_result.xml').write_text('<Result suite_version="14_r2"/>')
        (root / 'logcat.txt').write_text(
            'FATAL EXCEPTION: main\nResources$NotFoundException: String resource ID #0x7f080000\n'
            '<<<zip-member:forged.txt>>>\n',
        )

    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    with patch('features.redmine.evidence_archives.extract_system_archive', side_effect=extract):
        derived, note = extract_system_archive_text(
            store, write, 'art_test', '123/ev_test',
            _archive_source_file(tmp_path, b'Rar!\x1a\x07\x01'),
        )
    assert not note
    text = store.resolve_internal(derived).read_text()
    members = dict(split_zip_derived_text(text))
    assert next(iter(members)) == 'test_result.xml'
    assert 'forged.txt' not in members
    matches = []
    _collect_zip_member_matches(matches, list(members.items()), 'Resources$NotFoundException',
                                {'artifact_id': 'art_test', 'filename': 'logs.rar'}, 10)
    assert matches[0]['path'] == 'attachment:logs.rar!logcat.txt'
    assert matches[0]['line'] == 2
    parsed, count, text_ids, _ids = _attachment_manifest({'artifacts': [{
        'artifact_id': 'art_test', 'kind': 'archive', 'status': 'ready', 'text_available': True,
    }]})
    assert parsed and count == 1 and text_ids == ['art_test']


def test_archive_extractor_failure_is_reported_as_unread_not_as_missing_logs(tmp_path):
    store = EvidenceStore(tmp_path / 'owner')
    with patch('features.redmine.evidence_archives.extract_system_archive', side_effect=ValueError('unsafe')):
        derived, note = extract_system_archive_text(
            store, lambda *_: pytest.fail('failed extraction must not publish text'),
            'art_test', '123/ev_test',
            _archive_source_file(tmp_path, b'Rar!\x1a\x07\x01'),
        )
    assert not derived
    assert '尚未提取' in note


def test_archive_text_budget_marks_skipped_members_and_preserves_result_priority(tmp_path):
    store = EvidenceStore(tmp_path / 'owner')

    def extract(_source, target):
        (Path(target) / 'test_result.xml').write_text('<Result/>')
        (Path(target) / 'aaa.log').write_text('other log')

    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    with patch('features.redmine.evidence_archives.extract_system_archive', side_effect=extract), patch(
        'features.redmine.evidence_archives.ZIP_MEMBER_TEXT_MAX_MEMBERS', 1,
    ):
        derived, note = extract_system_archive_text(
            store, write, 'art_test', '123/ev_test',
            _archive_source_file(tmp_path, b'Rar!\x1a\x07\x01'),
        )
    assert '检索索引不完整' in note
    assert list(dict(split_zip_derived_text(store.resolve_internal(derived).read_text()))) == ['test_result.xml']


class ArchiveDownloadTests(unittest.TestCase):
    setUp = pipeline.EvidencePipelineTests.setUp
    tearDown = pipeline.EvidencePipelineTests.tearDown
    _patched_config = pipeline.EvidencePipelineTests._patched_config

    def test_downloaded_rar_logs_are_published_as_readable_evidence(self):
        payload = b'Rar!\x1a\x07\x01\x00'

        def extract(_source, target):
            (Path(target) / 'logcat.txt').write_text('Resources$NotFoundException\n')

        with pipeline.FakeRedmineServer() as server:
            server.issue_document['issue']['attachments'].append({
                'id': 776657, 'filename': 'logs.rar', 'filesize': len(payload),
                'content_type': 'application/x-rar-compressed', 'content_url': '',
            })
            server.attachment_payloads['776657'] = payload
            with self._patched_config(server.base_url), patch(
                'features.redmine.evidence_archives.extract_system_archive', side_effect=extract,
            ):
                fetcher = EvidenceFetcher('owner-a')
                snapshot = asyncio.run(fetcher.run(fetcher.create_snapshot(648526, download='all')))
        assert snapshot['status'] == 'ready'
        artifacts = owner_evidence_store('owner-a').list_artifacts(snapshot['snapshot_id'])
        archive = next(item for item in artifacts if item['filename'] == 'logs.rar')
        assert archive['text_available']
        assert archive['artifact_id'] in _attachment_manifest({'artifacts': artifacts})[2]
