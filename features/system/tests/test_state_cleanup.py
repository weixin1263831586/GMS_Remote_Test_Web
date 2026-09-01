"""GlobalState.cleanup_old_user_states 的过期回收行为。"""

from __future__ import annotations

from datetime import datetime, timedelta

from features.system.state import GlobalState


def _state_with_last_seen(hours_ago: float, *, running: bool) -> dict:
    return {
        'running': running,
        'last_seen': (datetime.now() - timedelta(hours=hours_ago)).isoformat(),
    }


def test_stale_running_state_is_reclaimed_after_8_hours() -> None:
    state = GlobalState()
    state.user_states['stale-running'] = _state_with_last_seen(9, running=True)
    state.user_states['active-running'] = _state_with_last_seen(2, running=True)

    state.cleanup_old_user_states()

    assert 'stale-running' not in state.user_states
    assert 'active-running' in state.user_states


def test_idle_state_keeps_24_hour_threshold() -> None:
    state = GlobalState()
    state.user_states['idle-9h'] = _state_with_last_seen(9, running=False)
    state.user_states['idle-25h'] = _state_with_last_seen(25, running=False)

    state.cleanup_old_user_states()

    assert 'idle-9h' in state.user_states
    assert 'idle-25h' not in state.user_states


def test_unparsable_last_seen_is_removed_and_logs_cleared() -> None:
    state = GlobalState()
    state.user_states['broken'] = {'running': False, 'last_seen': 'not-a-date'}
    state.test_logs['broken'] = [{'line': 1}]

    state.cleanup_old_user_states()

    assert 'broken' not in state.user_states
    assert 'broken' not in state.test_logs
