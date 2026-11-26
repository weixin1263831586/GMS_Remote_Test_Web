"""Model selection, credential forwarding and private configuration contracts."""

import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest

from features.kkagent import (
    ContextBudgetError,
    auth_headers,
    classify_failure,
    context_limited_config,
    list_model_options,
    model_config,
    provider_statuses,
    resolve_model_spec,
    restricted_runtime,
)
from features.kkagent import config as gateway_config
from features.kkagent import http_client as gateway_http
from features.kkagent.http_client import build_chat_request, endpoint_url


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
def test_context_budget_rejects_invalid_limits_in_catalog(catalog, tmp_path, limit):
    source = tmp_path / ".kkagent" / "config.toml"
    source.parent.mkdir()
    source.write_text('default_model = "legacy"\n[models.legacy]\nmodel = "actual"\n')
    with pytest.raises(ContextBudgetError), model_config("actual-model", {}, context_limit=limit):
        pytest.fail("invalid context budget must fail closed")


def test_catalog_rejects_an_unknown_explicit_alias(catalog, tmp_path):
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


def test_catalog_rejects_model_only_present_in_host_config(catalog, tmp_path):
    _host_with_role_models(tmp_path)
    with pytest.raises(ContextBudgetError, match="统一模型目录"), model_config("legacy", {}):
        pytest.fail("host-only model must not be selected")


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


def test_bootstrap_injects_source_search_into_report_analyzer(catalog, monkeypatch):
    from bootstrap import dependencies
    from bootstrap.dependencies import _report_failure_analyzer_factory

    search = Mock(return_value=[{"path": "src/Test.java", "url": "https://source.invalid/Test.java"}])
    monkeypatch.setattr(dependencies, "search_indexed_source", search)
    analyzer = _report_failure_analyzer_factory()
    assert analyzer._fetch_source_code_android("Test")["file_path"] == "src/Test.java"
    search.assert_called_once_with("Test", max_results=3)


# --------------------------------------------------------------- HTTP 传输层
# 跨提供商请求结构的契约校验——端点拼接（根地址、
# /v1 地址、完整端点、GLM /v4）、system 消息落位、思考参数与重试。


@pytest.mark.parametrize("base,fmt,expected", [
    ("https://api.anthropic.com", gateway_http.API_FORMAT_ANTHROPIC,
     "https://api.anthropic.com/v1/messages"),
    ("https://api.anthropic.com/v1", gateway_http.API_FORMAT_ANTHROPIC,
     "https://api.anthropic.com/v1/messages"),
    ("https://api.anthropic.com/v1/messages", gateway_http.API_FORMAT_ANTHROPIC,
     "https://api.anthropic.com/v1/messages"),
    ("https://api.openai.com", gateway_http.API_FORMAT_OPENAI,
     "https://api.openai.com/v1/chat/completions"),
    ("https://api.openai.com/v1", gateway_http.API_FORMAT_OPENAI,
     "https://api.openai.com/v1/chat/completions"),
    ("https://api.openai.com/v1/chat/completions", gateway_http.API_FORMAT_OPENAI,
     "https://api.openai.com/v1/chat/completions"),
    ("https://open.bigmodel.cn/api/paas/v4", gateway_http.API_FORMAT_OPENAI,
     "https://open.bigmodel.cn/api/paas/v4/chat/completions"),
])
def test_endpoint_urls_never_duplicate_the_version_segment(base, fmt, expected):
    assert endpoint_url(base, fmt) == expected


def test_anthropic_request_moves_system_out_of_messages(catalog):
    messages = [
        {"role": "system", "content": "You are concise."},
        {"role": "user", "content": "hi"},
    ]
    _url, headers, data, api_format = build_chat_request(
        "primary",
        {**catalog["providers"]["primary"], "base_url": "https://api.anthropic.com"},
        messages=messages, max_tokens=128, system_prompt="You are concise.",
    )
    assert api_format == gateway_http.API_FORMAT_ANTHROPIC
    assert headers["anthropic-version"] == "2023-06-01"
    assert data["system"] == "You are concise."
    assert data["messages"] == [{"role": "user", "content": "hi"}]


def test_openai_request_keeps_system_role_and_thinks_off(catalog):
    messages = [
        {"role": "system", "content": "You are concise."},
        {"role": "user", "content": "hi"},
    ]
    url, _headers, data, api_format = build_chat_request(
        "primary", catalog["providers"]["primary"],
        messages=messages, max_tokens=128, system_prompt="You are concise.",
    )
    assert api_format == gateway_http.API_FORMAT_OPENAI
    assert url == "https://gateway.invalid/v1/chat/completions"
    assert data["messages"] == messages
    assert "system" not in data
    assert data["disable_thinking"] is True and data["enable_thinking"] is False


@pytest.mark.parametrize("status", [400, 404, 422])
def test_thinking_rejection_retries_without_thinking_controls(catalog, status, monkeypatch):
    provider = catalog["providers"]["primary"]
    _url, headers, data, api_format = build_chat_request(
        "primary", provider, messages=[{"role": "user", "content": "hi"}],
        max_tokens=128,
    )
    rejected = Mock(status_code=status,
                    **{"json.return_value": {"error": {"message": "disable_thinking 不支持"}}})
    accepted = Mock(status_code=200,
                    **{"json.return_value": {"choices": [{"message": {"content": "ok"}}]}})
    posts = Mock(side_effect=[rejected, accepted])
    monkeypatch.setattr(gateway_http.requests, "post", posts)
    response = gateway_http.post_chat(_url, headers, data, api_format)
    assert response is accepted
    first, retry = posts.call_args_list
    assert first.kwargs["json"]["disable_thinking"] is True
    assert "disable_thinking" not in retry.kwargs["json"]
    assert retry.kwargs["json"]["reasoning_effort"] == "low"


@pytest.mark.parametrize("status", [401, 500])
def test_non_validation_errors_are_never_swallowed_by_thinking_retry(
        catalog, status, monkeypatch):
    provider = catalog["providers"]["primary"]
    _url, headers, data, api_format = build_chat_request(
        "primary", provider, messages=[{"role": "user", "content": "hi"}],
        max_tokens=128,
    )
    unauthorized = Mock(status_code=status,
                        **{"json.return_value": {"error": {"message": "thinking mode?"}}})
    posts = Mock(return_value=unauthorized)
    monkeypatch.setattr(gateway_http.requests, "post", posts)
    assert gateway_http.post_chat(_url, headers, data, api_format) is unauthorized
    assert posts.call_count == 1


@pytest.mark.parametrize(
    "stderr,max_turns,expected_type,expected_text",
    [
        ("LLM stream error: kind=timeout", 10, "llm_timeout", "分析设置"),
        ("Agent turn limit reached", 10, "max_turns", "分析设置"),
        ("Agent turn limit reached", 0, "max_turns", "分析侧未限制步数"),
    ],
)
def test_failure_classification_uses_caller_settings_context(
        stderr, max_turns, expected_type, expected_text):
    error_type, message = classify_failure(3, "", stderr, max_turns=max_turns)
    assert error_type == expected_type
    assert expected_text in message
    assert "晨报" not in message

    redmine_type, redmine_message = classify_failure(
        3, "", stderr, max_turns=max_turns, settings_context="晨报",
    )
    assert redmine_type == error_type
    assert redmine_message == message.replace("分析设置", "晨报设置").replace(
        "分析侧未限制步数", "晨报侧未限制步数",
    )
