import threading
import time
from unittest.mock import patch

import pytest

from worker_agent import device_actions
from worker_agent.device_actions import (
    _DEVICE_DETAILS_CACHE,
    _details_refresh_at,
    _probe_usb_only_devices,
)
from worker_agent.inventory import probe_devices


@pytest.fixture(autouse=True)
def _isolate_host_usb_inventory(monkeypatch):
    """Inventory unit tests must never depend on or enumerate the host's USB."""
    monkeypatch.setattr(device_actions, "_probe_usb_only_devices", lambda _known: [])
    # 探测逻辑单测与宿主机的 AOA 会话状态解耦：活 Worker 会话期间
    # （及标志 TTL 未过期时）adb 抑制会让 probe 跳过 adb 分支，
    # patch 的 _run 序列错位 → 偶发失败。本文件只测解析/枚举逻辑。
    monkeypatch.setattr(device_actions, "_adb_daemon_suppressed", lambda: False)
    # liveness 往返同理：mock 的 _run 无法应答真实 `adb shell true`，
    # 默认放行（设备态健康），幽灵传输由下面的专测显式关掉。
    monkeypatch.setattr(
        device_actions, "_adb_transport_alive", lambda *_a, **_k: True)


def test_usb_only_android_device_remains_available_for_aoa(tmp_path):
    device = tmp_path / "1-6"
    interface = tmp_path / "1-6:1.0"
    device.mkdir()
    interface.mkdir()
    (device / "serial").write_text("USB-OFF-1\n")
    (device / "idVendor").write_text("2207\n")
    (device / "idProduct").write_text("0006\n")
    (interface / "bInterfaceClass").write_text("ff\n")

    devices = _probe_usb_only_devices(set(), tmp_path)

    assert devices == [{
        "serial": "USB-OFF-1",
        "transport": "local_usb",
        "state": "usb_only",
        "properties": {
            "usb": "1-6",
            "usb_vendor_id": "2207",
            "usb_product_id": "0006",
            "aoa_recovery_candidate": "true",
        },
    }]
    assert _probe_usb_only_devices({"USB-OFF-1"}, tmp_path) == []


def test_usb_only_inventory_ignores_non_android_usb_device(tmp_path):
    device = tmp_path / "2-1"
    interface = tmp_path / "2-1:1.0"
    device.mkdir()
    interface.mkdir()
    (device / "serial").write_text("STORAGE-1\n")
    (device / "idVendor").write_text("abcd\n")
    (device / "idProduct").write_text("1234\n")
    (interface / "bInterfaceClass").write_text("08\n")

    assert _probe_usb_only_devices(set(), tmp_path) == []


def test_usb_only_probe_excludes_usb_uart_bridge_dongles(tmp_path):
    # FT232R 串口调试小板（vendor 0403，接口类恰为 ff）曾被 class-ff 兜底
    # 误收成 AOA 恢复候选，污染设备清单并可能被烧写流程误 reset 串口。
    # 宿主机 sysfs 实证：1-14 vendor=0403 product=6001 class=ff iSerial=A6022883。
    for port, vendor, product, serial in (
        ("1-14", "0403", "6001", "A6022883"),
        ("1-15", "1a86", "7523", "CH340-1"),
    ):
        device = tmp_path / port
        interface = tmp_path / f"{port}:1.0"
        device.mkdir()
        interface.mkdir()
        (device / "serial").write_text(f"{serial}\n")
        (device / "idVendor").write_text(f"{vendor}\n")
        (device / "idProduct").write_text(f"{product}\n")
        (interface / "bInterfaceClass").write_text("ff\n")

    assert _probe_usb_only_devices(set(), tmp_path) == []


def test_usb_only_probe_keeps_other_vendor_specific_boot_devices(tmp_path):
    # class-ff 兜底仍放行非桥接芯片厂商（如 Qualcomm EDL 05c6:9008）；
    # 排除名单只针对 USB-UART 桥，不得把兜底收窄成纯 18d1/2207 白名单。
    device = tmp_path / "3-2"
    interface = tmp_path / "3-2:1.0"
    device.mkdir()
    interface.mkdir()
    (device / "serial").write_text("EDL-9008\n")
    (device / "idVendor").write_text("05c6\n")
    (device / "idProduct").write_text("9008\n")
    (interface / "bInterfaceClass").write_text("ff\n")

    devices = _probe_usb_only_devices(set(), tmp_path)

    assert [item["serial"] for item in devices] == ["EDL-9008"]
    assert devices[0]["properties"]["aoa_recovery_candidate"] == "true"


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
                "__VARIANT__\nrk3588_box\n"
                "__BUILD_ID__\nAP2A\n"
                "__BOARD__\nrk3588\n"
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
        assert devices[0]["properties"]["variant"] == "rk3588_box"
        assert devices[0]["properties"]["build_id"] == "AP2A"
        assert devices[0]["properties"]["board"] == "rk3588"
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


def test_probe_marks_ghost_adb_transport_unresponsive():
    # 烧写后设备重枚举进 loader（2207:0006），adb server 里仍残留 device
    # 态幽灵条目：任何往返 error: closed。清单必须标 unresponsive，
    # 否则 Web「ADB 已连接」徽标误导选机（实机 66df75a34ebde4a4 回归）。
    with patch(
        "worker_agent.device_actions._run",
        side_effect=[
            "List of devices attached\n66df75a34ebde4a4 device usb:1-13 product:rk\n",
            "",
        ],
    ), patch(
        "worker_agent.device_actions._adb_transport_alive",
        return_value=False,
    ) as alive:
        devices = probe_devices()

    assert devices[0]["serial"] == "66df75a34ebde4a4"
    assert devices[0]["state"] == "unresponsive"
    # 幽灵条目仍占位 known：usb_only 探测不重复列同一物理口。
    alive.assert_called_once_with("66df75a34ebde4a4", None)


def test_probe_checks_liveness_only_for_device_state():
    # offline/unauthorized 是显式异常态，不需要额外往返确认；
    # 只有 device 态才值得为真实性付一次往返。
    with patch(
        "worker_agent.device_actions._run",
        side_effect=[
            "List of devices attached\n"
            "A\tdevice usb:1-6\n"
            "B\toffline\n"
            "C\tunauthorized usb:1-7\n",
            "",
        ],
    ), patch(
        "worker_agent.device_actions._adb_transport_alive",
        return_value=True,
    ) as alive:
        devices = probe_devices()

    assert [item["state"] for item in devices] == ["available", "offline", "unauthorized"]
    alive.assert_called_once()


def test_probe_normalizes_bare_usb_port_from_platform_tools_35():
    # platform-tools 35 的 `adb devices -l` 把物理端口打印成裸位置字段
    # （`1-7`），不再带 `usb:` 前缀；端口缺失会让烧写后 AOA 恢复冻结报
    # 409（无法确认物理端口）。
    with patch(
        "worker_agent.device_actions._run",
        side_effect=[
            "List of devices attached\nRK3576GMS1 device 1-7 product:ssi model:Box\n",
            "",
        ],
    ):
        devices = probe_devices()

    assert devices[0]["properties"]["usb"] == "1-7"


def test_probe_keeps_legacy_usb_prefix_port():
    with patch(
        "worker_agent.device_actions._run",
        side_effect=[
            "List of devices attached\nABC device usb:1-6 product:rk model:Box\n",
            "",
        ],
    ):
        devices = probe_devices()

    assert devices[0]["properties"]["usb"] == "1-6"


def test_probe_ignores_colonless_non_port_token():
    with patch(
        "worker_agent.device_actions._run",
        side_effect=[
            "List of devices attached\nABC device caveat product:rk model:Box\n",
            "",
        ],
    ):
        devices = probe_devices()

    assert "usb" not in devices[0]["properties"]


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
