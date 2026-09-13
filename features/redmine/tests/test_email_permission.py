from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from features.redmine.api import send_department_reminder_email


def test_reminder_email_requires_email_send_permission():
    principal = SimpleNamespace(has_permission=lambda _permission: False)
    with patch(
        "features.redmine.api.require_authenticated_user", return_value=principal
    ):
        response = asyncio.run(send_department_reminder_email(SimpleNamespace()))

    assert response.status_code == 403
    assert "email.send" in response.body.decode("utf-8")
