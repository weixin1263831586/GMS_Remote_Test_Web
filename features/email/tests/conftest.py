"""Keep SMTP configuration and outbox state scoped to each test."""

from types import SimpleNamespace

import pytest

from features.email import store


@pytest.fixture(autouse=True)
def isolated_email_state(tmp_path, monkeypatch):
    monkeypatch.setattr("foundation.config.config_manager.load_config", lambda *args, **kwargs: {})
    monkeypatch.setattr(store, "settings", SimpleNamespace(data_root=tmp_path))
    monkeypatch.setattr(store, "_STORE", None)
    monkeypatch.delenv("GMS_EMAIL_ENABLED", raising=False)
