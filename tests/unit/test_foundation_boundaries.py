"""Regression coverage for shared validation and persistence boundaries."""

import sqlite3
from unittest.mock import Mock

import pytest

from foundation.common_utils import CommonUtils
from foundation.database import connect_sqlite
from foundation.job_env import filter_job_env
from foundation.security import is_safe_device_id, sanitize_device_ids
from foundation.ssh import decode_ssh_output


@pytest.mark.parametrize('suffix', ['\n', '\r', '\r\n', '\x00', '\t', ' '])
def test_device_ids_reject_trailing_whitespace_and_controls(suffix):
    assert not is_safe_device_id('serial' + suffix)
    assert sanitize_device_ids(['serial' + suffix, 'serial']) == ['serial']


@pytest.mark.parametrize('value', ['x\n', '\n', 'x\r', 'x\r\n', 'x\x00'])
def test_job_environment_rejects_line_breaks_and_nul(value):
    assert filter_job_env({'TERM': value}) == ({}, ['TERM'])
    assert filter_job_env({'TERM': 'xterm'}) == ({'TERM': 'xterm'}, [])


def test_sqlite_connection_closed_when_initialization_fails(tmp_path, monkeypatch):
    connection = Mock()
    connection.execute.side_effect = sqlite3.OperationalError('database is locked')
    monkeypatch.setattr('foundation.database.sqlite3.connect', lambda *args, **kwargs: connection)
    with pytest.raises(sqlite3.OperationalError), connect_sqlite(tmp_path / 'state.sqlite3'):
        pytest.fail('failed initialization must not yield a connection')
    connection.close.assert_called_once_with()


@pytest.mark.parametrize('data', [b'abc', '中文'.encode('gbk'), b'\xff'])
def test_ssh_output_compatibility_uses_canonical_decoder(data):
    assert CommonUtils.decode_ssh_output is decode_ssh_output
    assert CommonUtils.decode_ssh_output(data) == decode_ssh_output(data)


@pytest.mark.parametrize('origin', [
    'https://gms.example/', 'https://gms.example?x=1', 'https://gms.example#section',
    'https://user@gms.example', 'https://gms.example:bad', 'https://*.example',
    'http://gms.example', 'https://gms.example:0', 'https://gms.example\t',
])
def test_production_origin_must_match_browser_origin(origin):
    from bootstrap.production_security import _validate_https_origin

    with pytest.raises(RuntimeError, match='GMS_ALLOWED_ORIGINS'):
        _validate_https_origin(origin)


@pytest.mark.parametrize('origin', ['https://gms.example', 'https://gms.example:5001', 'https://[::1]:5001'])
def test_production_accepts_exact_https_origin(origin):
    from bootstrap.production_security import _validate_https_origin

    _validate_https_origin(origin)
