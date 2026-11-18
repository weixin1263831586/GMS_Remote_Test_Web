"""Regression coverage for the private host-only Android soak resolver."""

import json

from tools.scripts.testing.resolve_soak_host_config import resolve, validate


def _write(root, name, payload):
    (root / name).write_text(json.dumps(payload), encoding="utf-8")


def test_uses_host_local_config_and_explicit_test_target(tmp_path):
    _write(tmp_path, "environment.json", {
        "GMS_SOAK_USERNAME": "test-admin",
        "GMS_SOAK_PASSWORD": "test-secret",
    })
    _write(tmp_path, "config.json", {
        "device_host": "host-user@192.0.2.10",
        "soak": {"busids": ["1-2"], "module": "CtsSecurityTestCases"},
    })
    _write(tmp_path, "cluster.json", {
        "controller_url": "https://controller.example.invalid:5001",
        "local_worker_id": "local-bridge",
        "enabled": False,
    })
    config, origins = resolve(tmp_path, {})
    assert validate(config) == []
    assert config["GMS_SOAK_BUSIDS"] == "1-2"
    assert config["GMS_SOAK_DEVICE_HOST"] == "host-user@192.0.2.10"
    assert config["GMS_SOAK_WORKER_ID"] == "local-bridge"
    assert origins["GMS_SOAK_PASSWORD"] == "local/environment.json"


def test_dedicated_local_soak_file_selects_explicit_target(tmp_path):
    _write(tmp_path, "soak.json", {
        "device_host": "source@192.0.2.33",
        "worker_id": "worker-a",
        "busids": ["1-4"],
    })
    _write(tmp_path, "config.json", {
        "soak": {"device_host": "stale@192.0.2.8", "busids": ["1-2"]},
    })
    config, _ = resolve(tmp_path, {})
    assert config["GMS_SOAK_DEVICE_HOST"] == "source@192.0.2.33"
    assert config["GMS_SOAK_WORKER_ID"] == "worker-a"
    assert config["GMS_SOAK_BUSIDS"] == "1-4"


def test_explicit_env_wins_and_requires_exact_busid(tmp_path):
    _write(tmp_path, "environment.json", {
        "GMS_SOAK_BUSIDS": "1-2",
        "GMS_SOAK_SESSION_TOKEN": "local-token",
    })
    _write(tmp_path, "config.json", {"soak": {"busids": ["2-3"]}})
    config, origins = resolve(tmp_path, {"GMS_SOAK_BUSIDS": "9-1"})
    assert config["GMS_SOAK_BUSIDS"] == "9-1"
    assert origins["GMS_SOAK_BUSIDS"] == "Actions"
    assert any("missing GMS_SOAK_BASE_URL" in x for x in validate(config))


def test_multihost_cluster_does_not_guess_a_worker(tmp_path):
    _write(tmp_path, "cluster.json", {
        "enabled": True,
        "local_worker_id": "not-an-automatic-target",
    })
    config, _ = resolve(tmp_path, {})
    assert "GMS_SOAK_WORKER_ID" not in config


def test_refuses_multiple_busids_and_missing_admin(tmp_path):
    config = {
        "GMS_SOAK_BASE_URL": "http://127.0.0.1:5001",
        "GMS_SOAK_DEVICE_HOST": "lab-user@192.0.2.21",
        "GMS_SOAK_WORKER_ID": "w1",
        "GMS_SOAK_BUSIDS": "1-2,1-3",
    }
    problems = validate(config)
    assert any("exactly one" in item for item in problems)
    assert any("SESSION_TOKEN" in item for item in problems)
