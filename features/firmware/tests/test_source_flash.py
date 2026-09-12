"""Unit tests for the Windows source-side flash dispatcher."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import MagicMock, patch

from features.firmware import source_flash
from features.firmware.source_flash import (
    SourceFlashError,
    enqueue_task,
    run_source_flash,
    sftp_mkdir_chain,
    wait_result,
    windows_exec,
)


def _make_ssh():
    ssh = MagicMock()
    stdout = MagicMock()
    stdout.channel.recv_exit_status.return_value = 0
    stdout.read.return_value = b""
    stderr = MagicMock()
    stderr.read.return_value = b""
    ssh.exec_command.return_value = (MagicMock(), stdout, stderr)
    return ssh, stdout, stderr


def _emulate_channel(stdout_bytes: bytes, stderr_bytes: bytes = b"",
                     exit_code: int = 0) -> MagicMock:
    """Build a channel mock that behaves like a REAL paramiko channel
    under foundation.ssh_executor.run's concurrent drain loop.

    A bare MagicMock makes `recv_ready()` always truthy while `recv()`
    returns a fresh (len==0) MagicMock: `made_progress` stays true forever,
    nothing accumulates — the drain loop spins at 100% CPU allocating new
    mocks until the host runs out of memory. That OOM/hang is exactly the
    "63% … runner shutdown" the CI used to hit. Real semantics: each ready
    flag is one-shot and recv() yields the actual bytes.
    """

    channel = MagicMock()
    out_pending = [bool(stdout_bytes)]
    err_pending = [bool(stderr_bytes)]

    def _recv_ready() -> bool:
        seen, out_pending[0] = out_pending[0], False
        return seen

    def _recv_stderr_ready() -> bool:
        seen, err_pending[0] = err_pending[0], False
        return seen

    channel.recv_ready.side_effect = _recv_ready
    channel.recv.side_effect = [stdout_bytes] if stdout_bytes else []
    channel.recv_stderr_ready.side_effect = _recv_stderr_ready
    channel.recv_stderr.side_effect = [stderr_bytes] if stderr_bytes else []
    channel.exit_status_ready.return_value = True
    channel.recv_exit_status.return_value = exit_code
    return channel


def _ssh_with_channel(stdout_bytes: bytes, stderr_bytes: bytes = b"",
                      exit_code: int = 0) -> tuple[MagicMock, MagicMock]:
    stdout = MagicMock()
    stdout.channel = _emulate_channel(stdout_bytes, stderr_bytes, exit_code)
    ssh = MagicMock()
    ssh.exec_command.return_value = (MagicMock(), stdout, MagicMock())
    return ssh, stdout


class WindowsExecTests(unittest.TestCase):
    def test_combines_stdout_and_stderr(self) -> None:
        ssh, stdout, _stderr = _make_ssh()
        # 统一执行层：windows_exec 委托 foundation.ssh_executor.run，
        # mock 伪造的是 channel 级接口（recv_ready/recv/recv_stderr_*）——
        # executor 并发 drain 这两个流，而不是顺序 file.read()。
        # ready 标志必须"一次性"：executor 在 exit 后还会继续轮询
        # recv_ready()/recv_stderr_ready()，恒真 mock 会把 side_effect
        # 列表耗尽（StopIteration）并让整条命令报错。
        channel = stdout.channel
        _out_pending = [True]
        _err_pending = [True]

        def _recv_ready() -> bool:
            seen, _out_pending[0] = _out_pending[0], False
            return seen

        def _recv_stderr_ready() -> bool:
            seen, _err_pending[0] = _err_pending[0], False
            return seen

        channel.recv_ready.side_effect = _recv_ready
        channel.recv.side_effect = [b"out"]
        channel.recv_stderr_ready.side_effect = _recv_stderr_ready
        channel.recv_stderr.side_effect = [b"err"]
        channel.exit_status_ready.return_value = True
        out, code = windows_exec(ssh, "dir", timeout=10)
        self.assertIn("out", out)
        self.assertIn("err", out)
        self.assertEqual(code, 0)


class SftpMkdirChainTests(unittest.TestCase):
    def test_creates_missing_dirs_only(self) -> None:
        sftp = MagicMock()
        sftp.stat.side_effect = [
            FileNotFoundError(),  # C:\
            FileNotFoundError(),  # C:\gms-flash\
            None,  # C:\gms-flash\t1 exists
        ]
        sftp_mkdir_chain(sftp, r"C:\gms-flash\t1")
        self.assertEqual(sftp.mkdir.call_count, 2)
        sftp.mkdir.assert_any_call("C:\\")
        sftp.mkdir.assert_any_call("C:\\gms-flash\\")


class EnqueueTaskTests(unittest.TestCase):
    def test_writes_task_json_with_device(self) -> None:
        ssh = MagicMock()
        sftp = MagicMock()
        sftp.stat.return_value = None  # queue dir exists
        ssh.open_sftp.return_value = sftp
        queue_dir = source_flash.windows_queue_dir("hcq@172.16.14.66")
        self.assertEqual(queue_dir, r"C:\Users\hcq\gms-flash-queue")
        enqueue_task(ssh, queue_dir, "flash-D1-1",
                     r"C:\gms-flash\t1\u.img", device="RK3576GMS1")
        handle = sftp.open.call_args[0][0]
        self.assertIn("flash-D1-1.json", handle)
        # 任务 JSON 必须携带目标设备。
        written = sftp.open.return_value.__enter__.return_value
        self.assertIn('"device"', written.write.call_args[0][0])

    def test_queue_dir_follows_ssh_user(self) -> None:
        self.assertEqual(
            source_flash.windows_queue_dir("wlq@10.0.0.5"),
            r"C:\Users\wlq\gms-flash-queue",
        )


class WaitResultTests(unittest.TestCase):
    def test_returns_parsed_result(self) -> None:
        ssh, _stdout = _ssh_with_channel(
            json.dumps({"status": "SUCCESS", "log_tail": "ok"}).encode(),
        )

        with patch.object(
            source_flash.time, "sleep", new=lambda _s: None,
        ):
            result = wait_result(ssh, r"C:\Users\hcq\gms-flash-queue",
                                 "flash-D1-1")
        self.assertEqual(result["status"], "SUCCESS")

    def test_raises_on_timeout(self) -> None:
        ssh, _stdout = _ssh_with_channel(b"", exit_code=1)

        calls = {"n": 0}

        def fake_sleep(_s):
            calls["n"] += 1
            if calls["n"] > 3:
                raise KeyboardInterrupt

        with patch.object(
            source_flash.time, "sleep", side_effect=fake_sleep,
        ), self.assertRaises(KeyboardInterrupt):
            wait_result(ssh, r"C:\Users\hcq\gms-flash-queue", "flash-D1-1")


class RunSourceFlashTests(unittest.TestCase):
    def test_success_flow_uploads_and_returns_report(self) -> None:
        ssh = MagicMock()
        sftp = MagicMock()
        sftp.stat.return_value = None
        ssh.open_sftp.return_value = sftp
        ssh.exec_command.return_value = (
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        # Patch exec_command AFTER capturing the channel-emulating stdout:
        # wait_result's `type` probe must return the SUCCESS result exactly
        # once (one-shot ready flags), then "not found" on later polls.
        success = json.dumps({
            "status": "SUCCESS",
            "log_tail": "Download Firmware Success",
        }).encode()
        stdout = MagicMock()
        stdout.channel = _emulate_channel(success)
        ssh.exec_command.side_effect = [
            (MagicMock(), stdout, MagicMock()),
            # poll 2+: result not written yet — command succeeds, empty stdout
            *(
                (MagicMock(), MagicMock(), MagicMock())
                for _ in range(50)
            ),
        ]

        with (
            patch.object(source_flash, "open_windows_ssh", return_value=ssh),
            patch.object(source_flash.time, "sleep", new=lambda _s: None),
        ):
            logs: list[str] = []
            async def collect(msg):
                logs.append(msg)
            report = asyncio.run(run_source_flash(
                device="D1",
                device_host="hcq@172.16.14.66",
                firmware_path="/suite/update.img",
                on_log=collect,
            ))

        self.assertTrue(report.success)
        self.assertEqual(report.stage, "SUCCEEDED")
        # sftp.put called once (firmware upload)
        self.assertTrue(sftp.put.called)

    def test_failure_raises_source_flash_error(self) -> None:
        ssh = MagicMock()
        sftp = MagicMock()
        sftp.stat.return_value = None
        ssh.open_sftp.return_value = sftp
        failure = json.dumps({
            "status": "FAILED",
            "log_tail": "Download Firmware Fail",
            "error": "",
        }).encode()
        stdout = MagicMock()
        stdout.channel = _emulate_channel(failure)
        ssh.exec_command.side_effect = [
            (MagicMock(), stdout, MagicMock()),
            *(
                (MagicMock(), MagicMock(), MagicMock())
                for _ in range(50)
            ),
        ]

        with (
            patch.object(source_flash, "open_windows_ssh", return_value=ssh),
            patch.object(source_flash.time, "sleep", new=lambda _s: None),self.assertRaises(SourceFlashError) as ctx
        ):
            asyncio.run(run_source_flash(
                device="D1",
                device_host="hcq@172.16.14.66",
                firmware_path="/suite/update.img",
                on_log=None,
            ))
        self.assertEqual(ctx.exception.stage, "FLASHING")

    def test_ssh_failure_raises(self) -> None:
        with patch.object(
            source_flash,
            "open_windows_ssh",
            side_effect=SourceFlashError(
                "SSH fail", status_code=502, stage="SSH",
            ),
        ), self.assertRaises(SourceFlashError) as ctx:
            asyncio.run(run_source_flash(
                device="D1",
                device_host="hcq@172.16.14.66",
                firmware_path="/suite/update.img",
            ))
        self.assertEqual(ctx.exception.stage, "SSH")


if __name__ == "__main__":
    unittest.main()
