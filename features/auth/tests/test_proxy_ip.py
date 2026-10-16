from types import SimpleNamespace
from unittest.mock import patch

import pytest

from features.auth.api import _request_source_ip


@pytest.mark.parametrize('peer,forwarded,expected', [
    ('127.0.0.1', '192.0.2.2', '192.0.2.2'),
    ('127.0.0.1', '192.0.2.3', '192.0.2.3'),
    ('198.51.100.9', '192.0.2.2', '198.51.100.9'),
])
def test_login_ip_binding_and_rate_limits_share_trusted_proxy_resolution(peer, forwarded, expected):
    request = SimpleNamespace(client=SimpleNamespace(host=peer), headers={'X-Forwarded-For': forwarded})
    with patch('foundation.networking._load_trusted_proxies_config', return_value=['127.0.0.1/32']):
        assert _request_source_ip(request) == expected
