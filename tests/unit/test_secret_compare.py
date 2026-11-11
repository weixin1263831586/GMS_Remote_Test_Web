"""secrets_equal accepts ASCII and non-ASCII without raising."""

import os
from types import SimpleNamespace
from unittest.mock import patch

from features.auth.request_security import bootstrap_token_matches
from foundation.security import secrets_equal


def test_secrets_equal_matches_ascii_and_unicode():
    assert secrets_equal('token', 'token')
    assert secrets_equal('密码', '密码')
    assert secrets_equal('', '')
    assert not secrets_equal('token', 'token ')
    assert not secrets_equal('密码', '口令')
    assert not secrets_equal('ascii', '密码')


def test_bootstrap_token_non_ascii_is_a_mismatch_not_an_error():
    connection = SimpleNamespace(headers={'x-gms-bootstrap-token': '密码'})
    with patch.dict(os.environ, {'GMS_BOOTSTRAP_TOKEN': 'bootstrap-token-value-0123456789'}):
        assert bootstrap_token_matches(connection) is False
    connection.headers['x-gms-bootstrap-token'] = '密码'
    with patch.dict(os.environ, {'GMS_BOOTSTRAP_TOKEN': '密码'}):
        assert bootstrap_token_matches(connection) is True
