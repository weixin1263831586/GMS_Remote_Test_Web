"""Digest-pinned cache hits and corrupt-download preservation."""

import hashlib
import io
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from worker_agent.config import WorkerConfig
from worker_agent.suite_actions import execute_suite_action


def config(tmp_path):
    root = tmp_path / 'suites'
    root.mkdir()
    return WorkerConfig(worker_id='w', controller_url='https://controller', token='private-token', suite_roots=[root])


def test_verified_cache_hit_avoids_all_network_calls(tmp_path):
    worker = config(tmp_path)
    archive = worker.suite_roots[0] / 'cts.zip'
    archive.write_bytes(b'known archive')
    payload = {'action': 'download_url', 'url': 'https://example.com/cts.zip', 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    with patch('worker_agent.suite_actions.open_outbound', side_effect=AssertionError('network should not be used')):
        result = execute_suite_action(worker, payload)
    assert result['cache_hit'] is True


def test_download_digest_mismatch_preserves_previous_cache_and_removes_temp(tmp_path):
    worker = config(tmp_path)
    archive = worker.suite_roots[0] / 'cts.zip'
    archive.write_bytes(b'old archive')

    @contextmanager
    def response(*args, **kwargs):
        stream = io.BytesIO(b'corrupt new archive')
        stream.headers = {}
        yield stream

    with patch('worker_agent.suite_actions.resolve_outbound_target'), patch('worker_agent.suite_actions.open_outbound', response), pytest.raises(ValueError, match='SHA-256 mismatch'):
        execute_suite_action(worker, {'action': 'download_url', 'url': 'https://example.com/cts.zip', 'sha256': '0' * 64})
    assert archive.read_bytes() == b'old archive'
    assert not list(worker.suite_roots[0].glob('*.part'))


def test_cache_rejects_symlinks(tmp_path):
    worker = config(tmp_path)
    outside = tmp_path / 'outside.zip'
    outside.write_bytes(b'other data')
    (worker.suite_roots[0] / 'cts.zip').symlink_to(outside)
    with pytest.raises(ValueError, match='symlink'):
        execute_suite_action(worker, {'action': 'download_url', 'url': 'https://example.com/cts.zip', 'sha256': '0' * 64})
