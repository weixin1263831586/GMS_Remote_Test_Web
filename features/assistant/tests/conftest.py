"""Legacy routing tests are deterministic; model-loop tests opt in with a fake bridge."""

import pytest


@pytest.fixture(autouse=True)
def isolate_planning_model(monkeypatch):
    # Reset per test even if a preceding test explicitly enables the model.
    monkeypatch.setenv("GMS_ASSISTANT_KKAGENT_ENABLED", "0")
