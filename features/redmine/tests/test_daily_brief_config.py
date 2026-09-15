"""Daily Brief settings metadata tests."""

from pathlib import Path
from tempfile import TemporaryDirectory

from features.redmine.daily_brief_config import (
    list_daily_brief_agent_profiles,
    list_daily_brief_model_options,
)


def test_agent_profiles_list_only_local_kkagent_profile_names():
    with TemporaryDirectory() as tmp:
        profiles_root = Path(tmp)
        (profiles_root / "kkagent-local.toml").write_text(
            'profile = "kkagent-local"\nclient = "kkagent"\n', encoding="utf-8"
        )
        (profiles_root / "codex-local.toml").write_text(
            'profile = "codex-local"\nclient = "codex"\n', encoding="utf-8"
        )
        (profiles_root / "invalid name.toml").write_text(
            'client = "kkagent"\n', encoding="utf-8"
        )
        assert list_daily_brief_agent_profiles(profiles_root) == {
            "profiles": ["kkagent-local"],
            "default_profile": "kkagent-local",
        }


def test_system_model_options_are_enabled_and_secret_free():
    options = list_daily_brief_model_options({
        "enabled": True,
        "primary_provider": "local",
        "providers": {
            "remote": {
                "enabled": True, "model": "remote-model", "display_name": "Remote Model",
                "api_key": "must-not-leak", "base_url": "https://private.example",
            },
            "local": {
                "enabled": True, "model": "glm-5.3-flash", "display_name": "GLM Local",
                "api_key": "must-not-leak",
            },
            "disabled": {"enabled": False, "model": "disabled-model"},
        },
    })
    assert options == {
        "default_model": "glm-5.3-flash",
        "models": [
            {"model": "glm-5.3-flash", "display_name": "GLM Local", "provider": "local"},
            {"model": "remote-model", "display_name": "Remote Model", "provider": "remote"},
        ],
    }
