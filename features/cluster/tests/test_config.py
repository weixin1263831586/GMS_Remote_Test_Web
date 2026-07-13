from __future__ import annotations

import json

from features.cluster.config import ClusterConfig


def test_cluster_defaults_disabled_when_config_is_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("GMS_CLUSTER_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.delenv("GMS_CLUSTER_ENABLED", raising=False)
    config = ClusterConfig.load()
    assert config.enabled is False
    assert config.remote_dispatch_enabled is False


def test_cluster_environment_can_force_single_host_fallback(monkeypatch, tmp_path):
    path = tmp_path / "cluster.json"
    path.write_text(json.dumps({"enabled": True, "remote_dispatch_enabled": True}), encoding="utf-8")
    monkeypatch.setenv("GMS_CLUSTER_CONFIG", str(path))
    monkeypatch.setenv("GMS_CLUSTER_ENABLED", "false")
    config = ClusterConfig.load()
    assert config.enabled is False
    assert config.remote_dispatch_enabled is False
