from pathlib import Path


SCRIPT = Path("features/devices/ui/page.js").read_text(
    encoding="utf-8", errors="ignore"
)


def test_port_refresh_rejects_stale_responses_and_deduplicates_background_polling():
    assert "portsRequestGeneration" in SCRIPT
    assert "portsRequestsInFlight" in SCRIPT
    assert "if (silent && state.portsRequestsInFlight > 0) return;" in SCRIPT
    assert "const generation = ++state.portsRequestGeneration;" in SCRIPT
    assert "if (generation !== state.portsRequestGeneration) return;" in SCRIPT


def test_history_refresh_is_bound_to_the_console_session_generation():
    assert "const portKey = session.portKey;" in SCRIPT
    assert "const generation = ++session.historyGeneration;" in SCRIPT
    assert (
        "if (generation !== session.historyGeneration || session.closed) return;"
    ) in SCRIPT
    assert "session.historyGeneration += 1;" in SCRIPT


def test_websocket_disconnect_revokes_write_controls():
    assert "function setWritable(session, writable)" in SCRIPT
    assert "socket.onerror = () =>" in SCRIPT
    assert "socket.onclose = event =>" in SCRIPT
    assert SCRIPT.count("setWritable(session, false);") >= 5
    assert (
        "if (message.type === 'backlog') "
        "setWritable(session, Boolean(message.writable));"
    ) in SCRIPT


def test_failed_send_preserves_user_input():
    assert "return false;" in SCRIPT
    assert "return true;" in SCRIPT
    assert "if (sendInput(session, input.value, true)) input.value = '';" in SCRIPT


def test_background_refresh_tracks_embedded_visibility():
    assert "function syncPortAutoRefresh(visible)" in SCRIPT
    assert "window.addEventListener('gms:embedded-visibility'" in SCRIPT
    assert "syncPortAutoRefresh(event.detail?.visible !== false);" in SCRIPT
    assert "syncPortAutoRefresh(false);" in SCRIPT


def test_console_tabs_support_multiple_concurrent_sessions():
    assert "sessions: []," in SCRIPT
    assert "function sessionByKey(portKey)" in SCRIPT
    assert "function renderConsoleTabs()" in SCRIPT
    assert "function buildConsolePane(session, port)" in SCRIPT
    assert "function closeConsole(portKey)" in SCRIPT
    # 每个会话独享终端与 WebSocket，切走后仍继续采集输出。
    assert "session.terminal.write(text);" in SCRIPT
    assert "session.socket = socket;" in SCRIPT
    # 关闭某个 tab 只销毁对应会话，不影响其它控制台。
    assert "state.sessions.filter(item => item !== session);" in SCRIPT
    # 关闭会话后必须使在途响应失效，避免写入已销毁面板。
    assert "session.closed = true;" in SCRIPT


def test_read_only_mode_hides_write_actions_without_inventory_permission():
    # 普通 user 角色没有 devices.inventory：capture/start 等写端点必然 403，
    # 页面必须先读 auth/status 判定权限再决定渲染哪些写操作按钮。
    assert "/api/auth/status" in SCRIPT
    assert "function computeManagePermission(status)" in SCRIPT
    assert "canManageDevices" in SCRIPT
    assert "permissions.includes('devices.inventory')" in SCRIPT
    # 403 "Permission denied" 需要映射为可操作的中文提示。
    assert "权限不足" in SCRIPT
    assert "/permission denied/i.test(message)" in SCRIPT
