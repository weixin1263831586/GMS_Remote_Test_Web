import asyncio
from unittest.mock import MagicMock, patch

from features.system.ssh import SSHManager
from features.system.ssh_async import SSHAsyncManager


def test_connection_pool_does_not_reuse_connection_for_another_host():
    manager = SSHManager(pool_size=2)
    pooled = MagicMock()
    pooled._gms_pool_identity = ('host-a', 'tester')
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
    pooled._gms_pool_identity = ('host-a', 'tester')
    stdout = MagicMock()
    stdout.channel.recv_exit_status.return_value = 0
    pooled.exec_command.return_value = (MagicMock(), stdout, MagicMock())
    manager.return_connection(pooled)

    with patch.object(manager, 'create_connection') as create:
        result = manager.get_connection({'host': 'host-a', 'username': 'tester', 'password': 'secret'})

    assert result is pooled
    create.assert_not_called()


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
