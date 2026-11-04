"""AOA 录制（userdebug → user 回放）的单元测试（全桩，不碰真机）。"""

from __future__ import annotations

import pytest

from foundation.aoa_profile import (
    AOA_TOUCH_CALIBRATION_VERSION,
    PRESET_JOURNEYS,
    parse_profile_commands,
    parse_record_input_action,
    parse_record_steps,
    real_to_aoa_point,
)
from worker_agent import aoa_actions


def test_coordinate_mapping_matches_hid_descriptor():
    # 官方触摸 HID 逻辑分辨率 360x640：真实分辨率线性映射后 clamp。
    assert real_to_aoa_point(0, 0, 1920, 1080) == (0, 0)
    assert real_to_aoa_point(1920, 1080, 1920, 1080) == (360, 640)
    assert real_to_aoa_point(960, 540, 1920, 1080) == (180, 320)
    # 1000px < 1080px：AOSP 分母 max+1 映射到 594（未越界不 clamp）。
    assert real_to_aoa_point(960, 1000, 1920, 1080) == (180, 594)
    # 越界才 clamp 到描述符上限。
    assert real_to_aoa_point(1920, 1200, 1920, 1080) == (360, 640)
    with pytest.raises(ValueError):
        real_to_aoa_point(1, 1, 0, 1080)


def test_interactive_record_input_validation():
    assert parse_record_input_action({"action": "tap", "x": 400, "y": 600}) == {
        "action": "tap", "x": 400, "y": 600,
    }
    swipe = parse_record_input_action({
        "action": "swipe", "x1": 10, "y1": 20, "x2": 30, "y2": 40,
        "duration_ms": 350, "scale_x": 2.2, "scale_y": 2.2,
        "calibration_context": {"version": AOA_TOUCH_CALIBRATION_VERSION, "model": "model", "release": "16", "display": [800, 1280], "rotation": 0},
    })
    assert swipe["duration_ms"] == 350 and swipe["scale_y"] == 2.2
    for invalid in [
        {"action": "shell"},
        {"action": "tap", "x": -1, "y": 1},
        {"action": "write", "text": "中文"},
        {"action": "write", "text": "a\tb"},
        {"action": "write", "text": "-dash-start"},
        {"action": "sleep", "duration_ms": 20000},
    ]:
        with pytest.raises(ValueError):
            parse_record_input_action(invalid)


def test_interactive_record_input_executes_and_returns_command(monkeypatch):
    calls = []

    def fake_adb(_serial, *args, **_kwargs):
        calls.append(args)
        if args[:3] == ("shell", "wm", "size"):
            return _completed(0, "Physical size: 800x1280\nOverride size: 600x1000\n")
        return _completed(0, "")

    monkeypatch.setattr(aoa_actions, "_adb_run", fake_adb)
    result = aoa_actions.record_aoa_input(
        "serial", {"action": "tap", "x": 300, "y": 500})
    assert result["display"] == [600, 1000]
    assert result["command"] == "click 180 320"
    assert ("shell", "input", "tap", "300", "500") in calls


def test_record_steps_validation():
    steps = [
        {"action": "wake", "wait_ms": 500},
        {"action": "click", "find": {"any_text": ["设置", "Settings"]},
         "scroll_attempts": 3},
        {"action": "click", "locate_only": True,
         "find": {"resource_id": "com.android.settings:id/switch_bar"}},
        {"action": "scroll_down", "swipe_ms": 400},
        {"action": "back"},
    ]
    parsed = parse_record_steps(steps)
    assert parsed[1]["scroll_attempts"] == 3
    assert parsed[2]["locate_only"] is True
    assert parsed[3]["swipe_ms"] == 400
    # scroll_x：滚动横轴（双栏设置找左栏列表用）。
    assert parsed[1].get("scroll_x") == 0.5
    steps_x = [{"action": "click", "find": {"text": "x"},
                "scroll_x": 0.25}]
    assert parse_record_steps(steps_x)[0]["scroll_x"] == 0.25
    for bad in [
        [{"action": "launch", "find": {}}],                # 非白名单动作
        [{"action": "click", "find": {}}],                 # 空 matcher
        [{"action": "click", "find": {"text": "x"}, "scroll_attempts": 99}],
        [{"action": "click", "find": {"text": "x"}, "clicks": 99}],
        [{"action": "click", "find": {"text": "x"}, "scroll_x": 1.5}],
        [{"action": "click", "find": {"text": "x"}, "scroll_x": "abc"}],
        [{"action": "wake"}] * 257,                         # 步数上限（256）
        ["not-a-dict"],
    ]:
        with pytest.raises(ValueError):
            parse_record_steps(bad)


def test_preset_journeys_are_valid():
    # 预设旅程必须能通过录制校验；且产出的命令能通过回放校验。
    for name, steps in PRESET_JOURNEYS.items():
        assert parse_record_steps(steps), name


def test_usb_debugging_preset_recovers_from_home_screen():
    """桌面无设置图标的启动器（实机 8e4d）上旅程必须能自恢复。"""
    steps = PRESET_JOURNEYS["usb_debugging"]
    assert steps[0]["action"] == "wake"
    # 先回桌面：录制机可能停在任意界面。
    assert steps[1]["action"] == "home"
    settings = next(
        step for step in steps if step["action"] == "click"
        and any("设置" in values for values in step["find"].values()))
    # 找不到设置时逐屏上滑重找（打开应用抽屉）。
    assert settings["scroll_attempts"] >= 4


def test_record_scrolls_are_replayed(monkeypatch):
    """scroll_attempts 的重试滑动必须写入回放命令（保真性）。"""
    plain = ("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"
             '<hierarchy><node text="主屏幕" bounds="[0,0][1920,1080]"/></hierarchy>')
    # 每次 dump 消耗 2 个 payload（dump 写文件 + cat 读回），第 1 轮读回
    # 主屏幕（无目标）→ 上滑重找 → 第 2 轮读回命中。
    fake = _FakeAdb([plain, plain, _SAMPLE_DUMP, _SAMPLE_DUMP])
    monkeypatch.setattr(aoa_actions, "_adb_run", fake)
    result = aoa_actions.record_aoa_profile(
        "serial",
        [{"action": "click", "wait_ms": 100, "scroll_attempts": 1,
          "find": {"any_text": ["设置", "Settings"]}}],
        sleep=lambda _s: None)
    # 1920x1080 回退换算：长慢滑 960,810→960,162 即逻辑 (180,480)→(180,96)。
    assert result["commands"] == [
        "wake", "swipe 180 480 500 180 96", "sleep 800",
        "click 180 314", "sleep 100",
    ]
    # 产物仍能通过回放层校验。
    assert parse_profile_commands(result["commands"])


def test_recorded_commands_replay_on_aoa_profile():
    """录制产物必须能被回放校验接受（user 固件无 ADB 时唯一接口）。"""
    commands = [
        "wake", "click 180 320", "swipe 180 448 300 180 192",
        "click 12 596", "back", "home",
    ]
    assert parse_profile_commands(commands)


class _FakeAdb:
    """按调用序列返回的 adb 桩。"""

    def __init__(self, dumps: list[str]):
        self.dumps = dumps
        self.dump_index = 0
        self.calls: list[list[str]] = []

    def run(self, serial, *args, timeout=15):
        self.calls.append([*args])
        joined = " ".join(args)
        if joined.startswith("shell uiautomator dump") or joined.startswith("exec-out cat"):
            # dump 与 cat 同步推进；rm 忽略。
            if self.dump_index < len(self.dumps):
                payload = self.dumps[self.dump_index]
                self.dump_index += 1
                return _completed(0, payload)
            return _completed(0, "")
        if joined.startswith("shell wm size"):
            return _completed(0, "Physical size: 1920x1080\n")
        if joined.startswith("shell getprop ro.product.model"):
            return _completed(0, "SSI 16 on ARM64\n")
        if joined.startswith("shell getprop ro.build.version.release"):
            return _completed(0, "16\n")
        return _completed(0, "")

    def __call__(self, serial, *args, **kwargs):
        return self.run(serial, *args, **kwargs)


def _completed(code: int, stdout: str):
    from types import SimpleNamespace

    return SimpleNamespace(returncode=code, stdout=stdout, stderr="")


_SAMPLE_DUMP = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0"><node index="0" text="" resource-id="" content-desc=""
bounds="[0,0][1920,1080]"><node index="1" text="设置" resource-id=""
content-desc="设置" class="android.widget.TextView" clickable="false"
bounds="[860,500][1060,560]"/><node index="2" text="" resource-id=""
content-desc="设置" class="android.widget.FrameLayout" clickable="true"
bounds="[800,470][1120,590]"/></node></hierarchy>"""


def test_record_executes_and_produces_replayable_commands(monkeypatch):
    # 每次 uiautomator dump 消耗 2 个桩 payload（dump + cat）。
    fake = _FakeAdb([_SAMPLE_DUMP] * 4)
    monkeypatch.setattr(aoa_actions, "_adb_run", fake)
    result = aoa_actions.record_aoa_profile(
        "8e4dbf3781825b31",
        [
            {"action": "click", "wait_ms": 100,
             "find": {"any_text": ["设置", "Settings"]}},
            {"action": "click", "locate_only": True, "wait_ms": 100,
             "find": {"content_desc": "设置"}},
            {"action": "back", "wait_ms": 100},
        ],
        sleep=lambda _s: None,
    )
    assert result["success"] is True
    assert result["display"] == [1920, 1080]
    # 前置 wake + 点击点取可点击祖先 (960, 530)，换算 (180, 314)。
    assert result["commands"][0] == "wake"
    assert result["commands"][1] == "click 180 314"
    # locate_only 只记录不点击：实际 tap 只发生一次。
    taps = [call for call in fake.calls if call[:2] == ["shell", "input"]]
    assert sum(1 for call in taps if "tap" in call) == 1
    # 录制产物能被回放层接受。
    assert parse_profile_commands(result["commands"])


def test_record_optional_step_skips_and_failure_reports_texts(monkeypatch):
    # optional 步骤 scroll_attempts 默认 0：一次 dump（2 payload）后跳过。
    fake = _FakeAdb(["<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy><node text=\"主屏幕\" bounds=\"[0,0][10,10]\"/></hierarchy>"] * 2)
    monkeypatch.setattr(aoa_actions, "_adb_run", fake)
    result = aoa_actions.record_aoa_profile(
        "serial", [{"action": "click", "optional": True, "wait_ms": 100,
                    "find": {"text": "开发者选项"}}], sleep=lambda _s: None)
    assert result["steps"][0]["skipped"] is True

    # 严格匹配失败：dump+cat 各 1 次（2 payload），可见文本进入错误信息。
    strict = _FakeAdb(
        ["<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy><node text=\"主屏幕\" bounds=\"[0,0][10,10]\"/></hierarchy>"] * 2)
    monkeypatch.setattr(aoa_actions, "_adb_run", strict)
    with pytest.raises(aoa_actions.AoaError) as excinfo:
        aoa_actions.record_aoa_profile(
            "serial", [{"action": "click", "wait_ms": 100,
                        "find": {"text": "开发者选项"}}], sleep=lambda _s: None)
    assert "主屏幕" in str(excinfo.value)


def test_record_setup_wizard_flags(monkeypatch):
    fake = _FakeAdb([_SAMPLE_DUMP, _SAMPLE_DUMP])
    monkeypatch.setattr(aoa_actions, "_adb_run", fake)
    result = aoa_actions.record_aoa_profile(
        "serial", [{"action": "home", "wait_ms": 100}],
        setup_wizard=True, tv=True, sleep=lambda _s: None)
    assert result["wizard"]["global.device_provisioned"] is True
    assert result["wizard"]["secure.user_setup_complete"] is True
    assert result["wizard"]["secure.tv_setup_complete"] is True


def test_rescale_swipe_preserves_duration_and_rescales_both_points():
    assert aoa_actions._rescale_aoa_command(
        "swipe 100 200 500 200 400", 2, 2, 4, 4
    ) == "swipe 50 100 500 100 200"


def test_interactive_record_receipt_excludes_inline_screenshot(monkeypatch):
    monkeypatch.setattr(aoa_actions, "_display_size", lambda _: (720, 1280))
    monkeypatch.setattr(aoa_actions, "_adb_run", lambda *a, **k: _completed(0, ""))
    result = aoa_actions.record_aoa_input(
        "serial", {"action": "home", "with_screenshot": True})
    assert "image" not in result


@pytest.mark.parametrize("field,value", [("rotation", []), ("rotation", {}), ("rotation", True),
    ("version", True), ("display", [True, 1280]), ("model", "")])
def test_malformed_calibration_context_is_a_validation_error(field, value):
    context = {"version": AOA_TOUCH_CALIBRATION_VERSION, "model": "model", "display": [800, 1280], "rotation": 0}
    context[field] = value
    with pytest.raises(ValueError, match="calibration_context"):
        parse_record_input_action({"action": "tap", "x": 1, "y": 2, "scale_x": 2.2, "scale_y": 2.2,
                                   "calibration_context": context})
