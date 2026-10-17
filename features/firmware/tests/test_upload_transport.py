"""SCP 传输 deadline 回归。

固件/工具传输此前只有 `thread.join(timeout=300)` 的兜底（等待循环本身
无 deadline，裸 `SCPClient.put` 完全无界）：卡死的传输会永挂请求并占住
设备锁。现在等待循环与裸 put 都按配置的单调时钟 deadline 收口，超时
关闭 SSH transport 中止阻塞线程并按 504 DEPENDENCY_TIMEOUT 返回。
"""

import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.firmware import runtime, upload_transport
from foundation.error_model import ApiError


class _FakeTransport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeSsh:
    def __init__(self):
        self.transport = _FakeTransport()

    def get_transport(self):
        return self.transport


class _BlockingSCPClient:
    """put() 永远阻塞，直到测试显式放行（模拟卡死的 SCP 通道）。"""

    release = None  # set per-test

    def __init__(self, *_args, **_kwargs):
        pass

    def put(self, *_args, **_kwargs):
        if _BlockingSCPClient.release is not None:
            _BlockingSCPClient.release.wait(timeout=10)
        return None

    def putfo(self, *_args, **_kwargs):
        return self.put()

    def close(self):
        return None


class _FailingSCPClient(_BlockingSCPClient):
    def put(self, *_args, **_kwargs):
        raise RuntimeError("scp channel broken")


class TransferTimeoutConfigTests(unittest.TestCase):
    def tearDown(self):
        runtime.configure_runtime()

    def test_timeout_clamps_to_at_least_one_second(self):
        runtime.configure_runtime(firmware_transfer_timeout=0)
        self.assertGreaterEqual(upload_transport.transfer_timeout_seconds(), 1.0)

    def test_garbage_config_falls_back_to_default(self):
        runtime.configure_runtime(firmware_transfer_timeout="not-a-number")
        self.assertEqual(
            upload_transport.transfer_timeout_seconds(),
            upload_transport._DEFAULT_TRANSFER_TIMEOUT_SECONDS,
        )


class ScpPutWithDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.ssh = _FakeSsh()
        _BlockingSCPClient.release = threading.Event()

    def tearDown(self):
        if _BlockingSCPClient.release is not None:
            _BlockingSCPClient.release.set()
        runtime.configure_runtime()

    def test_timeout_closes_transport_and_raises_504(self):
        runtime.configure_runtime(firmware_transfer_timeout=1)
        with patch("scp.SCPClient", _BlockingSCPClient), self.assertRaises(ApiError) as ctx:
            asyncio.run(
                upload_transport.scp_put_with_deadline(
                    self.ssh, "/local/tool", "/remote/tool"
                )
            )
        self.assertEqual(ctx.exception.code, "DEPENDENCY_TIMEOUT")
        self.assertTrue(self.ssh.transport.closed)

    def test_transport_failure_maps_to_502(self):
        with patch("scp.SCPClient", _FailingSCPClient), self.assertRaises(ApiError) as ctx:
            asyncio.run(
                upload_transport.scp_put_with_deadline(
                    self.ssh, "/local/tool", "/remote/tool"
                )
            )
        self.assertEqual(ctx.exception.code, "UPSTREAM_FAILURE")
        # 异常细节只进服务端日志；客户端消息保留失败上下文（路径），
        # 不回显底层异常文本（架构规则：宽带 except 细节不进 API 响应）。
        self.assertIn("/local/tool", ctx.exception.message)
        self.assertNotIn("scp channel broken", ctx.exception.message)

    def test_success_does_not_close_transport(self):
        calls = []

        class _RecordingClient(_BlockingSCPClient):
            def put(self, local_path, remote_path):
                calls.append((local_path, remote_path))
                return None

        with patch("scp.SCPClient", _RecordingClient):
            asyncio.run(
                upload_transport.scp_put_with_deadline(
                    self.ssh, "/local/tool", "/remote/tool"
                )
            )
        self.assertEqual(calls, [("/local/tool", "/remote/tool")])
        self.assertFalse(self.ssh.transport.closed)


class UploadDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.ssh = _FakeSsh()
        _BlockingSCPClient.release = threading.Event()
        runtime.configure_runtime(
            firmware_transfer_timeout=1,
            global_state=SimpleNamespace(
                apk_analysis_tasks={},
                apk_analysis_tasks_lock=threading.RLock(),
                firmware_upload_progress={},
                firmware_upload_progress_lock=threading.RLock(),
                websocket_connections={},
            ),
            safe_websocket_send=lambda *_args, **_kwargs: _noop_async(),
        )

    def tearDown(self):
        _BlockingSCPClient.release.set()
        runtime.configure_runtime(apk_task_store=None)

    def test_stalled_upload_times_out_with_504_and_aborts_transport(self):
        import tempfile

        with (
            tempfile.NamedTemporaryFile(suffix=".img") as firmware,
            patch("scp.SCPClient", _BlockingSCPClient),
            self.assertRaises(ApiError) as ctx,
        ):
            asyncio.run(
                upload_transport.upload_firmware_to_test_host(
                    self.ssh,
                    "client-1",
                    firmware.name,
                    "/remote/firmware.img",
                    "firmware.img",
                    1024,
                )
            )
        self.assertEqual(ctx.exception.code, "DEPENDENCY_TIMEOUT")
        self.assertTrue(self.ssh.transport.closed)


async def _noop_async(*_args, **_kwargs):
    return None


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
