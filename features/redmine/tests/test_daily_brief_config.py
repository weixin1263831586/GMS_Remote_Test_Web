"""Daily Brief settings metadata tests."""

from pathlib import Path
from tempfile import TemporaryDirectory

from features.redmine.daily_brief_config import (
    analyzer_env_extra,
    build_brief_analyzer,
    list_daily_brief_agent_profiles,
    list_daily_brief_model_options,
    normalize_daily_brief_config,
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


def test_device_serial_normalized_and_env_sets_toolsets():
    config = normalize_daily_brief_config({
        "agent_profile": "kk",
        "device_serial": "RK3572GMS7",
    })
    assert config["device_serial"] == "RK3572GMS7"
    env = analyzer_env_extra("kk", config["device_serial"])
    assert env["GMS_MCP_TOOLSETS"] == "evidence,device_evidence"
    # 无 serial 时保持 evidence-only
    assert "GMS_MCP_TOOLSETS" not in analyzer_env_extra("kk")
    # 非法 serial（注入字符）被拒绝
    assert normalize_daily_brief_config({"device_serial": "a; rm -rf"})["device_serial"] == ""
    assert normalize_daily_brief_config({"device_serial": ""})["device_serial"] == ""


def test_legacy_budgets_do_not_limit_analysis():
    config = normalize_daily_brief_config({"max_turns": 20})
    assert config["max_turns"] == 0
    assert config["issue_timeout_seconds"] == 0
    assert build_brief_analyzer(config).max_turns == 0
    assert build_brief_analyzer(config, extra_turns=6).max_turns == 0
    assert build_brief_analyzer(
        normalize_daily_brief_config({"max_turns": 50}), extra_turns=6
    ).max_turns == 0
    assert build_brief_analyzer({"max_turns": 20, "issue_timeout_seconds": 60}).timeout_seconds == 0
