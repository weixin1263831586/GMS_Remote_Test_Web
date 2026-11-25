"""Current configuration storage, backup and recovery contracts."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from foundation.config import ConfigManager
from foundation.private_config import read_json_object, write_private_json
from foundation.runtime_config_store import RuntimeConfigStore
from scripts.backup.gms_backup import _stage_sources


def test_cached_config_returns_independent_nested_snapshots(tmp_path):
    manager = ConfigManager(project_root=tmp_path)
    manager.update_runtime_config({'nested': {'list': [1], 'password': 'fixture'}})
    first = manager.load_config()
    first['nested']['list'].append(2)
    first['nested']['password'] = 'changed'
    assert manager.load_config()['nested'] == {'list': [1], 'password': 'fixture'}


def test_corrupt_partition_preserves_other_reads_but_blocks_writes(tmp_path):
    manager = ConfigManager(project_root=tmp_path)
    manager.update_runtime_config({'sidebar_order': ['devices'], 'ubuntu_host': '192.0.2.3'})
    store = manager._runtime_store
    store.paths['credentials'].write_text('{invalid', encoding='utf-8')
    config = manager.load_config(force_reload=True)
    assert config['sidebar_order'] == ['devices']
    assert config['ubuntu_host'] == '192.0.2.3'
    assert manager.update_runtime_config({'sidebar_order': ['test']}) is False
    assert store.paths['credentials'].read_text() == '{invalid'


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
    manager.runtime_config_path = str(tmp_path / "configs/secrets/redmine/by_user/fixture/config_runtime.json")
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
    write_private_json(root / "configs/local/config.json", {"ubuntu_user": "operator"})
    write_private_json(root / "configs/secrets/environment.json", {"GMS_ENV": "development"})
    RuntimeConfigStore(root).write({"sidebar_order": ["devices"]})
    certs = root / "configs/secrets/certs"
    certs.mkdir(parents=True)
    (certs / "fixture.key").write_text("fixture key", encoding="utf-8")
    stage = tmp_path / "stage"
    _stage_sources(root, None, stage)
    payload = stage / "payload/project"
    assert (payload / "configs/local/config.json").is_file()
    assert (payload / "configs/secrets/environment.json").is_file()
    assert (payload / "configs/secrets/certs/fixture.key").read_text() == "fixture key"
    assert (payload / "data/settings/preferences.json").is_file()


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


def test_shared_atomic_writer_preserves_json_formats_and_previous_file(tmp_path, monkeypatch):
    import json
    import stat

    import pytest

    from foundation import private_config

    value = {"name": "中文", "items": [1, 2]}
    target = tmp_path / "prefs.json"
    private_config.atomic_write_json(target, value)
    original = target.read_text(encoding="utf-8")
    assert original == json.dumps(value, ensure_ascii=False, indent=2)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    private = tmp_path / "private" / "config.json"
    private_config.write_private_json(private, value)
    assert private.read_text(encoding="utf-8") == json.dumps(value, ensure_ascii=False, indent=4) + "\n"
    assert stat.S_IMODE(private.stat().st_mode) == 0o600

    def fail_replace(*_args):
        raise OSError("replace failed")

    monkeypatch.setattr(private_config.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        private_config.atomic_write_json(target, {"name": "new"})
    assert target.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*.tmp"))
