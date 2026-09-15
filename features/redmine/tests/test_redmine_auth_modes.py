"""Redmine password/API-key credential mode tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from features.redmine import statistics_api
from features.redmine.agent import RedmineAgent
from features.redmine.client import RedmineClient


def test_client_prefers_api_key_over_password_credentials():
    with patch("features.redmine.client.Redmine") as redmine:
        RedmineClient(
            "https://redmine.example",
            username="operator",
            password="password",
            api_key="api-key",
        )
    redmine.assert_called_once_with("https://redmine.example", key="api-key")


def test_agent_creates_client_when_only_api_key_is_configured():
    agent = RedmineAgent.__new__(RedmineAgent)
    agent.config_manager = SimpleNamespace(
        get_redmine_config=lambda: {"base_url": "https://redmine.example"},
        load_redmine_credentials=lambda: {},
        load_redmine_api_key=lambda: "api-key",
    )
    with patch("features.redmine.agent.RedmineClient") as client:
        agent._make_client()
    client.assert_called_once_with("https://redmine.example", "", "", api_key="api-key")


def test_daily_brief_credential_gate_accepts_api_key_mode():
    manager = SimpleNamespace(
        redmine_credentials_status=lambda: {
            "configured": True,
            "base_url_configured": True,
            "password_configured": False,
            "api_key_configured": True,
        }
    )
    with patch.object(statistics_api, "_config_for_request", return_value=manager):
        assert statistics_api._has_redmine_credentials(None)
