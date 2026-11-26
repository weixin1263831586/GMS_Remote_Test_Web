"""Unified model catalog and explicit provider probes."""

import asyncio
import shutil

from fastapi import APIRouter, Query, Request

from .analyzer import UniversalAIAnalyzer
from .config import list_model_options
from .session import KKAGENT_BINARY
from foundation.config import config_manager
from foundation.redaction import redact_sensitive_data
from foundation.responses import error_response, success_response


router = APIRouter()


@router.get("/api/config/ai")
async def get_ai_config(
    request: Request,
    probe: bool = Query(False),
    provider: str = Query(''),
):
    """获取脱敏 AI 配置和可用性状态；真实模型探测必须显式请求。"""
    ai_config = config_manager.get_ai_config()

    if not ai_config:
        return error_response('AI 未配置或未启用，请在 configs/local/config.json 中配置 ai_models 段并设置 enabled: true', status_code=404)

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

    safe_config = redact_sensitive_data(ai_config)
    safe_config['status'] = {
        'local_provider': local_provider,
        'primary_provider': primary_provider,
        'probe_target': target_provider,
        'probed': bool(probe),
        'providers': redact_sensitive_data(statuses),
        'kkagent_installed': bool(shutil.which(KKAGENT_BINARY)),
    }
    safe_config['model_options'] = list_model_options(ai_config)
    return success_response(safe_config)
