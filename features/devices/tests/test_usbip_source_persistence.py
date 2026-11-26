from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import MagicMock

from features.devices.usbip import persistence
from foundation.config import ConfigManager


def test_source_changes_merge_without_replaying_other_runtime_config(monkeypatch):
    runtime_config = {
        "usbip_devices_source": {
            "old": {"source": "host-1"},
            "keep": {"source": "host-2"},
        },
        "other_config": {"enabled": True},
    }
    manager = MagicMock()
    manager.get_runtime_config.side_effect = lambda: dict(runtime_config)

    def update(updates):
        runtime_config.update(updates)
        return True

    manager.update_runtime_config.side_effect = update
    monkeypatch.setattr(persistence.runtime.get_runtime(), "config_manager", manager)

    assert persistence.update_usbip_source_records(
        updates={"new": {"source": "host-3"}}, remove=["old"],
    )
    assert runtime_config == {
        "usbip_devices_source": {
            "keep": {"source": "host-2"},
            "new": {"source": "host-3"},
        },
        "other_config": {"enabled": True},
    }
    manager.update_runtime_config.assert_called_once_with({
        "usbip_devices_source": runtime_config["usbip_devices_source"],
    })
    manager.save_runtime_config.assert_not_called()
    assert persistence.update_usbip_source_records(remove=["missing"])
    assert manager.update_runtime_config.call_count == 1


def test_source_merge_reads_after_the_other_config_manager_commits(tmp_path, monkeypatch):
    first = ConfigManager(project_root=tmp_path)
    second = ConfigManager(project_root=tmp_path)
    monkeypatch.setattr(persistence.runtime.get_runtime(), "config_manager", second)
    started = Event()

    def merge():
        started.set()
        return persistence.update_usbip_source_records(updates={"second": {"source": "host-2"}})

    with ThreadPoolExecutor(max_workers=1) as pool:
        with first.runtime_config_transaction():
            future = pool.submit(merge)
            assert started.wait(5)
            assert first.update_runtime_config({"usbip_devices_source": {"first": {"source": "host-1"}}})
        assert future.result(timeout=5)

    assert first.get_runtime_config()["usbip_devices_source"] == {
        "first": {"source": "host-1"},
        "second": {"source": "host-2"},
    }
