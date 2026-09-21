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


def _patch_config(trusted_proxies=None):
    """Patch the trusted-proxy config seam (now foundation-level).

    features.users.clients delegates get_client_ip to
    foundation.networking, so the config source is patched there.
    """
    return patch(
        'foundation.networking._load_trusted_proxies_config',
        return_value=(
            trusted_proxies if trusted_proxies is None
            else list(trusted_proxies)
        ),
    )


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
        with _patch_config():
            resolved = clients.get_client_ip(request)

        self.assertEqual(resolved, '192.0.2.10')

    def test_trusted_proxy_uses_nearest_untrusted_forwarded_address(self):
        request = _request(
            '127.0.0.1',
            {'X-Forwarded-For': '198.51.100.99, 203.0.113.8, 127.0.0.2'},
        )
        with _patch_config():
            resolved = clients.get_client_ip(request)

        self.assertEqual(resolved, '203.0.113.8')

    def test_configured_proxy_network_is_honored(self):
        request = _request('10.0.0.5', {'X-Real-IP': '192.0.2.30'})
        with _patch_config(['10.0.0.0/24']):
            resolved = clients.get_client_ip(request)

        self.assertEqual(resolved, '192.0.2.30')


class DetectUsernameHostKeyTests(unittest.TestCase):
    """主机密钥校验失败必须映射为明确的信任类错误。

    客户端重装系统后 SSH 主机密钥变化，paramiko 以 BadHostKeyException /
    「not found in known_hosts」失败；此前这类错误不含任何既有映射关键词，
    一路落到通用文案，登录页误报「密码错误」。
    """

    def _detect(self, side_effect):
        from features.users.sessions import ClientManager

        manager = ClientManager()
        manager.config_manager = SimpleNamespace(load_config=lambda: {})
        with patch.object(
            manager, '_ssh_whoami', side_effect=side_effect
        ):
            return manager.detect_username(
                '172.16.14.94', 'qiujian', 'secret'
            )

    def test_bad_host_key_exception_maps_to_trust_error(self):
        import paramiko

        ok, _, error = self._detect(
            paramiko.SSHException("Server '172.16.14.94' not found in known_hosts")
        )

        self.assertFalse(ok)
        self.assertIn('主机密钥', error)
        self.assertIn('known_hosts', error)

    def test_host_key_mismatch_text_maps_to_trust_error(self):
        # BadHostKeyException 的构造需要真实 PKey；其文本形态即 "does not
        # match"，这里用等价文本锚定字符串兜底路径。
        ok, _, error = self._detect(
            RuntimeError("Host key for server '172.16.14.94' does not match")
        )

        self.assertFalse(ok)
        self.assertIn('主机密钥', error)
        self.assertIn('known_hosts', error)

    def test_authentication_failure_keeps_password_hint(self):
        import paramiko

        ok, _, error = self._detect(
            paramiko.AuthenticationException('authentication failed')
        )

        self.assertFalse(ok)
        self.assertIn('用户名和密码', error)


if __name__ == '__main__':
    unittest.main()
