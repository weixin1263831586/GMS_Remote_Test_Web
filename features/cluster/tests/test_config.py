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


def test_cluster_capacity_limits_load_from_product_config(monkeypatch, tmp_path):
    path = tmp_path / "cluster.json"
    path.write_text(
        json.dumps(
            {
                "artifact_max_bytes": 101,
                "firmware_max_bytes": 102,
                "transfer_max_bytes": 103,
                "log_analysis_max_bytes": 104,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GMS_CLUSTER_CONFIG", str(path))

    config = ClusterConfig.load()

    assert config.artifact_max_bytes == 101
    assert config.firmware_max_bytes == 102
    assert config.transfer_max_bytes == 103
    assert config.log_analysis_max_bytes == 104
