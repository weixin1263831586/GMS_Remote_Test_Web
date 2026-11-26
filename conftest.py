"""Repository-wide test isolation and cluster port registration."""

import os

import pytest


# Isolate deployment settings before importing application modules.
os.environ['GMS_SKIP_RUNTIME_ENV'] = '1'
os.environ['GMS_AUTH_REQUIRED'] = 'false'
os.environ['GMS_ASSISTANT_KKAGENT_ENABLED'] = '0'


def _register_test_ports() -> None:
    from features.cluster import register_cluster_port

    register_cluster_port()


_register_test_ports()


@pytest.fixture(autouse=True)
def _isolate_agent_session_anchor_store(tmp_path, monkeypatch):
    """Keep Agent session data outside the runtime directory."""
    from features.assistant import session_store

    store_path = tmp_path / 'agent_sessions.json'
    monkeypatch.setattr(session_store, 'default_sessions_path', lambda: store_path)
    monkeypatch.setattr(session_store, 'default_transcripts_dir', lambda: tmp_path / 'agent_transcripts')
    yield store_path
