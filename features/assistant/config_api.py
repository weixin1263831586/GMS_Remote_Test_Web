"""AI configuration route owned by the Assistant feature.

``GET /api/config/ai`` reads the shared AI model config (stored in the users
config tree) and reports provider availability. The route lives here because
provider status/probing is Assistant-domain logic; users no longer imports
Assistant for it.
"""

import asyncio

from fastapi import APIRouter, Query, Request

from features.users import hide_sensitive_info
from features.users import runtime as users_runtime
from foundation.responses import error_response, success_response

from .universal_ai import UniversalAIAnalyzer


router = APIRouter()


@router.get("/api/config/ai")
async def get_ai_config(
    request: Request,
    probe: bool = Query(False),
    provider: str = Query(''),
):
    """获取脱敏 AI 配置和可用性状态；真实模型探测必须显式请求。"""
    config_manager = users_runtime.config_manager
    ai_config = config_manager.get_ai_config()

    if not ai_config:
        return error_response('AI 未配置或未启用，请在 configs/config.json 中配置 ai_models 段并设置 enabled: true', status_code=404)

    analyzer = UniversalAIAnalyzer(ai_config)
    statuses = analyzer.get_provider_statuses()
    local_provider = analyzer.get_local_provider() or ''
    primary_provider = analyzer.get_primary_provider() or ''
    target_provider = (provider or local_provider or primary_provider).strip()

    if probe:
        known = {item['provider'] for item in statuses}
        if target_provider not in known:
            return error_response('要检测的 AI provider 不存在', status_code=404)
        probed = await asyncio.to_thread(analyzer.probe_provider, target_provider)
        statuses = [
            probed if item['provider'] == target_provider else item
            for item in statuses
        ]

    safe_config = hide_sensitive_info(ai_config.copy())
    safe_config['status'] = {
        'local_provider': local_provider,
        'primary_provider': primary_provider,
        'probe_target': target_provider,
        'probed': bool(probe),
        'providers': statuses,
    }
    return success_response(safe_config)
