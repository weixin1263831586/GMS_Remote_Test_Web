"""Daily Brief scheduled-owner selection tests."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from features.redmine import daily_brief_cli


def test_nightly_owner_selection_uses_each_owner_trigger_time():
    with TemporaryDirectory() as tmp:
        data_root = Path(tmp)
        owners_root = data_root / "redmine" / "by_user"
        for owner_id in ("midnight", "morning", "disabled"):
            (owners_root / owner_id).mkdir(parents=True)
        services = {
            "midnight": SimpleNamespace(get_config=lambda: {"enabled": True, "trigger_time": "00:00"}),
            "morning": SimpleNamespace(get_config=lambda: {"enabled": True, "trigger_time": "08:30"}),
            "disabled": SimpleNamespace(get_config=lambda: {"enabled": False, "trigger_time": "08:30"}),
        }
        with patch("foundation.config.settings", SimpleNamespace(data_root=data_root)), patch.object(
            daily_brief_cli, "_owner_service", side_effect=lambda owner_id: services[owner_id]
        ), patch("features.redmine.daily_brief_owner_policy.is_daily_brief_owner_eligible", return_value=True):
            assert daily_brief_cli._enabled_owner_ids(trigger_time="00:00") == ["midnight"]
            assert daily_brief_cli._enabled_owner_ids(trigger_time="08:30") == ["morning"]
            assert daily_brief_cli._enabled_owner_ids() == ["midnight", "morning"]
