"""Actual migration, preservation, private storage, and recovery contracts."""

import fcntl
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from bootstrap import env_loader
from foundation.config import ConfigManager
from foundation.private_config import read_json_object, write_private_json
from foundation.runtime_config_store import RuntimeConfigStore
from scripts.gms_backup import BackupError, _stage_sources
from scripts.migrate_config_layout import migrate, rollback


def seed(root):
    static = {
        "ubuntu_user": "operator", "ubuntu_host": "192.0.2.2",
        "ubuntu_pswd": "fixture-ssh", "sidebar_order": ["old"],
        "ai_models": {"enabled": True, "primary_provider": "glm_local", "providers": {
            "glm_local": {"api_key": "fixture-static-ai", "base_url": "http://192.0.2.1"},
        }},
    }
    runtime = {
        "ubuntu_user": "operator", "sidebar_order": ["devices"],
        "client_ssh_credentials": [{"encrypted_password": "fixture-cipher"}],
        "usbip_network_quality_history": [{"host": "192.0.2.3", "rtt": 3}],
        "redmine_dashboard": {"email": {"username": "operator", "password": "fixture-mail"}},
    }
    environment = {
        "GMS_UBUNTU_PASSWORD": "fixture-ssh", "GMS_LOCAL_AI_API_KEY": "fixture-environment-ai",
        "GMS_ENV": "development", "GMS_TEST_ROOT_PATH": "${PROJECT_ROOT}/data",
    }
    for name, payload in {
        "config.json": static, "config_runtime.json": runtime, "runtime.json": environment,
        "cluster.json": {"enabled": True, "local_worker_id": "fixture-worker"},
        "worker_tokens.json": {"worker_tokens": {"fixture-worker": "fixture-token"}},
        "user_tools_data.json": {"fixture-client": {"tools": []}},
        "redmine_user_map.json": {"departments": [{"department": "fixture"}]},
    }.items():
        write_private_json(root / "configs" / name, payload)
    return static, runtime, environment


def test_migration_preserves_effective_configuration_and_secret_conflicts(tmp_path, monkeypatch):
    seed(tmp_path)
    before = ConfigManager(project_root=tmp_path).load_config()
    result = migrate(tmp_path, apply=True)
    assert result["applied"]
    assert Path(result["backup"]).is_dir()
    assert (tmp_path / "configs/config.json").is_symlink()
    assert read_json_object(tmp_path / "configs/config.json")["ubuntu_user"] == "operator"
    monkeypatch.setattr(env_loader, "_candidate_paths", lambda: [tmp_path / "configs/local/environment.json"])
    monkeypatch.delenv("GMS_SKIP_RUNTIME_ENV", raising=False)
    with patch.dict(os.environ, {}, clear=True):
        env_loader.load_runtime_env()
        after = ConfigManager(project_root=tmp_path).load_config()
        assert os.environ["GMS_LOCAL_AI_API_KEY"] == "fixture-environment-ai"
        assert os.environ["GMS_TEST_ROOT_PATH"] == str(tmp_path / "data")
    assert before == after
    local = read_json_object(tmp_path / "configs/local/config.json")
    assert "ubuntu_user" not in local
    assert "sidebar_order" not in local
    assert local["ai_models"]["providers"]["glm_local"]["api_key"].startswith("${")
    preferences = read_json_object(tmp_path / "data/settings/preferences.json")
    assert "client_ssh_credentials" not in preferences
    assert "password" not in preferences["redmine_dashboard"]["email"]
    assert "usbip_network_quality_history" not in preferences
    assert not (tmp_path / "data/redmine/by_user").exists()
    assert (tmp_path / "data/redmine/legacy/redmine_user_map.json").is_file()
    assert migrate(tmp_path, apply=True)["already_migrated"]
    for path in tmp_path.rglob("*.json"):
        assert path.stat().st_mode & 0o777 == 0o600


def test_dry_run_does_not_create_files(tmp_path):
    seed(tmp_path)
    before = set(tmp_path.rglob("*"))
    assert not migrate(tmp_path)["applied"]
    assert set(tmp_path.rglob("*")) == before


def test_conflicting_targets_are_not_overwritten(tmp_path):
    seed(tmp_path)
    target = tmp_path / "configs/local/config.json"
    write_private_json(target, {"existing": True})
    with pytest.raises(ValueError, match="target already exists"):
        migrate(tmp_path, apply=True)
    assert read_json_object(target) == {"existing": True}
    assert (tmp_path / "configs/config.json").exists()


def test_migration_refuses_running_controller(tmp_path):
    seed(tmp_path)
    (tmp_path / "data").mkdir()
    with (tmp_path / "data/controller.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        with pytest.raises(BackupError, match="stopped"):
            migrate(tmp_path, apply=True)
    assert (tmp_path / "configs/config.json").exists()


def test_migration_failure_preserves_originals(tmp_path, monkeypatch):
    seed(tmp_path)
    before = {path.name: path.read_bytes() for path in (tmp_path / "configs").glob("*.json")}
    from scripts import migrate_config_layout as module

    real_write = module.write_private_json
    calls = 0

    def fail_once(path, payload):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("fixture disk failure")
        real_write(path, payload)

    monkeypatch.setattr(module, "write_private_json", fail_once)
    with pytest.raises(OSError):
        migrate(tmp_path, apply=True)
    assert {path.name: path.read_bytes() for path in (tmp_path / "configs").glob("*.json")} == before
    assert not (tmp_path / "configs/local/layout.json").exists()


def test_runtime_transaction_recovers_failed_multi_file_write(tmp_path, monkeypatch):
    from foundation import runtime_config_store as module

    store = RuntimeConfigStore(tmp_path)
    original = {"sidebar_order": ["old"], "ubuntu_user": "operator", "redmine_auth": {"encrypted_password": "fixture"}}
    store.write(original)
    real_write = module.write_private_json
    calls = 0

    def fail_once(path, payload):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("fixture disk failure")
        real_write(path, payload)

    monkeypatch.setattr(module, "write_private_json", fail_once)
    with pytest.raises(OSError):
        store.write({"sidebar_order": ["replacement"]})
    assert store.read() == original
    assert not store.journal.exists()


def test_runtime_read_recovers_interrupted_transaction(tmp_path):
    store = RuntimeConfigStore(tmp_path)
    store.write({"sidebar_order": ["original"]})
    previous = {name: read_json_object(path) for name, path in store.paths.items()}
    write_private_json(store.journal, previous)
    write_private_json(store.paths["preferences"], {"sidebar_order": ["partial"]})
    assert store.read() == {"sidebar_order": ["original"]}


def test_multiple_managers_merge_without_lost_updates(tmp_path):
    first = ConfigManager(project_root=tmp_path)
    second = ConfigManager(project_root=tmp_path)

    def update(index):
        manager = first if index % 2 else second
        assert manager.update_runtime_config({f"fixture_{index}": index})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(update, range(20)))
    assert first.get_runtime_config() == {f"fixture_{index}": index for index in range(20)}


def test_owner_runtime_override_does_not_load_global_credentials(tmp_path):
    RuntimeConfigStore(tmp_path).write({"redmine_auth": {"encrypted_password": "global-fixture"}})
    manager = ConfigManager(project_root=tmp_path)
    manager.runtime_config_path = str(tmp_path / "data/redmine/by_user/fixture/config_runtime.json")
    assert manager.get_runtime_config() == {}
    assert manager.save_runtime({"redmine_auth": {"encrypted_password": "owner-fixture"}})
    assert manager.get_runtime_config()["redmine_auth"]["encrypted_password"] == "owner-fixture"
    assert RuntimeConfigStore(tmp_path).read()["redmine_auth"]["encrypted_password"] == "global-fixture"


def test_secret_file_changes_invalidate_config_cache(tmp_path):
    write_private_json(tmp_path / "configs/local/config.json", {})
    manager = ConfigManager(project_root=tmp_path)
    manager.save_runtime({"redmine_auth": {"encrypted_password": "old-fixture"}})
    manager.load_config()
    write_private_json(tmp_path / "configs/secrets/runtime_credentials.json", {"redmine_auth": {"encrypted_password": "new-fixture"}})
    assert manager.load_config()["redmine_auth"]["encrypted_password"] == "new-fixture"


def test_backup_includes_partitioned_configuration_and_certificates(tmp_path):
    root = tmp_path / "project"
    seed(root)
    certs = root / "configs/certs"
    certs.mkdir()
    (certs / "fixture.key").write_text("fixture key", encoding="utf-8")
    migrate(root, apply=True)
    stage = tmp_path / "stage"
    _stage_sources(root, None, stage)
    payload = stage / "payload/project"
    assert (payload / "configs/local/config.json").is_file()
    assert (payload / "configs/secrets/environment.json").is_file()
    assert (payload / "configs/secrets/certs/fixture.key").read_text() == "fixture key"
    assert (payload / "data/settings/preferences.json").is_file()


def test_rollback_restores_originals_and_keeps_new_values(tmp_path):
    seed(tmp_path)
    originals = {path.name: path.read_bytes() for path in (tmp_path / "configs").glob("*.json")}
    result = migrate(tmp_path, apply=True)
    ConfigManager(project_root=tmp_path).save_runtime({"new_setting": True})
    recovered = rollback(tmp_path, Path(result["backup"]))
    assert recovered["rolled_back"]
    assert {path.name: path.read_bytes() for path in (tmp_path / "configs").glob("*.json")} == originals
    assert not (tmp_path / "configs/local/layout.json").exists()
    retained = Path(recovered["replaced_layout_backup"])
    assert read_json_object(retained / "data/settings/preferences.json")["new_setting"]


def test_credentials_inside_lists_are_kept_private(tmp_path):
    store = RuntimeConfigStore(tmp_path)
    payload = {"integrations": [{"name": "fixture", "headers": {"authorization": "fixture-token"}}]}
    store.write(payload)
    assert store.read() == payload
    assert "integrations" not in read_json_object(store.paths["preferences"])
    assert read_json_object(store.paths["credentials"]) == payload


def test_runtime_storage_honors_explicit_data_root(tmp_path, monkeypatch):
    data_root = tmp_path / "external-data"
    monkeypatch.setenv("GMS_DATA_ROOT", str(data_root))
    manager = ConfigManager(project_root=tmp_path / "project")
    assert manager.save_runtime({"sidebar_order": ["devices"]})
    assert (data_root / "settings/preferences.json").is_file()
