"""AI 提供商 HTTP 传输层：端点/头/payload 构造与响应文本解析。

从 features/assistant/universal_ai.py 拆出（统一模型网关，ADR 0034）：
本模块只负责"把一次补全请求发出去、把响应还原成文本"。provider 选择、
failover、业务 prompt 与结果解析由 analyzer.py 负责。

GLM-5.x 等常开思考模型会拒绝 disable_thinking 参数并要求改用推理档位；
post_chat 对这类错误统一改用 reasoning_effort=low 重试一次。
"""
from __future__ import annotations

import requests

from features.kkagent.health import auth_headers


API_FORMAT_ANTHROPIC = 'anthropic'  # /v1/messages 端点
API_FORMAT_OPENAI = 'openai'  # /v1/chat/completions 端点

_THINKING_CONTROL_KEYS = ('disable_thinking', 'skip_reasoning', 'enable_thinking')
_ALWAYS_THINKING_MARKERS = ('不支持关闭思考', '始终思考')


def rejects_disabling_thinking(error_text: str) -> bool:
    return any(marker in (error_text or '') for marker in _ALWAYS_THINKING_MARKERS)


def api_format_for(provider_name: str, config: dict) -> str:
    """返回提供商使用的 Anthropic 或 OpenAI API 格式。"""
    api_format = config.get('api_format')
    if api_format:
        return api_format

    base_url = config.get('base_url', '')
    if 'anthropic' in base_url.lower() or base_url.endswith('/messages'):
        return API_FORMAT_ANTHROPIC

    return API_FORMAT_OPENAI


def endpoint_url(base_url: str, api_format: str) -> str:
    """按 API 格式补全补全端点；已带端点后缀的 base_url 原样使用。"""
    if api_format == API_FORMAT_ANTHROPIC:
        return f"{base_url}/v1/messages" if not base_url.endswith('/messages') else base_url
    return (
        f"{base_url}/v1/chat/completions"
        if not (base_url.endswith('/chat/completions') or base_url.endswith('/completions'))
        else base_url
    )


def build_chat_request(
    provider_name: str,
    provider_config: dict,
    *,
    messages: list[dict],
    max_tokens: int,
    system_prompt: str = '',
) -> tuple[str, dict, dict, str]:
    """构造一次补全请求的 ``(url, headers, data, api_format)``。

    默认关闭思考参数（常开思考模型的拒绝由 post_chat 统一降级重试）。
    ``system_prompt`` 仅在 Anthropic 格式下落到顶层 ``system`` 字段——
    与原 universal_ai 行为一致，OpenAI 格式的 system 角色由调用方放进
    ``messages``。
    """
    api_format = api_format_for(provider_name, provider_config)
    base_url = provider_config['base_url']
    model = provider_config['model']

    if api_format == API_FORMAT_ANTHROPIC:
        url = endpoint_url(base_url, api_format)
        headers = {
            **auth_headers(provider_name, provider_config, 'x-api-key'),
            'Content-Type': 'application/json',
            'anthropic-version': '2023-06-01',
        }
        data = {
            'model': model,
            'max_tokens': max_tokens,
            'messages': messages,
            'disable_thinking': True,
            'skip_reasoning': True,
        }
        if system_prompt:
            data['system'] = system_prompt
        # 与原实现一致：None 值不进入 payload。
        data = {key: value for key, value in data.items() if value is not None}
    else:
        url = endpoint_url(base_url, api_format)
        headers = {
            **auth_headers(provider_name, provider_config),
            'Content-Type': 'application/json',
        }
        data = {
            'model': model,
            'messages': messages,
            'temperature': provider_config.get('temperature', 0.3),
            'max_tokens': max_tokens,
            'disable_thinking': True,
            'skip_reasoning': True,
            'enable_thinking': False,
        }
    return url, headers, data, api_format


def post_chat(url: str, headers: dict, data: dict, api_format: str, timeout: int = 60):
    """POST 补全请求；常开思考模型拒绝关闭思考时降级 reasoning_effort 重试一次。"""
    response = requests.post(url, headers={'Connection': 'keep-alive', **headers},
                             json=data, timeout=timeout)
    if response.status_code != 200:
        try:
            message = response.json().get('error', {}).get('message', '')
        except Exception:
            message = response.text[:300]
        if rejects_disabling_thinking(message):
            retry_data = {k: v for k, v in data.items() if k not in _THINKING_CONTROL_KEYS}
            if api_format != API_FORMAT_ANTHROPIC:
                retry_data['reasoning_effort'] = 'low'
            response = requests.post(url, headers={'Connection': 'keep-alive', **headers},
                                     json=retry_data, timeout=timeout)
    return response


def parse_response_text(payload: dict, api_format: str) -> str:
    """Extract raw text content from an AI model response JSON.

    Shared by UniversalAIAnalyzer internals and RedmineAgent to avoid
    duplicating Anthropic/OpenAI response parsing logic.
    """
    if api_format == API_FORMAT_ANTHROPIC and 'content' in payload:
        content_items = payload.get('content') or []
        first = content_items[0] if content_items else {}
        return (first or {}).get('text', '') if isinstance(first, dict) else str(first or '')

    # OpenAI / compatible format
    choices = payload.get('choices') or []
    if choices:
        choice = choices[0] or {}
        message = choice.get('message', {})
        content = message.get('content') or ''
        if not content and message.get('reasoning'):
            content = message.get('reasoning', '')
        # 兜底：GLM-5.x 等推理模型在关闭 thinking 失败时，正文可能只在 reasoning_content
        if not content and message.get('reasoning_content'):
            content = message.get('reasoning_content', '')
        if not content:
            content = choice.get('text', '')
        return content

    return str(payload)
