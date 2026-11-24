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


@pytest.mark.asyncio
async def test_device_list_returns_router_denial_instead_of_internal_error(monkeypatch):
    from features.assistant.execution_result import ToolResult

    executor = ActionExecutor()
    denied = ToolResult(
        success=False, tool_name="devices_list", error="没有权限",
        formatted_text="没有权限",
    )

    async def fetch(*_args, **_kwargs):
        return denied, {}

    monkeypatch.setattr(ActionExecutor, "_fetch_router_json", staticmethod(fetch))
    result = await executor._query_devices({}, object(), {})
    assert result.success is False
    assert result.error == "没有权限"
    locked = await executor._query_locked_devices({}, object(), {})
    assert locked.success is False
    assert locked.tool_name == "devices_user_locked"
    assert locked.error == "没有权限"


@pytest.mark.asyncio
async def test_wifi_device_failure_is_not_reported_as_complete(monkeypatch):
    from fastapi.responses import JSONResponse

    executor = ActionExecutor()
    monkeypatch.setattr(
        "features.assistant.executor.config_manager.get_wifi_defaults",
        lambda: {"ssid": "lab", "password": "pw"},
    )

    async def fail(_req, _request):
        return JSONResponse(content={
            "success": True,
            "results": [{"device": "SER", "success": False}],
            "summary": {"total": 1, "success": 0, "failed": 1},
        })

    monkeypatch.setattr("features.devices.connect_wifi", fail)
    result = await executor._connect_wifi({}, object(), {"devices": ["SER"]})
    assert result.success is False
    assert "连接完成" not in result.formatted_text
    assert "成功 0/1" in result.formatted_text


@pytest.mark.asyncio
async def test_health_text_does_not_invent_a_websocket_count(monkeypatch):
    from fastapi.responses import JSONResponse

    async def probe():
        return JSONResponse(content={"status": "alive", "service": "gms", "modules": {}})

    monkeypatch.setattr("features.system.health_check", probe)
    result = await ActionExecutor()._query_health({}, object(), {})
    assert result.success
    assert "WebSocket" not in result.formatted_text
    assert "alive" in result.formatted_text


def test_stale_days_follow_the_http_bounds():
    from features.redmine import bounded_stale_days

    assert bounded_stale_days(None, 7) == 7
    assert bounded_stale_days(0, 7) == 7
    assert bounded_stale_days("nope", 7) == 7
    assert bounded_stale_days(45, 7) == 30
    assert bounded_stale_days("9", 7) == 9
