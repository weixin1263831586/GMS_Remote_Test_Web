import time
from unittest.mock import patch

from worker_agent.device_actions import (
    _DEVICE_DETAILS_CACHE,
    _details_refresh_at,
)
from worker_agent.inventory import probe_devices


def test_detailed_probe_serves_from_cache_and_enriches_in_background():
    """heartbeat 只读缓存快照，绝不同步做 ADB detail 往返（防假离线）。

    detail 属性由后台 enrichment 线程填充；首轮 heartbeat 的详情字段
    允许为空，最坏晚到一轮（评审第七节的设计契约）。
    """
    def run(argv, timeout=10, env=None):
        if argv[:3] == ["adb", "devices", "-l"]:
            return "List of devices attached\nABC device product:rk model:Box transport_id:1\n"
        if argv[:4] == ["adb", "-s", "ABC", "shell"]:
            return (
                "__MODEL__\nLiving Room Box\n"
                "__ANDROID__\n14\n"
                "__BATTERY__\n  level: 82\n"
                "__SOC__\nRK3588S\n"
            )
        if argv[:2] == ["fastboot", "devices"]:
            return ""
        raise AssertionError(argv)

    _DEVICE_DETAILS_CACHE.pop("ABC", None)
    _details_refresh_at.pop("ABC", None)
    try:
        with patch("worker_agent.device_actions._run", side_effect=run) as runner:
            devices = probe_devices(include_details=True)

            # 首轮：不阻塞等待 detail，返回快照 + base 属性。
            assert devices[0]["properties"] == {
                "product": "rk",
                "model": "Box",
                "transport_id": "1",
            }
            # 同步路径没有对设备做 detail shell 调用。
            assert sum(
                call.args[0][:4] == ["adb", "-s", "ABC", "shell"]
                for call in runner.call_args_list
            ) == 0

            # 后台 enrichment 线程完成一轮后，detail 属性从缓存提供。
            # （等待必须在 patch 块内：线程用的是模块级 _run。）
            details = {}
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                details = _DEVICE_DETAILS_CACHE.get("ABC", (0.0, {}))[1]
                if details.get("soc_model") == "RK3588S":
                    break
                time.sleep(0.1)
            assert details.get("soc_model") == "RK3588S"
            devices = probe_devices(include_details=True)
        assert devices[0]["properties"]["soc_model"] == "RK3588S"
        assert devices[0]["properties"]["battery_level"] == "82"
    finally:
        _DEVICE_DETAILS_CACHE.pop("ABC", None)
        _details_refresh_at.pop("ABC", None)
        from worker_agent.device_actions import _details_last_seen_at
        _details_last_seen_at.pop("ABC", None)
        from worker_agent.device_actions import _details_enrich_stop
        _details_enrich_stop.set()


def test_lightweight_probe_skips_detail_shell_call():
    with patch(
        "worker_agent.device_actions._run",
        side_effect=[
            "List of devices attached\nABC device product:rk model:Box\n",
            "",
        ],
    ) as runner:
        devices = probe_devices()

    assert devices[0]["properties"]["model"] == "Box"
    assert runner.call_count == 2


def test_probe_keeps_fastbootd_device_visible():
    with patch(
        "worker_agent.device_actions._run",
        side_effect=[
            "List of devices attached\n",
            "FB001\tfastbootd\n",
        ],
    ):
        devices = probe_devices()

    assert devices == [{
        "serial": "FB001",
        "transport": "local_usb",
        "state": "fastboot",
        "properties": {},
    }]


def test_probe_reports_adb_proxy_import_with_source_metadata():
    with patch(
        "worker_agent.device_actions._run",
        side_effect=[
            "List of devices attached\nRK3576GMS1 device model:Box\n",
            "",
        ],
    ), patch(
        "worker_agent.adb_proxy.sync_source_policy"
    ), patch(
        "worker_agent.adb_proxy.imported_devices",
        return_value={
            "RK3576GMS1": {
                "source_worker_id": "ats-worker-controller",
                "source_address": "172.16.14.233",
                "source_serial": "RK3576GMS1",
            }
        },
    ):
        devices = probe_devices()

    assert devices == [{
        "serial": "RK3576GMS1",
        "transport": "adb_proxy",
        "state": "available",
        "properties": {
            "model": "Box",
            "adb_proxy_source_worker_id": "ats-worker-controller",
            "adb_proxy_source_address": "172.16.14.233",
            "adb_proxy_source_serial": "RK3576GMS1",
        },
    }]
