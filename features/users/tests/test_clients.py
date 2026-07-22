import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.users import clients


class _ConfigManager:
    def __init__(self, trusted_proxies=None):
        self.trusted_proxies = trusted_proxies

    def load_config(self):
        if self.trusted_proxies is None:
            return {}
        return {'trusted_proxies': self.trusted_proxies}


def _request(peer, headers=None):
    return SimpleNamespace(
        client=SimpleNamespace(host=peer),
        headers=headers or {},
    )


class ClientIpResolutionTests(unittest.TestCase):
    def test_direct_client_cannot_spoof_forwarded_headers(self):
        request = _request(
            '192.0.2.10',
            {'X-Forwarded-For': '198.51.100.20', 'X-Real-IP': '198.51.100.21'},
        )
        with patch.object(clients.runtime, 'config_manager', _ConfigManager()):
            resolved = clients.get_client_ip(request)

        self.assertEqual(resolved, '192.0.2.10')

    def test_trusted_proxy_uses_nearest_untrusted_forwarded_address(self):
        request = _request(
            '127.0.0.1',
            {'X-Forwarded-For': '198.51.100.99, 203.0.113.8, 127.0.0.2'},
        )
        with patch.object(clients.runtime, 'config_manager', _ConfigManager()):
            resolved = clients.get_client_ip(request)

        self.assertEqual(resolved, '203.0.113.8')

    def test_configured_proxy_network_is_honored(self):
        request = _request('10.0.0.5', {'X-Real-IP': '192.0.2.30'})
        with patch.object(
            clients.runtime,
            'config_manager',
            _ConfigManager(['10.0.0.0/24']),
        ):
            resolved = clients.get_client_ip(request)

        self.assertEqual(resolved, '192.0.2.30')


if __name__ == '__main__':
    unittest.main()
