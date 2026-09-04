import asyncio
import shutil
import threading
from datetime import datetime

from features.system import notifications
from features.system.notifications import NotificationStore
from features.system.state import global_state
from foundation.events import EventBus


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


def test_duplicate_message_within_window_merges_to_single_record(tmp_path):
    """前后端对同一故障各写一条（标题不同、消息相同）时合并为一条。"""
    import features.system.notifications as notifications_module

    store = NotificationStore(tmp_path / "notifications.sqlite3")
    fake_now = datetime(2026, 9, 1, 14, 40, 30)
    original_datetime = notifications_module.datetime

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    notifications_module.datetime = _FrozenDatetime
    try:
        backend_record = store.upsert("alice", {
            "id": "backend-1",
            "timestamp": "2026-09-01T14:40:25",
            "title": "USB/IP Fastboot firmware burn failed",
            "message": "烧写失败原因详情",
            "level": "error",
            "category": "firmware",
            "read": False,
            "data": {"devices": ["serial-1"]},
        })
        frontend_record = store.upsert("alice", {
            "id": "frontend-1",
            "timestamp": "2026-09-01T14:40:26",
            "title": "固件烧写失败",
            "message": "烧写失败原因详情",
            "level": "error",
            "category": "firmware-burn",
            "read": False,
            "data": {},
        })
    finally:
        notifications_module.datetime = original_datetime

    records = store.list("alice", 100)["records"]
    assert len(records) == 1
    assert records[0]["id"] == backend_record["id"] == "backend-1"
    # 前端回存复用同一 ID，前端拿到的也是同一条记录。
    assert frontend_record["id"] == "backend-1"


def test_duplicate_message_outside_window_stays_separate(tmp_path):
    """超出去重窗口的相同消息是独立事件，不得合并。"""
    import features.system.notifications as notifications_module

    store = NotificationStore(tmp_path / "notifications.sqlite3")
    original_datetime = notifications_module.datetime

    class _StepDatetime(datetime):
        counter = 0

        @classmethod
        def now(cls, tz=None):
            cls.counter += 1
            # 每次调用跨 1 小时，远超去重窗口。
            return datetime(2026, 9, 1, 10 + cls.counter, 0, 0)

    notifications_module.datetime = _StepDatetime
    try:
        store.upsert("alice", {
            "id": "first",
            "timestamp": "2026-09-01T10:00:00",
            "title": "USB设备断开",
            "message": "断开：serial-1",
            "level": "warning",
            "category": "device",
            "read": False,
            "data": {},
        })
        store.upsert("alice", {
            "id": "second",
            "timestamp": "2026-09-01T12:00:00",
            "title": "USB设备断开",
            "message": "断开：serial-1",
            "level": "warning",
            "category": "device",
            "read": False,
            "data": {},
        })
    finally:
        notifications_module.datetime = original_datetime

    records = store.list("alice", 100)["records"]
    assert {record["id"] for record in records} == {"first", "second"}


def test_duplicate_check_is_owner_scoped_and_ignores_empty_messages(tmp_path):
    store = NotificationStore(tmp_path / "notifications.sqlite3")

    store.upsert("alice", {
        "id": "alice-1",
        "timestamp": "2026-09-01T10:00:00",
        "title": "事件A",
        "message": "共享消息",
        "level": "info",
        "category": "test",
        "read": False,
        "data": {},
    })
    # 空/空白消息不去重（大量通知消息为空，合并它们会丢事件）。
    store.upsert("alice", {
        "id": "alice-2",
        "timestamp": "2026-09-01T10:00:01",
        "title": "标题X",
        "message": "  ",
        "level": "info",
        "category": "test",
        "read": False,
        "data": {},
    })
    # 不同 owner 的相同消息互不影响。
    bob_record = store.upsert("bob", {
        "id": "bob-1",
        "timestamp": "2026-09-01T10:00:02",
        "title": "事件B",
        "message": "共享消息",
        "level": "info",
        "category": "test",
        "read": False,
        "data": {},
    })

    assert bob_record["id"] == "bob-1"
    alice_ids = {
        record["id"] for record in store.list("alice", 100)["records"]
    }
    assert alice_ids == {"alice-1", "alice-2"}


def test_event_bus_gives_each_listener_an_independent_payload_copy():
    bus = EventBus()
    received = []

    def mutate(_event_type, payload):
        payload["private"] = "changed"

    bus.subscribe("job.transition", mutate)
    bus.subscribe("job.transition", lambda _event_type, payload: received.append(payload))
    original = {"job_id": "job-1"}

    bus.emit("job.transition", original)

    assert received == [{"job_id": "job-1"}]
    assert original == {"job_id": "job-1"}


def test_broadcast_event_targets_owner_and_strips_routing_metadata(monkeypatch):
    sent = []

    async def capture(client_id, message):
        sent.append((client_id, message))

    monkeypatch.setattr(notifications, "safe_websocket_send", capture)
    with global_state.websocket_connections_lock:
        previous = dict(global_state.websocket_connections)
        global_state.websocket_connections.clear()
        global_state.websocket_connections.update({"alice": object(), "bob": object()})
    try:
        asyncio.run(notifications.broadcast_event(
            "job.transition",
            {"job_id": "job-1", "_target_client_id": "alice"},
        ))
    finally:
        with global_state.websocket_connections_lock:
            global_state.websocket_connections.clear()
            global_state.websocket_connections.update(previous)

    assert sent == [("alice", {
        "type": "event",
        "event": "job.transition",
        "payload": {"job_id": "job-1"},
    })]


def test_event_bus_listener_from_worker_thread_uses_bound_asgi_loop(monkeypatch):
    async def scenario():
        received = asyncio.Event()
        payloads = []

        async def capture(event_type, payload):
            payloads.append((event_type, payload))
            received.set()

        monkeypatch.setattr(notifications, "broadcast_event", capture)
        loop = notifications.bind_event_bus_loop()
        try:
            thread = threading.Thread(
                target=notifications._event_bus_listener,
                args=("worker.updated", {"worker_id": "worker-1"}),
            )
            thread.start()
            thread.join()
            await asyncio.wait_for(received.wait(), timeout=1)
            await asyncio.sleep(0)
        finally:
            notifications.unbind_event_bus_loop(loop)
        assert payloads == [("worker.updated", {"worker_id": "worker-1"})]

    asyncio.run(scenario())
