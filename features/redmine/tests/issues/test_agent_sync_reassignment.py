from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from features.redmine.agent import RedmineAgent
from features.redmine.repository import RedmineAgentDB


@pytest.mark.asyncio
async def test_complete_sync_refreshes_local_open_issue_reassigned_away():
    """A complete owner sync must reconcile rows absent after reassignment."""

    class Client:
        closed = False

        async def get_current_user(self):
            return SimpleNamespace(firstname="黄", lastname="超群")

        async def fetch_all_assigned_issues(self, status_id, limit):
            assert status_id == "*"
            return []

        async def close(self):
            self.closed = True

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        repository = RedmineAgentDB(
            db_path=root / "redmine.sqlite3", docs_dir=root / "docs"
        )
        repository.upsert_issue({
            "issue_id": 651998,
            "subject": "GTS fail",
            "status_name": "Confirmed",
            "priority_name": "Normal",
            "assigned_to_name": "黄 超群",
            "updated_on": "2026-09-17 13:05:56",
            "journals_json": [],
            "attachments_json": [],
            "is_resolved": 0,
        })
        client = Client()
        agent = RedmineAgent(
            db=repository,
            attachments_dir=root / "attachments",
        )
        agent._make_client = lambda: client
        agent.fetch_issue_snapshot = AsyncMock(return_value={
            "issue_id": 651998,
            "subject": "GTS fail",
            "status_name": "Confirmed",
            "priority_name": "Normal",
            "assigned_to_name": "李 煌",
            "updated_on": "2026-09-30 08:00:00",
            "journals_json": [],
            "attachments_json": [],
        })

        with patch("features.redmine.agent._load_agent_config", return_value={
            "sync_max_issues": 5000,
            "detail_sync_limit": 100,
        }):
            result = await agent.sync_all_assigned_issues(analyze_new=False)

        assert result["status"] == "done"
        assert result["reassigned_refreshed"] == 1
        assert repository.get_issue(651998)["assigned_to_name"] == "李 煌"
        assert repository.list_open_issue_ids_by_assignee(["黄 超群"]) == []
        assert client.closed is True
