import asyncio
import inspect
import unittest
from unittest.mock import Mock, patch

from features.system.state import global_state
from features.system.terminal_output import start_terminal_output_pump


class _Channel:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    def recv_ready(self):
        return bool(self.chunks)

    def recv(self, _size):
        return self.chunks.pop(0)

    def close(self):
        self.closed = True


class _WebSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, value):
        self.messages.append(value)


class _SplitUtf8Channel(_Channel):
    """Expose one chunk per polling cycle to model a split PTY code point."""

    def __init__(self, chunks):
        super().__init__(chunks)
        self.poll_ready = True

    def recv_ready(self):
        if not self.chunks:
            return False
        ready = self.poll_ready
        self.poll_ready = not self.poll_ready
        return ready


class TerminalOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_closing_during_submission_closes_every_unscheduled_coroutine(self):
        session_id = 'loop-closed'
        channel = _Channel([b'pending'])
        with global_state.terminal_lock:
            global_state.terminal_ssh_sessions[session_id] = {'channel': channel, 'ssh': None, 'mode': 'local'}
        with patch('features.system.terminal_output.asyncio.run_coroutine_threadsafe',
                   side_effect=RuntimeError('Event loop is closed')) as submit:
            thread = start_terminal_output_pump(session_id, _WebSocket(), asyncio.get_running_loop(),
                                                thread_name='test-terminal-closed-loop', notify_disconnect=True)
            await asyncio.to_thread(thread.join, 1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(channel.closed)
        self.assertNotIn(session_id, global_state.terminal_ssh_sessions)
        self.assertEqual(submit.call_count, 2)
        for call in submit.call_args_list:
            self.assertEqual(inspect.getcoroutinestate(call.args[0]), inspect.CORO_CLOSED)

    async def test_send_timeout_cancels_pending_delivery_before_cleanup(self):
        session_id = 'send-timeout'
        channel = _Channel([b'pending'])
        with global_state.terminal_lock:
            global_state.terminal_ssh_sessions[session_id] = {'channel': channel, 'ssh': None, 'mode': 'local'}
        future = Mock()
        future.result.side_effect = TimeoutError('send stalled')
        with patch('features.system.terminal_output._submit_output', return_value=future):
            thread = start_terminal_output_pump(session_id, _WebSocket(), asyncio.get_running_loop(),
                                                thread_name='test-terminal-send-timeout')
            await asyncio.to_thread(thread.join, 1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(channel.closed)
        future.cancel.assert_called_once_with()

    async def asyncTearDown(self):
        with global_state.terminal_lock:
            sessions = list(global_state.terminal_ssh_sessions.values())
            global_state.terminal_ssh_sessions.clear()
        for session in sessions:
            session["channel"].close()

    async def test_available_chunks_are_batched_into_one_websocket_message(self):
        session_id = "batch-session"
        channel = _Channel([b"hello ", b"terminal", b""])
        websocket = _WebSocket()
        with global_state.terminal_lock:
            global_state.terminal_ssh_sessions[session_id] = {
                "channel": channel,
                "ssh": None,
                "mode": "local",
            }

        thread = start_terminal_output_pump(
            session_id,
            websocket,
            asyncio.get_running_loop(),
            thread_name="test-terminal-batch",
        )
        await asyncio.to_thread(thread.join, 1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(websocket.messages, [{
            "type": "terminal_data",
            "data": "hello terminal",
        }])
        self.assertTrue(channel.closed)

    async def test_deliberate_session_removal_does_not_emit_disconnect_error(self):
        session_id = "closed-session"
        channel = _Channel([])
        websocket = _WebSocket()
        with global_state.terminal_lock:
            global_state.terminal_ssh_sessions[session_id] = {
                "channel": channel,
                "ssh": None,
                "mode": "local",
            }

        thread = start_terminal_output_pump(
            session_id,
            websocket,
            asyncio.get_running_loop(),
            thread_name="test-terminal-close",
            notify_disconnect=True,
        )
        await asyncio.sleep(0.03)
        with global_state.terminal_lock:
            global_state.terminal_ssh_sessions.pop(session_id, None)
        channel.close()
        await asyncio.to_thread(thread.join, 1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(websocket.messages, [])

    async def test_utf8_code_point_split_between_batches_is_preserved(self):
        session_id = "split-utf8-session"
        channel = _SplitUtf8Channel([b"\xe4\xbd", b"\xa0", b""])
        websocket = _WebSocket()
        with global_state.terminal_lock:
            global_state.terminal_ssh_sessions[session_id] = {
                "channel": channel,
                "ssh": None,
                "mode": "local",
            }

        thread = start_terminal_output_pump(
            session_id,
            websocket,
            asyncio.get_running_loop(),
            thread_name="test-terminal-split-utf8",
        )
        await asyncio.to_thread(thread.join, 1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(websocket.messages, [{
            "type": "terminal_data",
            "data": "你",
        }])
