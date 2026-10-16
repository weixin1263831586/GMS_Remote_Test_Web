import socket
from unittest.mock import patch

import pytest

from worker_agent.app import _validate_usbip_source_host
from worker_agent.device_actions import browse_directory, copy_image_into
from worker_agent.runtime import _validated_job_path_segment


@pytest.mark.parametrize('segment', ['../escape', '/tmp/escape', '..', '.', 'a/b', 'a\\b', ''])
def test_job_identifiers_cannot_escape_the_work_directory(segment):
    with pytest.raises(ValueError):
        _validated_job_path_segment(segment, 'job_id')


def test_file_operations_reject_symlinks_outside_worker_roots(tmp_path):
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    outside = tmp_path / 'private'
    outside.mkdir()
    (outside / 'secret.img').write_bytes(b'secret')
    (allowed / 'escape').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='outside'):
        browse_directory(str(allowed / 'escape'), allowed_roots=[allowed])
    with pytest.raises(ValueError, match='outside'):
        copy_image_into(allowed / 'escape/secret.img', allowed / 'copied.img', 100, allowed_roots=[allowed])
    assert not (allowed / 'copied.img').exists()


@pytest.mark.parametrize('address', ['127.0.0.1', '169.254.169.254', '10.0.0.1', '100.64.0.1', '::1'])
def test_usbip_probe_rejects_unapproved_local_addresses(address, monkeypatch):
    monkeypatch.delenv('GMS_WORKER_USBIP_ALLOWED_HOSTS', raising=False)
    with patch('worker_agent.app.socket.getaddrinfo', return_value=[(socket.AF_INET, 1, 6, '', (address, 0))]), \
            pytest.raises(ValueError, match='forbidden'):
        _validate_usbip_source_host('peer.example')


def test_usbip_probe_returns_operator_authorized_private_ip(monkeypatch):
    monkeypatch.setenv('GMS_WORKER_USBIP_ALLOWED_HOSTS', 'peer.example')
    with patch('worker_agent.app.socket.getaddrinfo', return_value=[(socket.AF_INET, 1, 6, '', ('10.0.0.2', 0))]):
        assert _validate_usbip_source_host('peer.example') == '10.0.0.2'
