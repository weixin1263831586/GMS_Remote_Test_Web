"""Model-discovered catalogs retain the same route elevation and owner checks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from features.assistant.executor import ActionExecutor
from features.assistant.tests.test_agent_session_safety import _bare_request
from features.assistant.tools import registry
from features.firmware import shares_api


@pytest.mark.asyncio
async def test_discovered_firmware_list_preserves_elevation_dependency(monkeypatch):
    monkeypatch.setattr("features.assistant.executor.require_authenticated_user", lambda request: SimpleNamespace(role="user"))
    denied = Mock(side_effect=HTTPException(403, "elevation required"))
    monkeypatch.setattr("features.auth.access.authentication_required", lambda: True)
    monkeypatch.setattr("features.auth.access.require_elevated_admin", denied)
    read = Mock(side_effect=AssertionError("must not load firmware records before authorization"))
    monkeypatch.setattr(shares_api, "_load_records", read)
    result = await ActionExecutor().execute({}, _bare_request(), "firmware_shares", {})
    assert result.success is False and "elevation required" in result.error
    denied.assert_called_once()
    read.assert_not_called()


@pytest.mark.asyncio
async def test_elevation_failure_keeps_a_readable_message_and_machine_action(monkeypatch):
    from features.assistant.payloads import session_payload

    monkeypatch.setattr("features.assistant.executor.require_authenticated_user", lambda request: SimpleNamespace(role="user"))
    monkeypatch.setattr("features.auth.access.authentication_required", lambda: True)
    monkeypatch.setattr("features.auth.access.require_elevated_admin", Mock(side_effect=HTTPException(
        403, {"message": "Elevation required", "elevation_required": True},
    )))
    read = Mock(side_effect=AssertionError("must authorize before reading records"))
    monkeypatch.setattr(shares_api, "_load_records", read)
    result = await ActionExecutor().execute({}, _bare_request(), "firmware_shares", {})
    assert result.data == {"status_code": 403, "code": "FORBIDDEN", "elevation_required": True}
    assert "需要管理员临时授权" in session_payload(result.to_dict())["formatted_text"]
    assert "***" not in session_payload(result.to_dict())["formatted_text"]
    read.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("query, expected", [("", ["FREE", "BUSY", "GHOST"]), ("available", ["FREE"])])
async def test_device_catalog_distinguishes_idle_from_unresponsive(monkeypatch, query, expected):
    executor = ActionExecutor()
    monkeypatch.setattr(executor, "_load_device_summaries", AsyncMock(return_value=[
        {"device_id": "FREE", "locked": False, "status": "online", "protocol": "adb"},
        {"device_id": "BUSY", "locked": True, "locked_by": "gms", "status": "online", "protocol": "adb"},
        {"device_id": "GHOST", "locked": False, "status": "unresponsive", "protocol": "unresponsive"},
    ]))
    result = await executor._query_devices({}, object(), {"query": query})
    assert [item["device_id"] for item in result.data["devices"]] == expected
    assert result.entities["devices"] == ["FREE"]
    assert "空闲 1 台" in result.formatted_text
    if not query:
        assert "不可用 1 台" in result.formatted_text and "无响应 GHOST" in result.formatted_text
        assert result.data["devices"][-1]["available"] is False


@pytest.mark.asyncio
async def test_discovered_aoa_catalog_is_scoped_to_current_owner(monkeypatch):
    from features.devices import aoa_api

    monkeypatch.setattr("features.assistant.executor.require_authenticated_user", lambda request: SimpleNamespace(role="user"))
    monkeypatch.setattr(aoa_api, "require_human_principal", lambda request: SimpleNamespace(role="user"))
    monkeypatch.setattr(aoa_api, "principal_owner_id", lambda request: "alice")
    profiles = Mock(return_value=[{"id": "alice-profile", "name": "Alice AOA"}])
    monkeypatch.setattr(aoa_api, "profile_store", lambda: SimpleNamespace(list_profiles=profiles))
    result = await ActionExecutor().execute({}, _bare_request(), "aoa_profiles", {"owner_id": "bob", "all": "1"})
    assert result.success
    profiles.assert_called_once_with("alice", False)
    assert "alice-profile" in str(result.data)


def test_catalog_entries_are_readonly_and_have_public_execution_surfaces():
    for name in ("firmware_shares", "aoa_profiles"):
        tool = registry.get(name)
        assert tool.is_readonly and not tool.is_dangerous
        assert tool.executor_ref.count(".") == 1
