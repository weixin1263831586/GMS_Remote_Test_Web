from unittest.mock import Mock

from features.devices.usbip import persistence


def test_source_changes_merge_without_replaying_other_runtime_config(monkeypatch):
    runtime_config = {
        "usbip_devices_source": {
            "old": {"source": "host-1"},
            "keep": {"source": "host-2"},
        },
        "other_config": {"enabled": True},
    }
    manager = Mock()
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
