"""Model-discovered catalogs retain the same route elevation and owner checks."""

from types import SimpleNamespace
from unittest.mock import Mock

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
