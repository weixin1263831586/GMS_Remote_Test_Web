"""Legacy routing tests are deterministic; model-loop tests opt in with a fake bridge."""

import pytest


@pytest.fixture(autouse=True)
def isolate_planning_model(monkeypatch):
    monkeypatch.setenv("GMS_ASSISTANT_KKAGENT_ENABLED", "0")
