"""AOA 会话期 adb daemon 抑制（adb_suppression）的单元测试。

不执行真实 ``adb kill-server``：通过 monkeypatch 替换模块级 _kill_adb_server
观察调用；跨进程 flag 走独立临时目录，测试间互不污染。
"""

from __future__ import annotations

import time

import pytest

from worker_agent import adb_suppression


@pytest.fixture()
def suppression_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GMS_WORKER_AOA_SUPPRESSION_DIR", str(tmp_path))
    monkeypatch.setattr(adb_suppression, "_SESSIONS", 0)
    killed: list[int] = []
    monkeypatch.setattr(adb_suppression, "_kill_adb_server", lambda: killed.append(1))
    return tmp_path, killed


def test_first_session_kills_daemon_and_sets_flag(suppression_env, monkeypatch):
    _tmp, killed = suppression_env
    assert adb_suppression.adb_daemon_suppressed() is False

    session = adb_suppression.begin_suppression()
    try:
        assert killed == [1]
        assert adb_suppression.adb_daemon_suppressed() is True
    finally:
        session.end()

    assert adb_suppression.adb_daemon_suppressed() is False


def test_concurrent_sessions_kill_once_and_restore_on_last(suppression_env):
    _tmp, killed = suppression_env
    first = adb_suppression.begin_suppression()
    second = adb_suppression.begin_suppression()
    assert killed == [1]
    first.end()
    # 还有活动会话：守护进程保持被抑制，flag 仍在。
    assert adb_suppression.adb_daemon_suppressed() is True
    second.end()
    assert adb_suppression.adb_daemon_suppressed() is False


def test_double_end_is_idempotent(suppression_env):
    session = adb_suppression.begin_suppression()
    session.end()
    session.end()
    assert adb_suppression.adb_daemon_suppressed() is False


def test_stale_flag_from_crashed_session_expires(suppression_env, monkeypatch):
    stale_time = time.time() - (adb_suppression.FLAG_TTL_SECONDS + 60)
    flag = adb_suppression._flag_path()
    flag.write_text("999", encoding="ascii")
    import os

    os.utime(flag, (stale_time, stale_time))
    assert adb_suppression.adb_daemon_suppressed() is False

    # 会话重开会覆盖 stale flag（时间戳刷新），TTL 只在无存活会话时兜底。
    session = adb_suppression.begin_suppression()
    try:
        assert adb_suppression.adb_daemon_suppressed() is True
    finally:
        session.end()


def test_probe_devices_skips_adb_enumeration_while_suppressed(
    tmp_path, monkeypatch,
):
    """probe_devices 在抑制窗口内不产生 adb 枚举输出，fastboot/usb_only 照常。"""
    from worker_agent import device_actions

    monkeypatch.setenv("GMS_WORKER_AOA_SUPPRESSION_DIR", str(tmp_path))
    monkeypatch.setattr(adb_suppression, "_SESSIONS", 0)
    monkeypatch.setattr(adb_suppression, "_kill_adb_server", lambda: None)
    monkeypatch.setattr(
        device_actions, "_adb_daemon_suppressed", lambda: True)

    calls: list[list[str]] = []

    def fake_run(argv, timeout=10, env=None):
        calls.append(list(argv))
        if argv[:1] == ["adb"]:
            return "List of devices attached\nSERIAL\tdevice usb:1-6\n"
        if argv[:1] == ["fastboot"]:
            return "FASTSERIAL\tfastboot\n"
        return ""

    monkeypatch.setattr(device_actions, "_run", fake_run)
    monkeypatch.setattr(
        device_actions, "_probe_usb_only_devices", lambda known: [])

    devices = device_actions.probe_devices()

    assert not any(argv[:1] == ["adb"] for argv in calls)
    assert [item["serial"] for item in devices] == ["FASTSERIAL"]
    assert devices[0]["state"] == "fastboot"


def test_probe_devices_enumerates_adb_when_not_suppressed(tmp_path, monkeypatch):
    from worker_agent import device_actions

    monkeypatch.setenv("GMS_WORKER_AOA_SUPPRESSION_DIR", str(tmp_path))
    monkeypatch.setattr(
        device_actions, "_adb_daemon_suppressed", lambda: False)

    def fake_run(argv, timeout=10, env=None):
        if argv[:1] == ["adb"]:
            return "List of devices attached\nRK3576GMS1\tdevice usb:1-6\n"
        return ""

    monkeypatch.setattr(device_actions, "_run", fake_run)
    monkeypatch.setattr(
        device_actions, "_probe_usb_only_devices", lambda known: [])

    devices = device_actions.probe_devices()
    assert [item["serial"] for item in devices] == ["RK3576GMS1"]
    assert devices[0]["state"] == "available"


def test_per_pid_flags_survive_other_process_session_end(tmp_path, monkeypatch):
    """跨进程：A 进程结束只删自己的 flag，B 进程的抑制不受影响。"""
    import os

    monkeypatch.setenv("GMS_WORKER_AOA_SUPPRESSION_DIR", str(tmp_path))
    pid_holder = {"pid": 100}
    monkeypatch.setattr(os, "getpid", lambda: pid_holder["pid"])

    uid = getattr(os, "getuid", lambda: 0)()
    flag_a = tmp_path / f"gms-aoa-adb-suppression-{uid}-100.flag"
    flag_b = tmp_path / f"gms-aoa-adb-suppression-{uid}-200.flag"

    # 进程 A 开会话（写 pid-100 flag）
    adb_suppression._SESSIONS = 0
    session_a = adb_suppression.begin_suppression()
    assert flag_a.exists()
    assert adb_suppression.adb_daemon_suppressed() is True

    # 进程 B 开会话（独立计数、写 pid-200 flag）
    pid_holder["pid"] = 200
    adb_suppression._SESSIONS = 0
    session_b = adb_suppression.begin_suppression()
    assert flag_b.exists()

    # 进程 A 结束自己的会话：只删 pid-100，B 的抑制必须存活
    pid_holder["pid"] = 100
    adb_suppression._SESSIONS = 1
    session_a.end()
    assert not flag_a.exists()
    assert flag_b.exists()
    assert adb_suppression.adb_daemon_suppressed() is True

    # 进程 B 结束：目录清空，抑制解除
    pid_holder["pid"] = 200
    adb_suppression._SESSIONS = 1
    session_b.end()
    assert not flag_b.exists()
    assert adb_suppression.adb_daemon_suppressed() is False
