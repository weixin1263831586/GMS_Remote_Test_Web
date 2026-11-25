"""USB/IP integration test fixture shared by connection and reconciliation tests."""

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

from features.auth import CurrentUser
from features.devices.runtime import configure_runtime


global_state = SimpleNamespace(
    usbip_devices_source={},
    usbip_devices_source_lock=threading.RLock(),
    usbip_states={},
    usbip_states_lock=threading.RLock(),
    device_cache={"devices": [], "timestamp": 0},
    device_cache_lock=threading.RLock(),
    user_states={},
    user_states_lock=threading.RLock(),
)


def _authenticated_request() -> Request:
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [],
        "client": ("127.0.0.1", 1234),
    })
    request.state.current_user = CurrentUser(
        id="id-alice", username="alice", role="user"
    )
    return request


class EmptyConfigManager:
    def get_runtime_config(self):
        return {}


configure_runtime(
    selected_config_manager=EmptyConfigManager(),
    selected_global_state=global_state,
    selected_ssh_manager=None,
    selected_store_notification=None,
    selected_generate_help_or_continue=None,
    selected_get_client_id_from_request=None,
    selected_probe_windows_usbipd=None,
    selected_resolve_tailscale_device_host=None,
)


class UsbipRuntimeTestCase(unittest.TestCase):
    def setUp(self):
        import features.devices.reconnect as reconnect
        from features.users import runtime as user_runtime
        reconnect.stop_usbip_reconnect_tasks(timeout=1)
        self.user_data = tempfile.TemporaryDirectory()
        self.addCleanup(self.user_data.cleanup)
        user_runtime.configure_runtime(data_root=Path(self.user_data.name))
        configure_runtime(
            selected_config_manager=EmptyConfigManager(),
            selected_global_state=global_state,
            selected_ssh_manager=None,
            selected_store_notification=None,
            selected_generate_help_or_continue=None,
            selected_get_client_id_from_request=None,
            selected_probe_windows_usbipd=None,
            selected_resolve_tailscale_device_host=None,
        )
    def tearDown(self):
        import features.devices.reconnect as reconnect
        reconnect.stop_usbip_reconnect_tasks(timeout=1)
        # 断连/固件接管类用例会写入模块级抑制与暂停表；不清除会让
        # 后续 _reconnect_worker 直接被抑制或暂停（跨文件状态泄漏）。
        # clear/resume 是按键弹出，这里强制全表复位。
        with reconnect._suppression_lock:
            reconnect._suppressed_hosts.clear()
            reconnect._suppressed_devices.clear()
        with reconnect._pause_lock:
            reconnect._paused_hosts.clear()
            reconnect._paused_devices.clear()
