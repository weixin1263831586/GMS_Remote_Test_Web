"""AOA USB 调试闭环恢复的单元测试（不触碰真实 USB/adb）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from worker_agent import aoa_recovery
from worker_agent.aoa_actions import AoaError


def _fake_adb_run(monkeypatch, calls=None):
    """替身 adb：记录调用并返回成功完成的 CompletedProcess。"""

    def fake_adb_run(serial, *args, timeout=15):
        if calls is not None:
            calls.append((serial, *args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(aoa_recovery, "_adb_run", fake_adb_run)


def _node(text="", resource_id="", node_class="", center=None,
          clickable=False, checked=False):
    return {"text": text, "resource_id": resource_id, "class": node_class,
            "clickable": clickable, "checked": checked, "center": center,
            "content_desc": ""}


def test_real_to_logical_scale_and_linear():
    # 实测 uniform 标定：real ÷ scale（RK3576: 3.3241）
    assert aoa_recovery.real_to_logical(173, 1450, (1200, 1920), 3.3241) == (52, 436)
    # 未标定：分辨率线性映射到 360x640
    assert aoa_recovery.real_to_logical(600, 960, (1200, 1920), None) == (180, 320)
    # clamp 不拒绝越界坐标
    assert aoa_recovery.real_to_logical(0, 0, (1200, 1920), None) == (0, 0)
    assert aoa_recovery.real_to_logical(2000, 3000, (1200, 1920), None) == (360, 640)


def test_real_to_logical_rejects_bad_scale():
    with pytest.raises(AoaError):
        aoa_recovery.real_to_logical(100, 100, (1200, 1920), 0.01)
    with pytest.raises(AoaError):
        aoa_recovery.real_to_logical(100, 100, (1200, 1920), 200)


def test_measure_usb_debug_switch_finds_row_switch(monkeypatch):
    _fake_adb_run(monkeypatch)
    dumps = [
        [_node(text="Bug report", center=(100, 300))],  # 第一屏无 USB 行
        [_node(text="USB debugging", center=(173, 1440)),
         _node(node_class="android.widget.Switch", center=(1084, 1458),
               checked=True)],
    ]

    def fake_dump(serial, sleep=None):
        return dumps.pop(0), {}

    monkeypatch.setattr(aoa_recovery, "_dump_elements", fake_dump)
    result = aoa_recovery.measure_usb_debug_switch("S1", sleep=lambda _s: None)
    assert result == {"real": (1084, 1458), "checked": True}


def test_measure_switch_picks_nearest_row(monkeypatch):
    # 同屏多行开关：必须取标题同行的那个，而不是全屏第一个。
    _fake_adb_run(monkeypatch)
    nodes = [
        _node(text="USB debugging", center=(173, 1440)),
        _node(node_class="android.widget.Switch", center=(1084, 300), checked=False),
        _node(node_class="android.widget.Switch", center=(1084, 1458), checked=True),
    ]
    monkeypatch.setattr(
        aoa_recovery, "_dump_elements", lambda serial, sleep=None: (nodes, {}))
    result = aoa_recovery.measure_usb_debug_switch("S1", sleep=lambda _s: None)
    assert result["real"] == (1084, 1458)
    assert result["checked"] is True


def test_measure_switch_matches_localized_title(monkeypatch):
    # 行标题随语言本地化（实测匈牙利语 "USB hibakeresés"）：USB 前缀兜底命中。
    _fake_adb_run(monkeypatch)
    nodes = [
        _node(text="USB hibakeresés", center=(184, 1435)),
        _node(node_class="android.widget.Switch", center=(1084, 1450),
              checked=False),
    ]
    monkeypatch.setattr(
        aoa_recovery, "_dump_elements", lambda serial, sleep=None: (nodes, {}))
    result = aoa_recovery.measure_usb_debug_switch("S1", sleep=lambda _s: None)
    assert result["real"] == (1084, 1450)
    assert result["checked"] is False


def test_measure_switch_enables_development_settings(monkeypatch):
    # 开机向导会重置开发模式：进入前必须经 adb 置位（否则深链被弹回桌面）。
    calls = []
    _fake_adb_run(monkeypatch, calls)
    nodes = [_node(text="USB debugging", center=(173, 1440)),
             _node(node_class="android.widget.Switch", center=(1084, 1458),
                   checked=True)]
    monkeypatch.setattr(
        aoa_recovery, "_dump_elements", lambda serial, sleep=None: (nodes, {}))
    aoa_recovery.measure_usb_debug_switch("S1", sleep=lambda _s: None)
    assert ("S1", "shell", "settings", "put", "global",
            "development_settings_enabled", "1") in calls


def test_measure_switch_not_found_after_scroll(monkeypatch):
    _fake_adb_run(monkeypatch)
    monkeypatch.setattr(
        aoa_recovery, "_dump_elements",
        lambda serial, sleep=None: ([_node(text="About phone", center=(100, 300))], {}))
    with pytest.raises(AoaError):
        aoa_recovery.measure_usb_debug_switch("S1", sleep=lambda _s: None)


def test_verify_requires_ok_point():
    with pytest.raises(AoaError):
        aoa_recovery.verify_usb_debug_loop("S1", None, ok_logical=None)


def test_verify_aborts_when_already_off(monkeypatch):
    _fake_adb_run(monkeypatch)
    monkeypatch.setattr(aoa_recovery, "_display_size", lambda serial: (1200, 1920))
    monkeypatch.setattr(aoa_recovery, "run_aoa_sequence", lambda *a, **k: (
        pytest.fail("USB debugging 已是 OFF 时不应触碰 AOA")))
    monkeypatch.setattr(
        aoa_recovery, "measure_usb_debug_switch",
        lambda serial, sleep=None: {"real": (173, 1450), "checked": False})
    result = aoa_recovery.verify_usb_debug_loop(
        "S1", (1049, 1063), scale=3.3241, sleep=lambda _s: None)
    assert result["success"] is False
    assert "OFF" in result["error"]


class _SequenceRecorder:
    """替身 run_aoa_sequence：记录 (命令, wait_adb_seconds) 并按脚本应答。"""

    def __init__(self, answers):
        self.calls = []
        self._answers = list(answers)

    def __call__(self, serial, commands, wait_adb_seconds=0):
        self.calls.append((list(commands), wait_adb_seconds))
        answer = self._answers.pop(0)
        return {"success": answer[0], "commands_total": 1,
                "commands_executed": 1, "adb_state": answer[1],
                "adb_recovered": answer[2]}


def _patch_dark_env(monkeypatch, *, dark="absent", native=True):
    monkeypatch.setattr(aoa_recovery, "_display_size", lambda serial: (1200, 1920))
    monkeypatch.setattr(aoa_recovery, "_wait_dark",
                        lambda serial, timeout_seconds=None, sleep=None: dark)
    monkeypatch.setattr(aoa_recovery, "_adb_state", lambda serial: dark)
    monkeypatch.setattr(aoa_recovery, "_ensure_native_mode",
                        lambda usb_core, serial: native)
    monkeypatch.setattr(aoa_recovery, "_usb_core", lambda: object())


def test_verify_loop_success_full_journey(monkeypatch):
    recorder = _SequenceRecorder([
        (True, "absent", False),   # 阶段 1：OFF 提交，adb 断开
        (True, "absent", False),   # 阶段 2：点开关，弹窗出现（adb 仍断开）
        (True, "device", True),    # 阶段 3：点 OK，adb 恢复
    ])
    monkeypatch.setattr(aoa_recovery, "run_aoa_sequence", recorder)
    _patch_dark_env(monkeypatch)
    result = aoa_recovery.verify_usb_debug_loop(
        "RK3576GMS1", None, ok_logical=(316, 320),
        switch_point=(173, 1450), scale=3.3241,
        wait_adb_seconds=45, sleep=lambda _s: None)
    assert result["success"] is True
    assert result["adb_recovered"] is True
    # 实测标定换算：real(173,1450)→logical(52,436)；OK 逻辑坐标直给。
    assert recorder.calls[0] == (["click 52 436"], 0)
    assert recorder.calls[1] == (["click 52 436"], 0)
    assert recorder.calls[2] == (["click 316 320"], 45)
    assert all(step["ok"] for step in result["steps"])
    assert result["switch"]["real"] == [173, 1450]
    assert result["ok"]["logical"] == [316, 320]


def test_verify_loop_fails_when_still_adb_after_off(monkeypatch):
    recorder = _SequenceRecorder([(True, "device", True)])
    monkeypatch.setattr(aoa_recovery, "run_aoa_sequence", recorder)
    _patch_dark_env(monkeypatch, dark="device")
    result = aoa_recovery.verify_usb_debug_loop(
        "S1", (1049, 1063), switch_point=(173, 1450), scale=3.3241,
        sleep=lambda _s: None)
    assert result["success"] is False
    assert len(recorder.calls) == 1  # OFF 失败后不再继续点弹窗
    assert result["steps"][-1]["phase"] == "off"


def test_verify_loop_fails_without_native_pid(monkeypatch):
    recorder = _SequenceRecorder([(True, "absent", False)])
    monkeypatch.setattr(aoa_recovery, "run_aoa_sequence", recorder)
    _patch_dark_env(monkeypatch, dark="absent", native=False)
    result = aoa_recovery.verify_usb_debug_loop(
        "S1", (1049, 1063), switch_point=(173, 1450), scale=3.3241,
        sleep=lambda _s: None)
    assert result["success"] is False
    assert len(recorder.calls) == 1


def test_verify_loop_fails_when_ok_misses(monkeypatch):
    recorder = _SequenceRecorder([
        (True, "absent", False),
        (True, "absent", False),
        (True, "absent", False),   # OK 点击送达但未命中，adb 未恢复
    ])
    monkeypatch.setattr(aoa_recovery, "run_aoa_sequence", recorder)
    _patch_dark_env(monkeypatch)
    result = aoa_recovery.verify_usb_debug_loop(
        "S1", None, ok_logical=(316, 320), switch_point=(173, 1450),
        scale=3.3241, wait_adb_seconds=10, sleep=lambda _s: None)
    assert result["success"] is False
    assert result["steps"][-1]["phase"] == "on"
    assert len(recorder.calls) == 3


def test_cli_ok_px_and_logical_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        aoa_recovery.main(["--serial", "S1", "--ok-px", "1", "2",
                           "--ok-logical", "3", "4"])


def test_cli_requires_ok_point():
    with pytest.raises(SystemExit):
        aoa_recovery.main(["--serial", "S1"])


def test_cli_passes_arguments_through(monkeypatch, capsys):
    captured = {}

    def fake_verify(serial, ok_point, **kwargs):
        captured["serial"] = serial
        captured["ok_point"] = ok_point
        captured.update(kwargs)
        return {"success": True, "steps": []}

    monkeypatch.setattr(aoa_recovery, "verify_usb_debug_loop", fake_verify)
    code = aoa_recovery.main([
        "--serial", "RK3576GMS1", "--ok-px", "1049", "1063",
        "--switch-px", "173", "1450", "--scale", "3.3241", "--wait-adb", "60"])
    assert code == 0
    assert captured["serial"] == "RK3576GMS1"
    assert captured["ok_point"] == (1049, 1063)
    assert captured["ok_logical"] is None
    assert captured["switch_point"] == (173, 1450)
    assert captured["scale"] == 3.3241
    assert captured["wait_adb_seconds"] == 60
    assert '"success": true' in capsys.readouterr().out
