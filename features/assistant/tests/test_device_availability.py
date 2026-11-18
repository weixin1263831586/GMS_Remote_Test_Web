"""Conversation Agent must not guess a device is ready from absent inventory fields."""
import pytest

from features.assistant.executor import ActionExecutor


@pytest.mark.asyncio
async def test_device_listing_requires_positive_status_and_transport(monkeypatch):
    executor = ActionExecutor()

    async def rows(_request):
        return [
            {"device_id": "incomplete", "locked": False, "stale": False},
            {"device_id": "no-transport", "status": "online", "locked": False, "stale": False},
            {"device_id": "offline", "status": "offline", "protocol": "adb",
             "locked": False, "stale": False},
            {"device_id": "ready", "status": "online", "protocol": "adb",
             "locked": False, "stale": False},
        ]

    monkeypatch.setattr(executor, "_load_device_summaries", rows)
    result = await executor._query_devices({}, None, {})
    assert result.success
    inventory = {row["device_id"]: row for row in result.data["devices"]}
    assert not inventory["incomplete"]["available"]
    assert not inventory["no-transport"]["available"]
    assert not inventory["offline"]["available"]
    assert inventory["ready"]["available"]
    assert "空闲 1 台" in result.formatted_text
    assert result.entities["devices"] == ["ready"]


@pytest.mark.asyncio
async def test_cached_management_inventory_never_becomes_available(monkeypatch):
    executor = ActionExecutor()

    async def management(*_args, **_kwargs):
        return None, {
            "success": True, "stale": True, "source": "ssh",
            "devices": [
                {"device_id": "cached", "status": "online", "protocol": "adb",
                 "locked_by": ""},
                {"device_id": "lease-only", "status": "online", "protocol": "adb",
                 "locked_by": "", "lease_id": "active-lease"},
            ],
        }

    monkeypatch.setattr(ActionExecutor, "_fetch_router_json", staticmethod(management))
    items = await executor._load_device_summaries(object())
    assert all(item["stale"] for item in items)
    assert not items[0]["locked"]
    assert items[1]["locked"]

    result = await executor._query_devices({}, object(), {})
    assert result.success
    assert not any(item["available"] for item in result.data["devices"])
    assert result.entities["devices"] == []
