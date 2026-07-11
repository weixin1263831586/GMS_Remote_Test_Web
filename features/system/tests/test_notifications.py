from features.system.notifications import store_notification
from features.system.state import global_state


def test_store_notification_does_not_mutate_callers_payload():
    payload = {"_synced_id": "notice-1", "_synced_read": True, "run_id": "ats-1"}
    original = dict(payload)

    with global_state.notifications_lock:
        global_state.notifications.pop("notification-test", None)
    record = store_notification("notification-test", "done", data=payload)

    assert payload == original
    assert record["id"] == "notice-1"
    assert record["read"] is True
    assert record["data"] == {"run_id": "ats-1"}
