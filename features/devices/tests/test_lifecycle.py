import threading
import unittest
from unittest.mock import patch

from features.devices import monitor, reconnect


class DeviceLifecycleTests(unittest.TestCase):
    def tearDown(self):
        reconnect.stop_usbip_reconnect_tasks(timeout=1)

    def test_reconnect_tasks_stop_on_shutdown(self):
        attempted = threading.Event()

        class FakeConfigManager:
            def load_config(self, force_reload=False):
                return {
                    "device_pswd": "secret",
                    "client_ssh_credentials": [],
                }

        class FakeUsbipManager:
            config_manager = FakeConfigManager()

            def start_usbip(self, device_host, device_password):
                attempted.set()
                return {"success": False, "device_list": []}

        with patch.object(reconnect.runtime, "config_manager", FakeConfigManager()), \
                patch.object(reconnect, "usbip_manager", FakeUsbipManager()), \
                patch.object(reconnect, "has_blocked_adb_process", return_value=False), \
                patch.object(reconnect, "USBIP_RECONNECT_INTERVAL_SECONDS", 60):
            self.assertTrue(reconnect.schedule_usbip_reconnect("host", reason="test"))
            self.assertTrue(attempted.wait(timeout=1))
            reconnect.stop_usbip_reconnect_tasks(timeout=1)

        self.assertEqual(reconnect.active_usbip_reconnect_hosts(), [])

    def test_usb_monitor_thread_stops(self):
        usb_monitor = monitor.USBMonitor(
            device_getter=lambda: [],
            check_interval=0.01,
            use_udev=False,
            debounce_count=1,
        )

        usb_monitor.start()
        thread = usb_monitor._thread
        usb_monitor.stop()

        self.assertIsNotNone(thread)
        self.assertFalse(thread.is_alive())
        self.assertFalse(usb_monitor.is_running)

    def test_usb_change_invalidates_device_cache(self):
        state = type(
            "State",
            (),
            {
                "device_cache": {"devices": [{"device_id": "old"}], "timestamp": 1},
                "device_cache_lock": threading.RLock(),
            },
        )()

        monitor.invalidate_device_cache(state)

        self.assertEqual(state.device_cache, {"devices": [], "timestamp": 0})

    def test_suppressed_usbip_device_is_hidden_from_monitor_getter(self):
        reconnect.suppress_usbip_reconnect("host", ["USBIP001"])
        try:
            self.assertEqual(
                reconnect.filter_suppressed_usbip_devices(["LOCAL001", "USBIP001"]),
                ["LOCAL001"],
            )
        finally:
            reconnect.clear_usbip_reconnect_suppression("host", ["USBIP001"])
