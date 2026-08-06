"""Provider selection and failover helpers shared by AI analysis callers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


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
