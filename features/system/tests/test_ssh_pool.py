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


def test_pool_health_check_matches_real_paramiko_contract():
    """P1 回归：健康检查不能给 recv_exit_status() 传 timeout。

    paramiko.Channel.recv_exit_status() 只接受 self；历史代码传了
    ``timeout=2``，真实连接上抛 TypeError 被当作死连接关闭，连接池
    复用路径永远失败。MagicMock 接受任意参数掩盖了该 bug——本测试
    用 spec=paramiko.Channel 绑定真实签名。
    """
    import paramiko

    manager = SSHManager(pool_size=2)
    pooled = MagicMock()
    pooled._gms_pool_identity = ('host-a', '22', 'tester')
    channel = MagicMock(spec=paramiko.Channel)
    channel.recv_exit_status.return_value = 0
    stdout = MagicMock()
    stdout.channel = channel
    pooled.exec_command.return_value = (MagicMock(), stdout, MagicMock())
    manager.return_connection(pooled)

    with patch.object(manager, 'create_connection') as create:
        result = manager.get_connection({'host': 'host-a', 'username': 'tester', 'password': 'secret'})

    # 复用成功且健康检查按真实 API 调用（无 timeout 参数）。
    assert result is pooled
    create.assert_not_called()
    channel.settimeout.assert_called_once_with(2.0)
    channel.recv_exit_status.assert_called_once_with()


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


def test_stream_reading_uses_stderr_api_for_stderr():
    """P1 回归：stderr 流必须走 recv_stderr/recv_stderr_ready。

    历史 _read_stream 无论 stdout/stderr 都读 channel.recv()，两个读取
    任务争抢同一个 stdout channel，stderr 日志错乱/丢失。
    """
    import paramiko

    from features.system.ssh_executor import ssh_executor

    channel = MagicMock(spec=paramiko.Channel)
    # ready 一次取数据，之后不再 ready；进程已退出（exit ready 恒真）。
    channel.exit_status_ready.return_value = True
    channel.recv_ready.return_value = False
    channel.recv_stderr_ready.side_effect = [True, False]
    channel.recv_stderr.return_value = b"boom\n"
    stderr = MagicMock()
    stderr.channel = channel
    stdout = MagicMock()
    stdout.channel = channel

    ssh = MagicMock()
    ssh.exec_command.return_value = (MagicMock(), stdout, stderr)

    logged = []

    async def capture_log(line, level):
        logged.append((line, level))

    async def run():
        return await ssh_executor.run_stream(
            ssh, "true", capture_log, timeout=5,
        )

    result = asyncio.run(run())
    channel.recv_stderr_ready.assert_called()
    channel.recv_stderr.assert_called_with(65536)
    channel.recv.assert_not_called()
    assert ("boom", "error") in logged
    assert result.stderr == "boom"


def test_stream_reading_uses_stdout_api_for_stdout():
    import paramiko

    from features.system.ssh_executor import ssh_executor

    channel = MagicMock(spec=paramiko.Channel)
    channel.exit_status_ready.return_value = True
    channel.recv_ready.side_effect = [True, False]
    channel.recv.return_value = b"hello\n"
    channel.recv_stderr_ready.return_value = False
    stdout = MagicMock()
    stdout.channel = channel
    stderr = MagicMock()
    stderr.channel = channel

    ssh = MagicMock()
    ssh.exec_command.return_value = (MagicMock(), stdout, stderr)

    logged = []

    async def capture_log(line, level):
        logged.append((line, level))

    async def run():
        return await ssh_executor.run_stream(
            ssh, "true", capture_log, timeout=5,
        )

    result = asyncio.run(run())
    channel.recv.assert_called_with(65536)
    channel.recv_stderr.assert_not_called()
    assert ("hello", "info") in logged
    assert result.stdout == "hello"


def test_simple_exec_drains_output_before_exit_status():
    """P1 回归：大输出时先 recv_exit_status 可能永久等待（Paramiko
    官方警告），必须先 drain stdout/stderr 再取退出码。"""
    import features.system.ssh_async as mod

    calls = []

    class FakeStream:
        def __init__(self, name, channel):
            self._name = name
            self.channel = channel

        def read(self):
            calls.append(f"read:{self._name}")
            return b"out" if self._name == "stdout" else b"err"

    class FakeChannel:
        def recv_exit_status(self):
            calls.append("exit_status")
            return 0

    ssh = MagicMock()
    channel = FakeChannel()
    ssh.exec_command.return_value = (
        MagicMock(), FakeStream("stdout", channel), FakeStream("stderr", channel),
    )

    async def run():
        with patch.object(
            mod.SSHAsyncManager, 'connect', return_value=ssh,
        ):
            return await mod.SSHAsyncManager().execute_command_simple(
                'host-a', 'tester', 'pw', 'true',
            )

    result = asyncio.run(run())
    assert result.code == 0
    assert result.stdout == "out" and result.stderr == "err"
    assert calls == ["read:stdout", "read:stderr", "exit_status"]
