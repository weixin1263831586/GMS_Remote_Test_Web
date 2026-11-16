"""Stopping a Controller releases authenticated command long polls safely."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, Request

from features.cluster import commands_api


@pytest.mark.parametrize("stopping", [False, True])
async def test_poll_still_authenticates_and_fences_during_shutdown(monkeypatch, stopping):
    repository = Mock()
    repository.get_worker.return_value = {"id": "worker-1"}
    repository.validate_worker_session.return_value = False
    authenticate = Mock()
    monkeypatch.setattr(commands_api, "_authenticate", authenticate)
    monkeypatch.setattr(commands_api, "service", lambda: SimpleNamespace(repository=repository))
    request = Request({"type": "http", "gms.shutdown_requested": stopping})
    with pytest.raises(HTTPException) as failure:
        await commands_api.poll_commands("worker-1", request, "Bearer fixture", "session", 2)
    assert failure.value.status_code == 409
    authenticate.assert_called_once_with("worker-1", "Bearer fixture")
    repository.poll_commands.assert_not_called()


async def test_shutdown_releases_poll_without_claiming_next_command(monkeypatch):
    request = Request({"type": "http"})
    repository = Mock()
    repository.get_worker.return_value = {"id": "worker-1"}
    repository.validate_worker_session.return_value = True
    repository.poll_commands.side_effect = [[], [{"id": "must-stay-queued"}]]
    monkeypatch.setattr(commands_api, "_authenticate", Mock())
    monkeypatch.setattr(commands_api, "service", lambda: SimpleNamespace(repository=repository))

    async def begin_shutdown(_seconds):
        request.scope["gms.shutdown_requested"] = True

    monkeypatch.setattr(commands_api.asyncio, "sleep", begin_shutdown)
    result = await commands_api.poll_commands("worker-1", request, "Bearer fixture", "session", 2)
    assert result == {"success": True, "commands": []}
    repository.poll_commands.assert_called_once_with("worker-1")
