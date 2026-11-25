"""通用 AI 模型网关：provider 选择、文本生成、failover 与健康探针。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from features.kkagent.health import (
    is_local_provider as _is_local_provider,
)
from features.kkagent.health import (
    probe_provider as probe_provider_health,
)
from features.kkagent.health import (
    provider_statuses,
)
from features.kkagent.http_client import (
    API_FORMAT_ANTHROPIC,
    API_FORMAT_OPENAI,
    api_format_for,
    build_chat_request,
    parse_response_text,
    post_chat,
)
from foundation.redaction import redact_sensitive_text


class UniversalAIAnalyzer:
    """通用AI模型分析器"""

    def __init__(self, config: dict = None):
        """Initialize the shared model gateway."""
        self.config = config or {}
        self.timeout = 60
        # 兼容既有调用方（features/redmine/analysis/ai.py）的格式常量。
        self.API_FORMAT_ANTHROPIC = API_FORMAT_ANTHROPIC
        self.API_FORMAT_OPENAI = API_FORMAT_OPENAI

    def _get_api_format(self, provider_name: str, config: dict) -> str:
        """返回提供商使用的 Anthropic 或 OpenAI API 格式。"""
        return api_format_for(provider_name, config)

    def get_primary_provider(self, preferred: str | None = None) -> str | None:
        """获取主要提供商。

        Args:
            preferred: 若指定且该 provider 已启用，则优先返回它；否则按原逻辑选择。
        """
        if not self.config.get('enabled', False):
            return None

        providers = self.config.get('providers', {})

        if preferred:
            if providers.get(preferred, {}).get('enabled', False):
                return preferred

        primary = self.config.get('primary_provider')

        if primary and providers.get(primary, {}).get('enabled', False):
            return primary

        for provider_name, provider_config in providers.items():
            if provider_config.get('enabled', False):
                return provider_name

        return None

    def get_local_provider(self) -> str | None: return first_local_provider(self.config, _is_local_provider)

    def _post_chat(self, url: str, headers: dict, data: dict, api_format: str):
        return post_chat(url, headers, data, api_format, timeout=self.timeout)

    def get_provider_statuses(self) -> list[dict]:
        return provider_statuses(self.config)

    def probe_provider(self, provider_name: str) -> dict:
        return probe_provider_health(self.config, provider_name, self._generate_with_provider)

    def generate(self, user_prompt: str, system_prompt: str = '', max_tokens: int | None = None,
                 preferred_provider: str | None = None) -> dict:
        """使用首选或主 AI 提供商生成通用纯文本。"""
        try:
            provider_name = self.get_primary_provider(preferred=preferred_provider)
            if not provider_name:
                return {'success': False, 'error': '未配置可用的 AI 提供商'}
            providers = self.config.get('providers', {})
            provider_order = [provider_name] + [
                name for name, config in providers.items()
                if name != provider_name and config.get('enabled', False)
            ]
            result = call_provider_chain(
                provider_order,
                providers,
                lambda name, config: self._generate_with_provider(
                    name,
                    config,
                    user_prompt,
                    system_prompt,
                    max_tokens,
                ),
            )
            return {
                **result,
                'preferred_provider': provider_name,
            }
        except Exception as e:
            return {'success': False, 'error': f'AI调用失败: {redact_sensitive_text(e)}'}

    def _generate_with_provider(
        self,
        provider_name: str,
        provider_config: dict,
        user_prompt: str,
        system_prompt: str,
        max_tokens: int | None,
    ) -> dict:
        """Generate text with one provider; provider-chain handles fallback."""
        try:
            base_url = provider_config.get('base_url')
            model = provider_config.get('model')
            if not base_url or not model:
                return {'success': False, 'error': f'{provider_name} base_url/model 未配置'}

            messages = []
            if system_prompt:
                messages.append({'role': 'system', 'content': system_prompt})
            messages.append({'role': 'user', 'content': user_prompt})
            tokens = max_tokens or provider_config.get('max_tokens', 2000)

            url, headers, data, api_format = build_chat_request(
                provider_name,
                provider_config,
                messages=messages,
                max_tokens=tokens,
                system_prompt=system_prompt,
            )

            response = self._post_chat(url, headers, data, api_format)
            if response.status_code != 200:
                try:
                    err = response.json().get('error', {}).get('message', f'HTTP {response.status_code}')
                except Exception:
                    err = f'HTTP {response.status_code}: {response.text[:100]}'
                return {'success': False, 'error': f'{provider_name} API错误: {redact_sensitive_text(err)}', 'provider': provider_name}

            content = self._parse_response_raw(response.json(), api_format)
            return {'success': True, 'content': content.strip()}
        except Exception as e:
            return {'success': False, 'error': f'{provider_name}调用失败: {redact_sensitive_text(e)}'}

    def _parse_response_raw(self, payload: dict, api_format: str) -> str:
        """Extract raw text content from an AI model response JSON.

        Shared by UniversalAIAnalyzer internals and RedmineAgent to avoid
        duplicating Anthropic/OpenAI response parsing logic.
        """
        return parse_response_text(payload, api_format)



def first_local_provider(
    config: dict[str, Any],
    is_local: Callable[[str, dict[str, Any]], bool],
) -> str | None:
    if not config.get('enabled', False):
        return None
    return next((
        name for name, provider in config.get('providers', {}).items()
        if provider.get('enabled', False) and is_local(name, provider)
    ), None)


def call_provider_chain(
    provider_order: list[str],
    providers: dict[str, dict[str, Any]],
    invoke: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    errors = []
    attempted = []
    for index, provider_name in enumerate(provider_order):
        attempted.append(provider_name)
        provider_result = invoke(provider_name, providers.get(provider_name, {}))
        if provider_result.get('success'):
            return {
                **provider_result,
                'provider': provider_name,
                'fallback_used': index > 0,
                'provider_errors': errors,
                'attempted_providers': attempted,
            }
        errors.append(
            f"{provider_name}: {provider_result.get('error', '分析失败')}"
        )
    return {
        'success': False,
        'error': '; '.join(errors) or '分析失败',
        'provider_errors': errors,
        'attempted_providers': attempted,
    }
