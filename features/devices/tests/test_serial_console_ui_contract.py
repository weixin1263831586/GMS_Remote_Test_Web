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


def test_history_refresh_is_bound_to_the_selected_port_generation():
    assert "const portKey = state.selectedKey;" in SCRIPT
    assert "const generation = ++state.historyRequestGeneration;" in SCRIPT
    assert (
        "if (generation !== state.historyRequestGeneration || "
        "state.selectedKey !== portKey) return;"
    ) in SCRIPT
    assert "state.historyRequestGeneration += 1;" in SCRIPT


def test_websocket_disconnect_revokes_write_controls():
    assert "function setWritable(writable)" in SCRIPT
    assert "socket.onerror = () =>" in SCRIPT
    assert "socket.onclose = event =>" in SCRIPT
    assert SCRIPT.count("setWritable(false);") >= 5
    assert "if (message.type === 'backlog') setWritable(Boolean(message.writable));" in SCRIPT


def test_failed_send_preserves_user_input():
    assert "return false;" in SCRIPT
    assert "return true;" in SCRIPT
    assert "if (sendInput(input.value, true)) input.value = '';" in SCRIPT


def test_background_refresh_tracks_embedded_visibility():
    assert "function syncPortAutoRefresh(visible)" in SCRIPT
    assert "window.addEventListener('gms:embedded-visibility'" in SCRIPT
    assert "syncPortAutoRefresh(event.detail?.visible !== false);" in SCRIPT
    assert "syncPortAutoRefresh(false);" in SCRIPT
