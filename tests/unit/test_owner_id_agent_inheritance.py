"""owner_id_from_request agent-token inheritance tests.

Agent Service Token principals must resolve owner-scoped data (Redmine
credentials, evidence stores, ...) to their token's owner account
(``owner_user_id``) instead of the synthetic ``agent:<token_id>`` id, so a
credential configured once under the enrolling account is visible to its
agents (2026-09-09 audit: agent owner never saw Redmine credentials).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from features.auth.principal import CurrentUser
from features.users.clients import owner_id_from_request


def _agent_request(owner_user_id: str | None, *, with_record: bool = True):
    state = SimpleNamespace(
        current_user=CurrentUser(
            id="agent:agt_4f8fbaad8550c451",
            username="agent:kkagent",
            role="agent",
        ),
        auth_method="agent_token",
    )
    if with_record:
        state.agent_token_record = {
            "id": "agt_4f8fbaad8550c451",
            "name": "kkagent",
            "owner_user_id": owner_user_id,
        }
    return SimpleNamespace(state=state)


def _human_request(user_id: str = "hcq@10.10.10.206"):
    return SimpleNamespace(state=SimpleNamespace(
        current_user=CurrentUser(
            id=user_id,
            username=user_id,
            role="user",
        ),
        auth_method="session",
    ))


class OwnerIdAgentInheritanceTest(unittest.TestCase):
    def test_agent_token_inherits_owner_account(self):
        request = _agent_request("gms")
        self.assertEqual(owner_id_from_request(request), "gms")

    def test_agent_token_without_owner_falls_back_to_principal_id(self):
        request = _agent_request("", with_record=True)
        self.assertEqual(
            owner_id_from_request(request),
            "agent:agt_4f8fbaad8550c451",
        )

    def test_agent_token_missing_record_falls_back_to_principal_id(self):
        request = _agent_request("gms", with_record=False)
        self.assertEqual(
            owner_id_from_request(request),
            "agent:agt_4f8fbaad8550c451",
        )

    def test_human_session_keeps_account_id(self):
        self.assertEqual(
            owner_id_from_request(_human_request()),
            "hcq@10.10.10.206",
        )


if __name__ == "__main__":
    unittest.main()
