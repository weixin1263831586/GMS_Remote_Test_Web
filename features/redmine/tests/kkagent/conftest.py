"""Headless tests use synthetic models and never read operator configuration."""

import pytest

from features.kkagent import config as gateway_config


@pytest.fixture(autouse=True)
def isolated_model_catalog(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GMS_ASSISTANT_KKAGENT_CONFIG", raising=False)
    monkeypatch.delenv("KKAGENT_DEFAULT_MODEL", raising=False)
    config = {
        "enabled": True, "primary_provider": "gateway",
        "providers": {"gateway": {
            "enabled": True, "base_url": "https://gateway.invalid", "model": "test",
        }},
    }
    monkeypatch.setattr(gateway_config._config_manager, "get_ai_config", lambda: config)
