"""Model selection, credential forwarding and private configuration contracts."""

import asyncio
import importlib.util
import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from features.kkagent import (
    ContextBudgetError,
    auth_headers,
    context_limited_config,
    list_model_options,
    model_config,
    provider_statuses,
    resolve_model_spec,
    restricted_runtime,
)
from features.kkagent import config as gateway_config


@pytest.fixture
def catalog(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GMS_ASSISTANT_KKAGENT_CONFIG", raising=False)
    monkeypatch.delenv("KKAGENT_DEFAULT_MODEL", raising=False)
    config = {
        "enabled": True, "primary_provider": "primary",
        "providers": {"primary": {
            "enabled": True, "base_url": "https://gateway.invalid",
            "model": "actual-model", "api_key_env": "GMS_TEST_MODEL_KEY",
            "max_context_size": 64000, "max_tokens": 8000,
        }},
    }
    monkeypatch.setenv("GMS_TEST_MODEL_KEY", "synthetic-model-key")
    monkeypatch.setattr(gateway_config._config_manager, "get_ai_config", lambda: config)
    return config


def test_catalog_alone_generates_private_config_and_forwards_key(catalog):
    with model_config("", {}) as (path, env):
        body = path.read_text()
        parsed = gateway_config.tomllib.loads(body)
        assert parsed["default_model"] == "actual-model"
        model = parsed["models"]["actual-model"]
        assert model["model"] == "actual-model"
        assert model["max_output_size"] == 8000
        assert parsed["providers"]["primary"]["api_key_env"] == "GMS_MODEL_API_KEY"
        assert env == {"GMS_MODEL_API_KEY": "synthetic-model-key"}
        assert "synthetic-model-key" not in body
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not path.exists()


def test_http_and_readiness_resolve_imported_env_credentials(catalog):
    provider = catalog["providers"]["primary"]
    assert auth_headers("primary", provider) == {"Authorization": "Bearer synthetic-model-key"}
    assert provider_statuses(catalog)[0]["state"] == "ready_to_probe"
    assert "synthetic-model-key" not in repr(resolve_model_spec(catalog, "actual-model"))


def test_unresolved_inline_credential_uses_the_configured_env_key(catalog):
    provider = catalog["providers"]["primary"]
    provider["api_key"] = "${MISSING_DEPLOYMENT_KEY}"
    assert auth_headers("primary", provider) == {"Authorization": "Bearer synthetic-model-key"}
    assert resolve_model_spec(catalog, "actual-model").api_key == "synthetic-model-key"


def test_unresolved_local_credential_uses_the_local_key(catalog, monkeypatch):
    provider = catalog["providers"]["primary"]
    provider.update({"api_key": "${MISSING_DEPLOYMENT_KEY}", "base_url": "http://127.0.0.1"})
    provider.pop("api_key_env")
    monkeypatch.setenv("GMS_LOCAL_AI_API_KEY", "synthetic-local-key")
    assert auth_headers("primary", provider) == {"Authorization": "Bearer synthetic-local-key"}


def test_unresolved_credentials_are_missing_instead_of_sent_as_tokens(catalog, monkeypatch):
    provider = catalog["providers"]["primary"]
    provider["api_key"] = "${MISSING_DEPLOYMENT_KEY}"
    monkeypatch.setenv("GMS_TEST_MODEL_KEY", "${ALSO_UNRESOLVED}")
    with pytest.raises(ValueError, match="API密钥未配置"):
        auth_headers("primary", provider)
    assert provider_statuses(catalog)[0]["state"] == "credential_missing"
    assert resolve_model_spec(catalog, "actual-model").api_key == ""


def test_duplicate_model_uses_primary_route_and_one_ui_option(catalog):
    primary = catalog["providers"].pop("primary")
    catalog["providers"] = {
        "other": {**primary, "base_url": "https://other.invalid"}, "primary": primary,
    }
    assert resolve_model_spec(catalog, "actual-model").provider == "primary"
    assert list_model_options(catalog)["models"] == [{
        "model": "actual-model", "display_name": "actual-model", "provider": "primary",
    }]


def test_context_overlay_keeps_host_identity_and_policy_without_host_models(catalog, tmp_path):
    source = tmp_path / ".kkagent" / "config.toml"
    source.parent.mkdir()
    original = '''default_model = "stale"
[models.stale]
model = "stale"
[providers.stale]
api_key = "stale-secret"
[sandbox]
enabled = true
[mcp_servers.gms]
command = "gms-mcp"
[loop_control]
max_steps_per_turn = 0
'''
    source.write_text(original)
    env = {"HOME": str(tmp_path)}
    with context_limited_config("actual-model", 16384, env) as path:
        body = path.read_text()
        config = gateway_config.tomllib.loads(body)
        assert config["models"]["actual-model"]["max_context_size"] == 16384
        assert config["models"]["actual-model"]["max_output_size"] == 8000
        assert config["sandbox"]["enabled"]
        assert config["mcp_servers"]["gms"]["command"] == "gms-mcp"
        assert config["loop_control"]["max_steps_per_turn"] == 0
        assert config["loop_control"]["auto_compact"]
        assert "stale" not in body
        assert env["GMS_MODEL_API_KEY"] == "synthetic-model-key"
    assert "GMS_MODEL_API_KEY" not in env
    assert source.read_text() == original


@pytest.mark.parametrize("limit", [0, 2047])
def test_context_budget_rejects_invalid_limits_in_host_fallback(catalog, tmp_path, limit):
    source = tmp_path / ".kkagent" / "config.toml"
    source.parent.mkdir()
    source.write_text('default_model = "legacy"\n[models.legacy]\nmodel = "actual"\n')
    with pytest.raises(ContextBudgetError), model_config("legacy", {}, context_limit=limit):
        pytest.fail("invalid context budget must fail closed")


def test_host_fallback_rejects_an_unknown_explicit_alias(catalog, tmp_path):
    source = tmp_path / ".kkagent" / "config.toml"
    source.parent.mkdir()
    source.write_text('default_model = "legacy"\n[models.legacy]\nmodel = "actual"\n')
    with pytest.raises(ContextBudgetError), model_config("missing", {}):
        pytest.fail("must not silently select the host default")


def test_context_overlay_honors_per_call_model_selection(catalog):
    catalog["providers"]["second"] = {
        **catalog["providers"]["primary"], "model": "selected-model",
    }
    with model_config("", {"KKAGENT_DEFAULT_MODEL": "selected-model"}) as (path, _env):
        assert gateway_config.tomllib.loads(path.read_text())["default_model"] == "selected-model"


def _host_with_role_models(tmp_path):
    source = tmp_path / ".kkagent" / "config.toml"
    source.parent.mkdir(exist_ok=True)
    source.write_text('''default_model = "legacy"
balance_model = "legacy"
fast_model = "legacy"
fallback_model = "legacy"
quality_model = "legacy"
compaction_model = "legacy"
[models.legacy]
model = "legacy"
provider = "legacy"
[providers.legacy]
type = "openai"
base_url = "https://legacy.invalid"
[goal]
judge_model = "legacy"
[sandbox]
enabled = true
''')
    return source


def test_catalog_clears_stale_role_selectors_and_preserves_host_policy(catalog, tmp_path):
    source = _host_with_role_models(tmp_path)
    original = source.read_text()
    with model_config("", {}) as (path, _env):
        config = gateway_config.tomllib.loads(path.read_text())
        assert config["default_model"] == "actual-model"
        assert config["sandbox"]["enabled"]
        assert not set(gateway_config._HOST_MODEL_ROLE_KEYS) & config.keys()
        assert "judge_model" not in config["goal"]
    assert source.read_text() == original


def test_host_fallback_preserves_its_valid_role_selectors(catalog, tmp_path):
    _host_with_role_models(tmp_path)
    with model_config("legacy", {}) as (path, _env):
        config = gateway_config.tomllib.loads(path.read_text())
        assert config["balance_model"] == "legacy"
        assert config["fast_model"] == "legacy"
        assert config["goal"]["judge_model"] == "legacy"


@pytest.mark.skipif(not shutil.which("kkagent"), reason="kkagent CLI not installed")
async def test_real_cli_accepts_catalog_overlay_and_applies_isolation_policy(catalog, tmp_path):
    source = _host_with_role_models(tmp_path)
    original = source.read_text()
    env = {
        "PATH": str(Path(shutil.which("kkagent")).parent), "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path),
    }
    async with restricted_runtime(shutil.which("kkagent"), env) as (path, _directory):
        config = gateway_config.tomllib.loads(path.read_text())
        assert config["default_model"] == "actual-model"
        assert config["default_permission_mode"] == "manual"
        assert config["mcp_servers"] == {}
        assert config["hooks"] == []
        assert config["permission"]["rules"] == [{"decision": "deny", "pattern": "*", "scope": "user"}]
        assert env["GMS_MODEL_API_KEY"] == "synthetic-model-key"
    assert not path.exists()
    assert source.read_text() == original


def test_import_keeps_remote_model_id_credentials_and_output_budget(catalog):
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "import_kkagent_models", root / "tools/scripts/kkagent/import_kkagent_models.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    converted = module.convert({
        "default_model": "friendly", "providers": {"gateway": {
            "base_url": "https://gateway.invalid", "type": "openai", "api_key_env": "REMOTE_KEY",
        }}, "models": {"friendly": {
            "provider": "gateway", "model": "remote-model-id", "max_output_size": 12000,
        }},
    })
    provider = converted["providers"][converted["primary_provider"]]
    assert provider["model"] == "remote-model-id"
    assert provider["api_key_env"] == "REMOTE_KEY"
    assert provider["max_tokens"] == 12000


def test_bootstrap_injects_source_search_into_report_analyzer(catalog, monkeypatch):
    from bootstrap.dependencies import _universal_analyzer_factory
    from features.reports import ReportAnalyzer

    search = Mock(return_value=[{"path": "src/Test.java", "url": "https://source.invalid/Test.java"}])
    monkeypatch.setattr(ReportAnalyzer, "rk_codesearch", search)
    analyzer = _universal_analyzer_factory()
    assert analyzer._fetch_source_code_android("Test")["file_path"] == "src/Test.java"
    search.assert_called_once_with("Test", max_results=3)


async def test_redmine_spawn_receives_catalog_config_and_private_key(catalog, monkeypatch):
    # 跨 feature 走公共面：KkAgentRedmineAnalyzer 由 redmine.kkagent 包
    # __init__ 导出（架构规则禁止直接摸 features.redmine.kkagent.analyzer）。
    from features.redmine import kkagent as redmine_kkagent

    KkAgentRedmineAnalyzer = redmine_kkagent.KkAgentRedmineAnalyzer

    stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
    stdout.feed_data(json.dumps({"type": "result", "session_id": "test-session"}).encode() + b"\n")
    stdout.feed_eof()
    stderr.feed_eof()
    process = Mock(stdout=stdout, stderr=stderr, returncode=0, wait=AsyncMock(return_value=0))
    paths = []

    async def spawn(*command, **options):
        path = Path(command[command.index("--config") + 1])
        paths.append(path)
        body = gateway_config.tomllib.loads(path.read_text())
        assert body["default_model"] == "actual-model"
        assert options["env"]["GMS_MODEL_API_KEY"] == "synthetic-model-key"
        assert "synthetic-model-key" not in path.read_text()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    analyzer = KkAgentRedmineAnalyzer()
    trace, _raw, timed_out = await analyzer._run_stream(analyzer.build_command("test"))
    assert trace.session_id == "test-session" and not timed_out
    assert paths and all(not path.exists() for path in paths)
