"""Shared kkagent process capture and identity isolation contracts."""

from __future__ import annotations

import asyncio
import os
import unittest

from features.kkagent import (
    CAPTURE_HEAD_BYTES,
    CAPTURE_TAIL_BYTES,
    CappedCapture,
    child_env,
    read_bounded,
    read_stream_capped,
)


class StreamCaptureTests(unittest.TestCase):
    def _feed_and_collect(self, payload: bytes) -> bytes:
        async def scenario():
            reader = asyncio.StreamReader()
            reader.feed_data(payload)
            reader.feed_eof()
            return await read_stream_capped(reader)

        return asyncio.run(scenario())

    def test_stream_capture_is_memory_capped(self):
        payload = (b"x" * 65536) * 40  # 2.5MB，远超 head+tail 上限
        captured = self._feed_and_collect(payload)
        self.assertLessEqual(len(captured), CAPTURE_HEAD_BYTES + CAPTURE_TAIL_BYTES + 64)
        self.assertIn(b"...[truncated]...", captured)
        self.assertTrue(captured.startswith(b"x"))
        self.assertTrue(captured.endswith(b"x"))

    def test_stream_capture_small_output_untouched(self):
        captured = self._feed_and_collect(b'{"result": {"ok": true}}')
        self.assertEqual(captured, b'{"result": {"ok": true}}')

    def test_capped_capture_accepts_text_chunks(self):
        capture = CappedCapture(head=8, tail=8)
        capture.feed("中文ab")
        capture.feed("cd")
        self.assertTrue(capture.text())

    def test_exact_tail_fill_is_not_marked_truncated(self):
        capture = CappedCapture(head=4, tail=4)
        capture.feed(b"abcdefgh")
        self.assertEqual(capture.text(), "abcdefgh")
        self.assertNotIn("truncated", capture.text())

    def test_tail_overflow_inserts_marker(self):
        capture = CappedCapture(head=4, tail=4)
        capture.feed(b"abcdefghi")
        self.assertIn("...[truncated]...", capture.text())


class ChildEnvTests(unittest.TestCase):
    def test_child_env_strips_inherited_mcp_identity(self):
        leaked = {
            "GMS_RT_PROFILE": "other-owner",
            "GMS_AGENT_CLIENT": "kimi",
            "GMS_AGENT_AUTH_MODE": "service-token",
            "GMS_AUTH_TOKEN_FILE": "/tmp/other-owner.token",
            "GMS_REMOTE_TEST_SERVER": "https://controller.example",
            "GMS_MCP_TOOLSETS": "core,admin",
        }
        old_env = {k: os.environ.get(k) for k in leaked}
        try:
            os.environ.update(leaked)
            env = child_env({"GMS_RT_PROFILE": "owner-a"})
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(env["GMS_RT_PROFILE"], "owner-a")
        # 泄漏的宿主身份要么不存在、要么为空，绝不允许出现他人值。
        self.assertNotEqual(env.get("GMS_AGENT_CLIENT"), "kimi")
        self.assertNotIn("other-owner.token", env.get("GMS_AUTH_TOKEN_FILE", ""))
        self.assertEqual(env.get("GMS_REMOTE_TEST_SERVER", ""), "")
        # 继承的 toolset 必须被剥离（由晨报侧显式注入 evidence）。
        self.assertNotEqual(env.get("GMS_MCP_TOOLSETS"), "core,admin")
        self.assertEqual(env["NO_COLOR"], "1")


class BoundedReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_output_and_line_callbacks(self):
        reader = asyncio.StreamReader()
        reader.feed_data("一\ntwo\nlast".encode())
        reader.feed_eof()
        lines = []
        self.assertEqual(await read_bounded(reader, on_line=lines.append),
                         "一\ntwo\nlast".encode())
        self.assertEqual(lines, ["一", "two", "last"])

    async def test_exact_limit_is_allowed(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b"abcd")
        reader.feed_eof()
        self.assertEqual(await read_bounded(reader, limit=4), b"abcd")

    async def test_over_limit_fails_instead_of_truncating(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b"abcde")
        reader.feed_eof()
        with self.assertRaisesRegex(RuntimeError, "输出超过大小限制"):
            await read_bounded(reader, limit=4)
