"""AOA 语法层与执行器的单元测试（不触碰真实 USB）。"""

from __future__ import annotations

import pytest

from foundation import aoa_profile
from foundation.aoa_profile import (
    AOA_TOUCH_MAX_X,
    AOA_TOUCH_MAX_Y,
    key_report,
    parse_profile_commands,
    swipe_points,
    system_report,
    text_to_key_reports,
    touch_report,
)
from worker_agent import aoa_actions


def test_official_syntax_is_accepted():
    commands = [
        "wake", "home", "back",
        "click 100 200", "longClick 360 640",
        "swipe 0 0 100 360 640",
        "write hello world.",
        "key RIGHT 2*TAB ENTER",
        "key dpad_down dpad_center",
        "sleep 500",
    ]
    parsed = parse_profile_commands(commands)
    kinds = [item.kind for item in parsed]
    assert kinds == ["wake", "home", "back", "click", "longClick", "swipe",
                     "write", "key", "key", "sleep"]
    combo = parsed[7].keys
    assert combo == ((0, 0x4F), (0, 0x2B), (0, 0x2B), (0, 0x28))
    # TV 遥控别名映射到 DPAD/ENTER usage
    assert parsed[8].keys == ((0, 0x51), (0, 0x28))
    assert parsed[9].sleep_ms == 500


def test_uppercase_letters_apply_shift_modifier():
    parsed = parse_profile_commands(["key A b"])
    assert parsed[0].keys == ((aoa_profile.MODIFIER_SHIFT, 0x04), (0, 0x05))


def test_invalid_actions_rejected():
    for bad in ["fly 1 2", "click", "click a b", "swipe 1 2 3", "key CTRL",
                "key 5*", "write 你好", "sleep 0", "sleep 99999",
                # 空白只支持空格（无 \t\n 的 HID 映射）；首字符 '-' 会被
                # Android `input` 当选项解析。
                "write tab\there", "write line\nbreak", "write -starts-with-dash"]:
        with pytest.raises(ValueError):
            parse_profile_commands([bad])


@pytest.mark.parametrize("command", ["click 400 700", "longClick 361 0", "swipe 0 0 100 0 641"])
def test_profile_coordinates_outside_hid_range_are_rejected(command):
    with pytest.raises(ValueError, match="coordinates outside"):
        parse_profile_commands([command])


def test_budget_limits():
    with pytest.raises(ValueError):
        parse_profile_commands([])
    assert len(parse_profile_commands(["wake"] * 200)) == 200
    with pytest.raises(ValueError):
        parse_profile_commands(["wake"] * 251)      # 超总时长预算
    with pytest.raises(ValueError):
        parse_profile_commands([f"click {i%360} {i%640}" for i in range(256)])
    with pytest.raises(ValueError):
        parse_profile_commands(["sleep 10000"] * 35)  # 总时长超 300s


def test_touch_and_key_reports_match_aoa_device():
    # 触摸报文 [type, x_lo, x_hi, y_lo, y_hi]，坐标 clamp 到 360x640
    assert touch_report(True, 10, 20) == bytes([0b11, 10, 0, 20, 0])
    assert touch_report(False, 0, 0) == bytes([0b00, 0, 0, 0, 0])
    assert touch_report(True, AOA_TOUCH_MAX_X + 10, AOA_TOUCH_MAX_Y + 10) == \
        bytes([0b11, 0x68, 0x01, 0x80, 0x02])
    assert key_report(0, 0x28) == bytes([0, 0x28])
    assert system_report(aoa_profile.AOA_SYSTEM_HOME) == bytes([0b010])
    reports = text_to_key_reports("aB ")
    assert reports == [bytes([0, 0x04]), bytes([0b10, 0x05]), bytes([0, 0x2C])]


def test_swipe_points_interpolation():
    points = swipe_points(0, 0, 100, 50, 100)
    assert points[0] == (True, 0, 0)
    assert points[-1] == (False, 100, 50)
    downs = [p for p in points if p[0]]
    assert len(downs) >= 10  # 100ms / 10ms 步进


def test_key_repeat_count_is_bounded():
    # 官方 key 支持 N*KEY 重复，但 ADR 0022 要求显式上限而非静默截断。
    with pytest.raises(ValueError):
        parse_profile_commands(["key 33*ENTER"])
    parsed = parse_profile_commands(["key 32*ENTER"])
    assert len(parsed[0].keys) == 32


def test_key_repeat_counts_toward_runtime_budget():
    # 每键 2 个 HID 事件 ×~120ms 计入 300s 预算；步骤上限 64 先触顶时
    # 同样拒绝。单条命令按 32 键 ×~120ms + 间隔计费（~4.8s）。
    from foundation.aoa_profile import _estimate_runtime_ms

    parsed = parse_profile_commands(["key 32*ENTER"])
    assert _estimate_runtime_ms(parsed[0]) >= 32 * 120
    with pytest.raises(ValueError):
        # 步骤上限 64 内、总时长越界：63 条 32 键 + sleep 3000 ≈ 308s > 300s。
        parse_profile_commands(["sleep 3000"] + ["key 32*ENTER"] * 62)
    # 压线对照：62 条 ≈ 298s < 300s，在预算内放行（无 key 计费时这组
    # 会被低估约 48s，key 计费是必要条件）。
    parsed = parse_profile_commands(["sleep 3000"] + ["key 32*ENTER"] * 61)
    assert len(parsed) == 62


def test_probe_reports_missing_device(monkeypatch):
    class FakeCore:
        class USBError(Exception):
            pass

        @staticmethod
        def find(**_kwargs):
            return iter(())

    monkeypatch.setattr(aoa_actions, "_usb", lambda: (FakeCore(), None))
    with pytest.raises(aoa_actions.AoaError):
        aoa_actions.probe_aoa("nope")


def test_find_tap_point_prefers_exact_text_match():
    """“System”必须精确命中 System 行，不误点 System update。"""
    nodes = [
        {"text": "System update", "content_desc": "", "resource_id": "",
         "clickable": True, "center": [100, 100]},
        {"text": "System", "content_desc": "", "resource_id": "",
         "clickable": True, "center": [100, 300]},
    ]
    point = aoa_actions._find_tap_point(nodes, {}, {"any_text": ["系统", "System"]})
    assert point == [100, 300]


def test_node_matches_content_desc_accepts_list():
    """content_desc 匹配器同时接受字符串与多语言候选列表（预设旅程用法）。"""
    node = {"content_desc": "设置", "center": (10, 20)}
    assert aoa_actions._node_matches(node, {"content_desc": "设置"})
    assert aoa_actions._node_matches(node, {"content_desc": ["Settings", "设置"]})
    assert not aoa_actions._node_matches(node, {"content_desc": ["Camera", "相机"]})
    assert not aoa_actions._node_matches(node, {"content_desc": "相机"})


def test_record_appends_pacing_sleeps(monkeypatch):
    """录制的命令必须包含 wait_ms pacing（sleep），否则回放全速连发
    会把点击落在界面动画中间（实机教训）。"""

    class FakeCompleted:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(aoa_actions, "_display_size", lambda _serial: (800, 1280))
    monkeypatch.setattr(aoa_actions, "_adb_run",
                        lambda _serial, *args: FakeCompleted())
    monkeypatch.setattr(
        aoa_actions, "_dump_elements",
        lambda _serial, _sleep=None: (
            [{"text": "设置", "content_desc": "", "clickable": True, "center": [400, 600]}],
            {},
        ),
    )
    result = aoa_actions.record_aoa_profile(
        "8e4dbf3781825b31",
        [{"action": "click", "wait_ms": 2000, "find": {"text": "设置"}}],
        sleep=lambda _s: None,
    )
    assert result["success"] is True
    assert result["commands"][0] == "wake"
    # real (400,600)@800x1280 → AOA 逻辑坐标 (180, 300)
    assert "click 180 300" in result["commands"]
    assert "sleep 2000" in result["commands"]


def test_parse_probe_cooked_point_from_dumpsys():
    """从真实 dumpsys input 结构中解析探针 cooked 坐标。"""
    dump = """  some other mapper
      Last Raw Touch:
        When: 1000
        Raw pointer data:
          0 pointers:
      Last Cooked Touch: pointerCount=0
      Last Raw Touch:
        When: 2000
        Raw pointer data:
          1 pointers:
            [0]: id=0, x=180, y=320, pressure=0, toolType=STYLUS, isHovering=0
          ID bits: hovering = 0x00000000, touching = 0x80000000
      Last Cooked Button State: 0x00000000
      Last Cooked Touch: pointerCount=1
        [0]: id=0, x=400.500, y=709.300, dx=0.000, pressure=1.000, toolType=STYLUS
"""
    assert aoa_actions._parse_probe_cooked_point(dump, 180, 320) == (400.5, 709.3)
    # 探针坐标不匹配 → None（回退按轴换算）
    assert aoa_actions._parse_probe_cooked_point(dump, 100, 200) is None


def test_calibration_failure_still_releases_probe(monkeypatch):
    """探针 UP 传输失败（Pipe error）也必须重试抬起并复位恢复。

    DOWN 成功而 UP 失败会在设备上留下"幽灵手指"，后续滑动手势全部
    失效（实机 8e4d 验证）；finally 必须尽力重发 UP 并恢复原生模式。
    """
    hid_events: list[bool] = []
    adb_calls: list[tuple] = []
    restored: list[str] = []

    class FakeDevice:
        def reset(self):
            restored.append("reset")

    class FakeUsbCore:
        @staticmethod
        def find(*_args, **_kwargs):
            return iter([])

    class FakeUsbUtil:
        @staticmethod
        def dispose_resources(_device):
            restored.append("dispose")

    def fake_transfer(_device, request, index, _value, payload=b""):
        if request == aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT and \
                index == aoa_profile.AOA_HID_TOUCH_SCREEN:
            hid_events.append(payload == aoa_profile.touch_report(True, 180, 320))
            if not hid_events[-1]:
                raise aoa_actions.AoaError("control transfer failed (request 57): pipe")
        return 0

    monkeypatch.setattr(aoa_actions, "_usb", lambda: (FakeUsbCore, FakeUsbUtil))
    monkeypatch.setattr(aoa_actions, "_configure_accessory", lambda _c, _s: FakeDevice())
    monkeypatch.setattr(aoa_actions, "_detach_kernel_drivers", lambda _d: None)
    monkeypatch.setattr(aoa_actions, "_register_all_hid", lambda _d, touch_mode="pointer": None)
    monkeypatch.setattr(aoa_actions, "_transfer", fake_transfer)
    monkeypatch.setattr(aoa_actions, "_open_device", lambda _c, _s: None)
    monkeypatch.setattr(
        aoa_actions, "_adb_run",
        lambda _serial, *args, **_kw: (adb_calls.append(args), type(
            "R", (), {"returncode": 0, "stdout": "", "stderr": ""}))[-1])
    result = aoa_actions._calibrate_touch_mapping("serial", 800, 1280, lambda _t: None)
    assert result is None
    # DOWN 一次 + UP 两次（try 内失败一次、finally 重试一次）。
    assert hid_events == [True, False, False]
    # 幽灵手指防护：finally 里对长按补发 BACK 清理上下文菜单。
    assert any("keyevent" in args and "4" in args for args in adb_calls)
    assert "reset" in restored and "dispose" in restored


def test_record_uses_measured_calibration(monkeypatch):
    """统一比例设备上，录制坐标必须按实测映射反解
    （(594,1140)@800x1280 → (267,514)，按轴换算会错成 (267,570)）。"""

    class FakeCompleted:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(aoa_actions, "_display_size", lambda _serial: (800, 1280))
    monkeypatch.setattr(aoa_actions, "_adb_run",
                        lambda _serial, *args: FakeCompleted())
    from foundation.aoa_calibration import five_point_calibration_points, mapping_from_samples

    measured = mapping_from_samples("pointer",
        [((x, y), (x * 2.2, y * 2.2)) for x, y in five_point_calibration_points(360, 640)],
        display_pixels=(800, 1280),
        validation_samples=[((120, 213), (264, 468.6)), ((240, 427), (528, 939.4))])
    monkeypatch.setattr(aoa_actions, "_calibrate_touch_affine", lambda *a, **_kw: measured)
    monkeypatch.setattr(
        aoa_actions, "_dump_elements",
        lambda _serial, _sleep=None: (
            [{"text": "Chrome", "content_desc": "", "clickable": True,
              "center": [594, 900]}],   # 在遮蔽线（1280-170）之上
            {},
        ),
    )
    result = aoa_actions.record_aoa_profile(
        "8e4dbf3781825b31",
        [{"action": "click", "wait_ms": 1000, "find": {"text": "Chrome"}}],
        calibrate_touch=True,
        sleep=lambda _s: None,
    )
    assert "click 270 409" in result["commands"]   # 900/2.2 = 409
    # 按轴换算（旧逻辑）会错成 (267, 570)——这正是实机上导航失效的原因。
    assert "click 267 570" not in result["commands"]
    assert "click 267 514" not in result["commands"]


def test_record_uses_affine_mapping_for_profile_commands(monkeypatch):
    """Profile recording must use the five-point affine mapping, not only scales."""
    from foundation.aoa_calibration import five_point_calibration_points, mapping_from_samples

    class FakeCompleted:
        returncode = 0
        stdout = ""

    context = {"version": 3, "model": "RK3576", "release": "16",
               "display": [800, 1280], "rotation": 0, "hid_touch_mode": "direct"}
    mapping = mapping_from_samples(
        "direct",
        [((x, y), (x * 2.2, y * 2.2 + 60))
         for x, y in five_point_calibration_points(360, 640)],
        context=context, display_pixels=(800, 1280),
        validation_samples=[((120, 213), (264, 528.6)), ((240, 427), (528, 999.4))],
    )
    monkeypatch.setattr(aoa_actions, "_display_size", lambda _serial: (800, 1280))
    monkeypatch.setattr(aoa_actions, "_adb_run",
                        lambda _serial, *args: FakeCompleted())
    monkeypatch.setattr(aoa_actions, "_calibrate_touch_affine",
                        lambda *args, **kwargs: mapping)
    monkeypatch.setattr(aoa_actions, "_dump_elements",
                        lambda _serial, _sleep=None: (
                            [{"text": "Chrome", "content_desc": "", "clickable": True,
                              "center": [400, 700]}], {},))
    result = aoa_actions.record_aoa_profile(
        "serial", [{"action": "click", "wait_ms": 100, "find": {"text": "Chrome"}}],
        calibrate_touch=True, touch_mode="direct", sleep=lambda _s: None)
    assert "click 182 291" in result["commands"]
    assert result["touch_calibration"]["mapping"]["context"] == context


def test_run_sequence_mocked_hid(monkeypatch):
    """全链路（accessory 切换→HID 注册→动作→ADB 等待）用桩验证。"""
    state = {"accessory": False, "registered": [], "events": [], "transfers": []}

    class FakeDevice:
        # 属性名与 pyusb 保持一致（idVendor/idProduct/bNumInterfaces），

        idVendor = 0x18D1  # noqa: N815
        idProduct = 0x2D00  # noqa: N815
        serial_number = "8e4dbf3781825b31"
        bNumInterfaces = 0  # noqa: N815

        @staticmethod
        def is_kernel_driver_active(_interface):
            return False

        def reset(self):
            type(self).idProduct = 0x1234
            state["reset"] = state.get("reset", 0) + 1

        def ctrl_transfer(self, request_type, request, value, index, data, timeout):
            state["transfers"].append((request, value, index, bytes(data)))
            if request == aoa_profile.AOA_ACCESSORY_GET_PROTOCOL:
                return b"\x02\x00"
            if request == aoa_profile.AOA_ACCESSORY_SEND_STRING:
                return len(data)
            if request == aoa_profile.AOA_ACCESSORY_START:
                state["accessory"] = True
                return 0
            if request == aoa_profile.AOA_ACCESSORY_REGISTER_HID:
                state["registered"].append(value)
                return 0
            if request == aoa_profile.AOA_ACCESSORY_SET_HID_REPORT_DESC:
                return len(data)
            if request == aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT:
                state["events"].append((value, bytes(data)))
                return len(data)
            if request == aoa_profile.AOA_ACCESSORY_UNREGISTER_HID:
                return 0
            raise AssertionError(f"unexpected request {request}")

    class FakeCore:
        class USBError(Exception):
            pass

        @staticmethod
        def find(**_kwargs):
            return iter([FakeDevice()])

    monkeypatch.setattr(aoa_actions, "_usb", lambda: (FakeCore(), None))
    monkeypatch.setattr(aoa_actions, "_wait_adb_state",
                        lambda serial, timeout: {"adb_state": "device", "adb_recovered": True})
    result = aoa_actions.run_aoa_sequence(
        "8e4dbf3781825b31",
        ["wake", "click 10 20", "key dpad_up", "write abc"],
        wait_adb_seconds=30,
        sleep=lambda _seconds: None,
    )
    assert result["success"] is True
    assert result["commands_executed"] == 4
    assert result["adb_state"] == "device"
    # 序列结束后 USB reset 退出 accessory 模式（实机验证的恢复路径）
    assert state.get("reset") == 1
    # 三个 HID 都被注册（3=触摸 1=键盘 2=系统）
    assert sorted(state["registered"]) == [1, 2, 3]
    # 系统键 wake 与按键事件都发出；触摸包含 down+up
    assert (aoa_profile.AOA_HID_SYSTEM, bytes([1])) in state["events"]
    touch_events = [data for hid, data in state["events"] if hid == aoa_profile.AOA_HID_TOUCH_SCREEN]
    assert touch_events[0][0] == 0b11 and touch_events[-1][0] == 0b00


def test_run_sequence_reports_progress_per_command(monkeypatch):
    """每条命令（含 sleep）执行后回调 (index, total, 原文本)，序号从 1 起。"""
    state = {"accessory": False, "registered": [], "events": [], "transfers": []}

    class FakeDevice:
        idVendor = 0x18D1  # noqa: N815
        idProduct = 0x2D00  # noqa: N815
        serial_number = "progress-device"
        bNumInterfaces = 0  # noqa: N815

        @staticmethod
        def is_kernel_driver_active(_interface):
            return False

        def reset(self):
            pass

        def ctrl_transfer(self, request_type, request, value, index, data, timeout):
            if request == aoa_profile.AOA_ACCESSORY_GET_PROTOCOL:
                return b"\x02\x00"
            if request == aoa_profile.AOA_ACCESSORY_SEND_STRING:
                return len(data)
            if request == aoa_profile.AOA_ACCESSORY_START:
                state["accessory"] = True
                return 0
            if request in (aoa_profile.AOA_ACCESSORY_REGISTER_HID,
                           aoa_profile.AOA_ACCESSORY_SET_HID_REPORT_DESC,
                           aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT,
                           aoa_profile.AOA_ACCESSORY_UNREGISTER_HID):
                return len(data) if isinstance(data, (bytes, bytearray)) else 0
            raise AssertionError(f"unexpected request {request}")

    class FakeCore:
        class USBError(Exception):
            pass

        @staticmethod
        def find(**_kwargs):
            return iter([FakeDevice()])

    monkeypatch.setattr(aoa_actions, "_usb", lambda: (FakeCore(), None))
    monkeypatch.setattr(aoa_actions, "_wait_adb_state",
                        lambda serial, timeout: {"adb_state": "none", "adb_recovered": False})
    commands = ["wake", "sleep 800", "click 10 20"]
    progress: list[tuple[int, int, str]] = []
    result = aoa_actions.run_aoa_sequence(
        "progress-device", commands, sleep=lambda _seconds: None,
        on_progress=lambda index, total, text: progress.append((index, total, text)),
    )
    assert result["commands_executed"] == 3
    assert [item[0] for item in progress] == [1, 2, 3]
    assert all(item[1] == 3 for item in progress)
    assert [item[2] for item in progress] == commands


def test_run_sequence_progress_callback_error_does_not_abort(monkeypatch):
    """进度回调异常只告警，不得中断命令序列执行。"""

    class FakeDevice:
        idVendor = 0x18D1  # noqa: N815
        idProduct = 0x2D00  # noqa: N815
        serial_number = "progress-error-device"
        bNumInterfaces = 0  # noqa: N815

        @staticmethod
        def is_kernel_driver_active(_interface):
            return False

        def reset(self):
            pass

        def ctrl_transfer(self, request_type, request, value, index, data, timeout):
            if request == aoa_profile.AOA_ACCESSORY_GET_PROTOCOL:
                return b"\x02\x00"
            if request == aoa_profile.AOA_ACCESSORY_SEND_STRING:
                return len(data)
            if request in (aoa_profile.AOA_ACCESSORY_START,
                           aoa_profile.AOA_ACCESSORY_REGISTER_HID,
                           aoa_profile.AOA_ACCESSORY_SET_HID_REPORT_DESC,
                           aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT,
                           aoa_profile.AOA_ACCESSORY_UNREGISTER_HID):
                return len(data) if isinstance(data, (bytes, bytearray)) else 0
            raise AssertionError(f"unexpected request {request}")

    class FakeCore:
        class USBError(Exception):
            pass

        @staticmethod
        def find(**_kwargs):
            return iter([FakeDevice()])

    monkeypatch.setattr(aoa_actions, "_usb", lambda: (FakeCore(), None))
    monkeypatch.setattr(aoa_actions, "_wait_adb_state",
                        lambda serial, timeout: {"adb_state": "none", "adb_recovered": False})

    def boom(_index, _total, _text):
        raise RuntimeError("callback exploded")

    result = aoa_actions.run_aoa_sequence(
        "progress-error-device", ["home", "home"], sleep=lambda _s: None,
        on_progress=boom,
    )
    assert result["success"] is True
    assert result["commands_executed"] == 2


def _stall_device_factory(state, stall_channels):
    """构造带部分 HID 通道 STALL 的假设备（模拟 RK3588 gadget 行为）。"""

    class FakeDevice:
        idVendor = 0x18D1  # noqa: N815
        idProduct = 0x1234 if state.get("reset") else 0x2D00  # noqa: N815
        serial_number = "partial-hid-device"
        bNumInterfaces = 0  # noqa: N815

        @staticmethod
        def is_kernel_driver_active(_interface):
            return False

        def reset(self):
            type(self).idProduct = 0x1234
            state["reset"] = state.get("reset", 0) + 1

        def ctrl_transfer(self, request_type, request, value, index, data, timeout):
            if value in stall_channels and request in (
                    aoa_profile.AOA_ACCESSORY_REGISTER_HID,
                    aoa_profile.AOA_ACCESSORY_SET_HID_REPORT_DESC,
                    aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT):
                raise OSError("[Errno 32] Pipe error")
            if request == aoa_profile.AOA_ACCESSORY_GET_PROTOCOL:
                return b"\x02\x00"
            if request == aoa_profile.AOA_ACCESSORY_SEND_STRING:
                return len(data)
            if request == aoa_profile.AOA_ACCESSORY_START:
                return 0
            if request == aoa_profile.AOA_ACCESSORY_REGISTER_HID:
                state["registered"].append(value)
                return 0
            if request == aoa_profile.AOA_ACCESSORY_SET_HID_REPORT_DESC:
                return len(data)
            if request == aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT:
                state["events"].append((value, bytes(data)))
                return len(data)
            if request == aoa_profile.AOA_ACCESSORY_UNREGISTER_HID:
                return 0
            raise AssertionError(f"unexpected request {request}")

    return FakeDevice()


def _stall_usb(monkeypatch, state, stall_channels):
    class FakeCore:
        class USBError(Exception):
            pass

        @staticmethod
        def find(**_kwargs):
            return iter([_stall_device_factory(state, stall_channels)])

    monkeypatch.setattr(aoa_actions, "_usb", lambda: (FakeCore(), None))


def test_run_sequence_tolerates_partial_hid_stall(monkeypatch):
    """设备 gadget 只支持部分 HID 通道时（实机 RK3588 触摸通道 STALL）：
    注册尽力而为，未受影响通道的动作照常执行，序列仍成功。"""
    state: dict = {"registered": [], "events": []}
    _stall_usb(monkeypatch, state, stall_channels={aoa_profile.AOA_HID_TOUCH_SCREEN})
    result = aoa_actions.run_aoa_sequence(
        "partial-hid-device", ["wake"], wait_adb_seconds=0, sleep=lambda _s: None)
    assert result["success"] is True
    # wake 只需系统通道；不注册无关的触摸和键盘设备。
    assert state["registered"] == [2]
    assert state["events"][0][0] == aoa_profile.AOA_HID_SYSTEM
    assert all(hid != aoa_profile.AOA_HID_TOUCH_SCREEN for hid, _ in state["events"])


def test_run_sequence_rejects_unsupported_channel(monkeypatch):
    """动作依赖的通道被设备 STALL 时：给出指明通道号的明确错误，
    而不是底层 Pipe error。"""
    state: dict = {"registered": [], "events": []}
    _stall_usb(monkeypatch, state, stall_channels={2})
    with pytest.raises(aoa_actions.AoaError, match="channel 2"):
        aoa_actions.run_aoa_sequence(
            "partial-hid-device", ["wake"], wait_adb_seconds=0, sleep=lambda _s: None)


def test_run_sequence_sleep_only_with_all_stall(monkeypatch):
    """全部通道 STALL 时，纯 sleep 序列不涉及 HID 发送，仍然成功。"""
    state: dict = {"registered": [], "events": []}
    _stall_usb(monkeypatch, state, stall_channels={1, 2, 3})
    result = aoa_actions.run_aoa_sequence(
        "partial-hid-device", ["sleep 500"], wait_adb_seconds=0, sleep=lambda _s: None)
    assert result["success"] is True
    assert result["commands_executed"] == 1
    assert state["registered"] == []


def test_accessory_timeout_does_not_return_native_device(monkeypatch):
    from types import SimpleNamespace

    native = SimpleNamespace(idVendor=0x1234, idProduct=0x5678)
    monkeypatch.setattr(aoa_actions, "_open_device", lambda *a: native)
    ticks = iter([0, 21])
    monkeypatch.setattr(aoa_actions.time, "monotonic", lambda: next(ticks))
    with pytest.raises(aoa_actions.AoaError, match="timed out"):
        aoa_actions._configure_accessory(None, "serial")


def test_adb_state_does_not_retain_disconnected_device(monkeypatch):
    from types import SimpleNamespace

    outputs = iter(["List of devices attached\ns1 unauthorized\n",
                    "List of devices attached\n"])
    monkeypatch.setattr(aoa_actions.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(stdout=next(outputs)))
    ticks = iter([0, 0, 3])
    monkeypatch.setattr(aoa_actions.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(aoa_actions.time, "sleep", lambda _: None)
    result = aoa_actions._wait_adb_state("s1", 2)
    assert result["adb_state"] == "absent"
    assert result["adb_recovered"] is False


def test_adb_state_accepts_usb_port_with_and_without_prefix(monkeypatch):
    """platform-tools 版本差异：端口字段可能是 ``usb:1-6`` 或裸 ``1-6``。

    本机 adb 1.0.41 输出裸端口，老版本带 ``usb:`` 前缀；绑定端口正确时
    两种都必须判 device，不得误报 identity_mismatch（RK3576 实测回归）。
    """
    from types import SimpleNamespace

    from worker_agent.aoa_usb import bind_usb

    monkeypatch.setattr(aoa_actions.time, "sleep", lambda _: None)
    monkeypatch.setattr(aoa_actions.subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout="List of devices attached\ns1 device 1-6 product:p transport_id:9\n",
        returncode=0))

    with bind_usb("1-6"):
        ticks = iter([0, 1])
        monkeypatch.setattr(aoa_actions.time, "monotonic", lambda: next(ticks))
        result = aoa_actions._wait_adb_state("s1", 5)
    assert result["adb_recovered"] is True
    assert result["adb_state"] == "device"

    with bind_usb("1-6"):
        ticks = iter([0, 1])
        monkeypatch.setattr(aoa_actions.time, "monotonic", lambda: next(ticks))
        monkeypatch.setattr(aoa_actions.subprocess, "run", lambda *a, **k: SimpleNamespace(
            stdout="List of devices attached\ns1 device usb:1-6 product:p transport_id:9\n",
            returncode=0))
        result = aoa_actions._wait_adb_state("s1", 5)
    assert result["adb_recovered"] is True

    with bind_usb("1-6"):
        ticks = iter([0, 1])
        monkeypatch.setattr(aoa_actions.time, "monotonic", lambda: next(ticks))
        monkeypatch.setattr(aoa_actions.subprocess, "run", lambda *a, **k: SimpleNamespace(
            stdout="List of devices attached\ns1 device 2-1 product:p transport_id:9\n",
            returncode=0))
        result = aoa_actions._wait_adb_state("s1", 1)
    assert result["adb_recovered"] is False
    assert result["adb_state"] == "identity_mismatch"


def test_long_click_runtime_includes_hold_and_pacing():
    from foundation.aoa_profile import _estimate_runtime_ms

    assert _estimate_runtime_ms(parse_profile_commands(["longClick 1 1"])[0]) >= 2000
    with pytest.raises(ValueError):
        parse_profile_commands(["sleep 100 extra"])


def test_duplicate_usb_serial_is_rejected():
    from types import SimpleNamespace

    core = SimpleNamespace(find=lambda **k: iter([object(), object()]))
    with pytest.raises(aoa_actions.AoaError, match="ambiguous"):
        aoa_actions._open_device(core, "duplicate")


def test_aoav1_rejected_before_accessory_start(monkeypatch):
    from types import SimpleNamespace

    calls = []

    def transfer(*args, **kwargs):
        calls.append(args[1])
        return [1, 0]

    native = SimpleNamespace(idVendor=0x1234, idProduct=0x5678, ctrl_transfer=transfer)
    monkeypatch.setattr(aoa_actions, "_open_device", lambda *a: native)
    with pytest.raises(aoa_actions.AoaError, match="does not support"):
        aoa_actions._configure_accessory(None, "serial")
    assert calls == [aoa_profile.AOA_ACCESSORY_GET_PROTOCOL]


def test_register_retries_transient_stall_before_touch(monkeypatch):
    state = {"registered": [], "events": []}
    _stall_usb(monkeypatch, state, stall_channels=set())
    original = aoa_actions._transfer
    attempts = []

    def transient(device, request, value, index, data=b""):
        if request == aoa_profile.AOA_ACCESSORY_REGISTER_HID and value == aoa_profile.AOA_HID_TOUCH_SCREEN:
            attempts.append(value)
            if len(attempts) == 1:
                raise aoa_actions.AoaError("temporary ep0 STALL")
        return original(device, request, value, index, data)

    monkeypatch.setattr(aoa_actions, "_transfer", transient)
    result = aoa_actions.run_aoa_sequence(
        "partial-hid-device", ["click 100 100"], sleep=lambda _: None)
    assert result["success"]
    assert len(attempts) == 2
    assert sum(hid == 3 and data[0] == 0b11 for hid, data in state["events"]) == 1


def test_touch_registration_avoids_zero_hid_id(monkeypatch):
    state = {"registered": [], "events": []}
    _stall_usb(monkeypatch, state, stall_channels={0})
    result = aoa_actions.run_aoa_sequence(
        "partial-hid-device", ["click 100 100"], sleep=lambda _: None)
    assert result["success"]
    assert 0 not in state["registered"]
    assert state["events"][:2] == [(3, touch_report(True, 100, 100)),
                               (3, touch_report(False, 100, 100))]


def _gate_probe_usb(monkeypatch):
    """run_aoa_sequence 抑制门控测试共用的最小 USB 桩。"""
    state = {"accessory": False, "registered": [], "events": [], "transfers": []}

    class FakeDevice:
        idVendor = 0x18D1  # noqa: N815
        idProduct = 0x2D00  # noqa: N815
        serial_number = "gate-device"
        bNumInterfaces = 0  # noqa: N815

        @staticmethod
        def is_kernel_driver_active(_interface):
            return False

        def reset(self):
            pass

        def ctrl_transfer(self, request_type, request, value, index, data, timeout):
            if request == aoa_profile.AOA_ACCESSORY_GET_PROTOCOL:
                return b"\x02\x00"
            if request == aoa_profile.AOA_ACCESSORY_SEND_STRING:
                return len(data)
            if request == aoa_profile.AOA_ACCESSORY_START:
                state["accessory"] = True
                return 0
            if request in (aoa_profile.AOA_ACCESSORY_REGISTER_HID,
                           aoa_profile.AOA_ACCESSORY_SET_HID_REPORT_DESC,
                           aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT,
                           aoa_profile.AOA_ACCESSORY_UNREGISTER_HID):
                return len(data) if isinstance(data, (bytes, bytearray)) else 0
            raise AssertionError(f"unexpected request {request}")

    class FakeCore:
        class USBError(Exception):
            pass

        @staticmethod
        def find(**_kwargs):
            return iter([FakeDevice()])

    monkeypatch.setattr(aoa_actions, "_usb", lambda: (FakeCore(), None))
    monkeypatch.setattr(aoa_actions, "_wait_adb_state",
                        lambda serial, timeout: {"adb_state": "device", "adb_recovered": True})
    return state


def test_suppression_gate_overrides_static_bool_at_arm_time(monkeypatch):
    """门控在武装瞬间求值：True 才抑制，False 覆盖静态布尔（不杀 adb）。"""
    _gate_probe_usb(monkeypatch)
    sessions = []

    class FakeSession:
        def __init__(self, label):
            self.label = label

        def end(self):
            sessions.append(self.label)

    begins = []
    monkeypatch.setattr(aoa_actions, "_begin_adb_suppression",
                        lambda: begins.append(1) or FakeSession("s"))

    gate_results = iter([True])
    result = aoa_actions.run_aoa_sequence(
        "gate-device", ["wake", "click 10 20"], sleep=lambda _s: None,
        suppress_adb_daemon=True,
        suppress_adb_daemon_gate=lambda: next(gate_results),
    )
    assert result["success"] is True
    assert len(begins) == 1 and sessions == ["s"]

    begins.clear()
    result = aoa_actions.run_aoa_sequence(
        "gate-device", ["wake", "click 10 20"], sleep=lambda _s: None,
        suppress_adb_daemon=True,
        suppress_adb_daemon_gate=lambda: False,
    )
    assert result["success"] is True
    assert begins == [] and sessions == ["s"]


def test_suppression_gate_exception_is_fail_open(monkeypatch):
    """门控自身异常按不抑制处理：误杀无关 adb 的代价高于放弃抑制。"""
    _gate_probe_usb(monkeypatch)
    begins = []
    monkeypatch.setattr(aoa_actions, "_begin_adb_suppression",
                        lambda: begins.append(1))

    def broken_gate():
        raise RuntimeError("job inventory unavailable")

    result = aoa_actions.run_aoa_sequence(
        "gate-device", ["wake", "click 10 20"], sleep=lambda _s: None,
        suppress_adb_daemon=True,
        suppress_adb_daemon_gate=broken_gate,
    )
    assert result["success"] is True
    assert begins == []


def test_static_bool_false_skips_gate_evaluation(monkeypatch):
    """静态布尔为假（快路径预过滤）时门控不求值、不抑制。"""
    _gate_probe_usb(monkeypatch)
    begins = []
    gate_calls = []
    monkeypatch.setattr(aoa_actions, "_begin_adb_suppression",
                        lambda: begins.append(1))

    result = aoa_actions.run_aoa_sequence(
        "gate-device", ["wake", "click 10 20"], sleep=lambda _s: None,
        suppress_adb_daemon=False,
        suppress_adb_daemon_gate=lambda: gate_calls.append(1) or True,
    )
    assert result["success"] is True
    assert begins == [] and gate_calls == []
