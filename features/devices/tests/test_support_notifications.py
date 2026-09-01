"""Tests for device-change broadcast notification deduplication."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from starlette.websockets import WebSocketState


class _FakeWebSocket:
    def __init__(self):
        self.client_state = WebSocketState.CONNECTED
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)


def _runtime_with_notification_store(stored):
    def store_notification(client_id, title, message, level, category, data=None):
        record = {
            "id": f"nid-{len(stored)}",
            "owner": client_id,
            "title": title,
            "message": message,
            "level": level,
            "category": category,
            "data": data or {},
        }
        stored.append(record)
        return record

    return SimpleNamespace(store_notification=store_notification)


class BroadcastDeviceChangeTests(unittest.TestCase):
    def test_single_notification_per_broadcast_across_workspace_connections(self):
        import features.devices.support as support

        stored = []
        page_ws = _FakeWebSocket()
        terminal_ws = _FakeWebSocket()
        connections = {
            "user-a": page_ws,
            "user-a:terminal_workspace_0_123_abc": terminal_ws,
        }
        locks = SimpleNamespace(websocket_connections_lock=None)
        runtime_stub = _runtime_with_notification_store(stored)
        # connections_lock used via `with`, emulate no-op context manager.
        class _NullLock:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        runtime_stub.global_state = SimpleNamespace(
            websocket_connections=connections,
            websocket_connections_lock=_NullLock(),
        )

        async def run():
            await support.broadcast_device_change(
                ["user-a"], disconnected=["RK3572GMS1"],
                source="usbip_disconnect",
            )

        with patch.object(support.runtime, "store_notification",
                          runtime_stub.store_notification), \
             patch.object(support.runtime, "global_state",
                          runtime_stub.global_state):
            asyncio.run(run())

        # One persisted notification for the base client id only.
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["owner"], "user-a")
        self.assertEqual(stored[0]["title"], "USB设备断开")
        # Both connections still receive the same notification payload.
        self.assertEqual(len(page_ws.sent), 1)
        self.assertEqual(len(terminal_ws.sent), 1)
        self.assertEqual(
            page_ws.sent[0]["notification"]["id"],
            terminal_ws.sent[0]["notification"]["id"],
        )

    def test_no_notification_persisted_without_title(self):
        import features.devices.support as support

        stored = []
        page_ws = _FakeWebSocket()
        connections = {"user-a": page_ws}

        class _NullLock:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        runtime_stub = _runtime_with_notification_store(stored)
        runtime_stub.global_state = SimpleNamespace(
            websocket_connections=connections,
            websocket_connections_lock=_NullLock(),
        )

        async def run():
            # No disconnected/connected -> no notification title.
            await support.broadcast_device_change(["user-a"], source="poll")

        with patch.object(support.runtime, "store_notification",
                          runtime_stub.store_notification), \
             patch.object(support.runtime, "global_state",
                          runtime_stub.global_state):
            asyncio.run(run())

        self.assertEqual(stored, [])
        self.assertEqual(len(page_ws.sent), 1)
        self.assertNotIn("notification", page_ws.sent[0])


if __name__ == "__main__":
    unittest.main()
