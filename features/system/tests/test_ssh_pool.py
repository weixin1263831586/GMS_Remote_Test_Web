import asyncio
from unittest.mock import MagicMock, patch

from features.system.ssh import SSHManager
from features.system.ssh_async import SSHAsyncManager


def test_connection_pool_does_not_reuse_connection_for_another_host():
    manager = SSHManager(pool_size=2)
    pooled = MagicMock()
    pooled._gms_pool_identity = ('host-a', '22', 'tester')
    manager.return_connection(pooled)

    replacement = MagicMock()
    with patch.object(manager, 'create_connection', return_value=replacement) as create:
        result = manager.get_connection({'host': 'host-b', 'username': 'tester', 'password': 'secret'})

    assert result is replacement
    pooled.close.assert_called_once_with()
    pooled.exec_command.assert_not_called()
    create.assert_called_once()


def test_connection_pool_reuses_matching_healthy_connection():
    manager = SSHManager(pool_size=2)
    pooled = MagicMock()
    pooled._gms_pool_identity = ('host-a', '22', 'tester')
    stdout = MagicMock()
    stdout.channel.recv_exit_status.return_value = 0
    pooled.exec_command.return_value = (MagicMock(), stdout, MagicMock())
    manager.return_connection(pooled)

    with patch.object(manager, 'create_connection') as create:
        result = manager.get_connection({'host': 'host-a', 'username': 'tester', 'password': 'secret'})

    assert result is pooled
    create.assert_not_called()


def test_key_auth_falls_back_to_agent_and_default_keys_when_configured_key_is_missing():
    manager = SSHManager(pool_size=2)
    client = MagicMock()

    with (
        patch('features.system.ssh.paramiko.SSHClient', return_value=client),
        patch('features.system.ssh.configure_strict_host_keys'),
        patch.object(manager, '_load_ssh_key', return_value=None),
    ):
        result = manager.create_connection({
            'host': 'host-a',
            'username': 'tester',
            'use_key_auth': True,
            'private_key_path': '/missing/gms_web_app_rsa',
        })

    assert result is client
    client.connect.assert_called_once_with(
        'host-a',
        port=22,
        username='tester',
        timeout=10,
        banner_timeout=10,
        auth_timeout=10,
        allow_agent=True,
        look_for_keys=True,
    )
    assert client._gms_pool_identity == ('host-a', '22', 'tester')


def test_async_connections_are_scoped_by_username():
    manager = SSHAsyncManager()
    first = MagicMock()
    second = MagicMock()

    with patch('features.system.ssh_async.paramiko.SSHClient', side_effect=[first, second]):
        alice = asyncio.run(manager.connect('host-a', 'alice', 'pw-a'))
        bob = asyncio.run(manager.connect('host-a', 'bob', 'pw-b'))

    assert alice is first
    assert bob is second
    assert len(manager.connections) == 2
