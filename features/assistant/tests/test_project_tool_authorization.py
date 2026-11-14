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
    for name in ("firmware_shares", "aoa_profiles", "sdk_sources"):
        tool = registry.get(name)
        assert tool.is_readonly and not tool.is_dangerous
        assert tool.executor_ref.count(".") == 1


def test_sdk_question_discovers_registered_sources_first():
    names = [item.tool.name for item in registry.find("有哪些SDK可用", top_k=5)]
    assert names and names[0] == "sdk_sources"


@pytest.mark.asyncio
async def test_sdk_sources_lists_registered_providers_without_paths(monkeypatch):
    from features.auth import CurrentUser
    from features.system import source_api

    request = _bare_request()
    request.state.current_user = CurrentUser(
        id="owner", username="owner", role="user", display_name="owner",
    )
    listed = Mock(return_value=[{
        "source_id": "7_Android17_1017",
        "provider": "ssh_git",
        "default_revision": "main",
        "repo_root_exposed": False,
        "ssh_host": "10.0.0.9",
        "ssh_port": 22,
        "ssh_username": "hcq",
        "repo_root": "/srv/aosp",
    }])
    monkeypatch.setattr(source_api, "source_registry", lambda: SimpleNamespace(list_sources=listed))
    result = await ActionExecutor().execute({}, request, "sdk_sources", {})
    assert result.success
    assert result.data["sources"][0]["source_id"] == "7_Android17_1017"
    assert "repo_root" not in result.data["sources"][0]
    assert "7_Android17_1017" in result.formatted_text
    assert "hcq@10.0.0.9:22" in result.formatted_text
    assert "/srv/aosp" not in result.formatted_text
    listed.assert_called_once()


@pytest.mark.asyncio
async def test_sdk_sources_reports_an_empty_registry(monkeypatch):
    from features.auth import CurrentUser
    from features.system import source_api

    request = _bare_request()
    request.state.current_user = CurrentUser(
        id="owner", username="owner", role="user", display_name="owner",
    )
    listed = Mock(return_value=[])
    monkeypatch.setattr(source_api, "source_registry", lambda: SimpleNamespace(list_sources=listed))
    result = await ActionExecutor().execute({}, request, "sdk_sources", {})
    assert result.success
    assert result.data["sources"] == []
    assert result.formatted_text == "尚未登记 SDK 源。"


@pytest.mark.asyncio
async def test_sdk_sources_keeps_the_scope_gate(monkeypatch):
    from features.auth import CurrentUser
    from features.system import source_api

    request = _bare_request()
    request.state.current_user = CurrentUser(
        id="agent:t1", username="agent", role="agent_service",
        extra_permissions=frozenset({"devices.read"}),
    )
    listed = Mock(side_effect=AssertionError("must not list sources before sdk.read"))
    monkeypatch.setattr(source_api, "source_registry", lambda: SimpleNamespace(list_sources=listed))
    result = await ActionExecutor().execute({}, request, "sdk_sources", {})
    assert result.success is False
    assert result.data["status_code"] == 403
    listed.assert_not_called()
