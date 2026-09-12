import threading
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
    允许为空，最坏晚到一轮（设计契约：detail 由后台 enrichment 异步填充）。

    注意：首轮"详情为空"不是硬契约——seed 之后、同步读取之前，后台
    线程可能恰好完成首轮 refresh（全量测试套件下 CPU 竞争会放大这个
    窗口，此前"首轮必须无 detail 字段"的断言因此偶发失败）。硬契约是：
    同步路径绝不为 detail 发起或等待 ADB 往返，detail 一律来自缓存。
    """
    def run(argv, timeout=10, env=None):
        # 按调用时刻记录调用方线程：mock 的 call_args_list 无法事后区分
        # 调用来自主线程还是后台 enrichment 线程（生成器里
        # threading.current_thread() 求值于测试主线程，恒为 MainThread）。
        shell_calls.append((threading.current_thread().name, tuple(argv[:4])))
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

    shell_calls: list[tuple[str, tuple[str, ...]]] = []
    _DEVICE_DETAILS_CACHE.pop("ABC", None)
    _details_refresh_at.pop("ABC", None)
    try:
        with patch("worker_agent.device_actions._run", side_effect=run) as runner:
            devices = probe_devices(include_details=True)

            # 首轮：base 属性来自 `adb devices -l` 快照行。
            assert devices[0]["properties"]["product"] == "rk"
            assert devices[0]["properties"]["model"] in {"Box", "Living Room Box"}
            assert devices[0]["properties"]["transport_id"] == "1"
            # 硬契约：主线程（同步 probe 路径）从未发起 detail shell 调用；
            # 后台 enrichment 线程的调用不受此限制（记录于调用时刻的线程名）。
            assert not [
                call for call in shell_calls
                if call[0] == "MainThread"
                and call[1] == ("adb", "-s", "ABC", "shell")
            ]
            assert runner.call_count >= 2

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
