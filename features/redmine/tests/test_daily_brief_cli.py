"""Daily Brief scheduled-owner selection tests."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from features.redmine import daily_brief_cli


def test_scheduled_owner_selection_uses_each_mode_trigger_time():
    with TemporaryDirectory() as tmp:
        data_root = Path(tmp)
        owners_root = data_root / "redmine" / "by_user"
        for owner_id in ("midnight", "morning", "delta-disabled", "disabled"):
            (owners_root / owner_id).mkdir(parents=True)
        services = {
            "midnight": SimpleNamespace(get_config=lambda: {
                "enabled": True, "trigger_time": "00:00",
                "delta_enabled": True, "delta_trigger_time": "06:00",
            }),
            "morning": SimpleNamespace(get_config=lambda: {
                "enabled": True, "trigger_time": "08:30",
                "delta_enabled": True, "delta_trigger_time": "09:15",
            }),
            "delta-disabled": SimpleNamespace(get_config=lambda: {
                "enabled": True, "trigger_time": "00:00",
                "delta_enabled": False, "delta_trigger_time": "06:00",
            }),
            "disabled": SimpleNamespace(get_config=lambda: {
                "enabled": False, "trigger_time": "08:30",
                "delta_enabled": True, "delta_trigger_time": "06:00",
            }),
        }
        with patch("foundation.config.settings", SimpleNamespace(data_root=data_root)), patch.object(
            daily_brief_cli, "_owner_service", side_effect=lambda owner_id: services[owner_id]
        ), patch("features.redmine.daily_brief_owner_policy.is_daily_brief_owner_eligible", return_value=True):
            assert daily_brief_cli._enabled_owner_ids(trigger_time="00:00") == [
                "delta-disabled", "midnight",
            ]
            assert daily_brief_cli._enabled_owner_ids(trigger_time="08:30") == ["morning"]
            assert daily_brief_cli._enabled_owner_ids() == [
                "delta-disabled", "midnight", "morning",
            ]
            assert daily_brief_cli._enabled_owner_ids(
                trigger_time="06:00", mode="delta"
            ) == ["midnight"]
            assert daily_brief_cli._enabled_owner_ids(
                trigger_time="09:15", mode="delta"
            ) == ["morning"]
