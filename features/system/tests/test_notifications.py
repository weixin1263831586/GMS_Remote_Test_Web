import shutil

from features.system import notifications
from features.system.notifications import NotificationStore


def test_store_notification_does_not_mutate_callers_payload(tmp_path, monkeypatch):
    store = NotificationStore(tmp_path / "notifications.sqlite3")
    monkeypatch.setattr(notifications, "notification_store", store)
    payload = {"_synced_id": "notice-1", "_synced_read": True, "run_id": "ats-1"}
    original = dict(payload)

    record = notifications.store_notification(
        "notification-test", "done", data=payload
    )

    assert payload == original
    assert record["id"] == "notice-1"
    assert record["read"] is True
    assert record["data"] == {"run_id": "ats-1"}


def test_notification_history_survives_restart_and_is_owner_scoped(tmp_path):
    path = tmp_path / "notifications.sqlite3"
    first = NotificationStore(path)
    first.upsert("alice", {
        "id": "notice-1",
        "timestamp": "2026-07-17T10:00:00",
        "title": "done",
        "message": "completed",
        "level": "success",
        "category": "test",
        "read": False,
        "data": {"run_id": "ats-1"},
    })

    restarted = NotificationStore(path)

    assert restarted.list("alice", 100)["records"][0]["id"] == "notice-1"
    assert restarted.list("bob", 100)["records"] == []
    assert restarted.mark_read("bob", ["notice-1"])["updated"] == 0


def test_notification_store_recovers_after_runtime_data_directory_deletion(tmp_path):
    data_dir = tmp_path / "notifications"
    store = NotificationStore(data_dir / "notifications.sqlite3")

    shutil.rmtree(data_dir)

    assert store.list("alice", 100) == {"records": [], "unread_count": 0}
