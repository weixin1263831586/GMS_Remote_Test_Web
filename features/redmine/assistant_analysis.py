"""Conversation adapters reuse the single-issue queue and its owner guards."""

from fastapi import Request

from features.users import owner_id_from_request
from foundation.error_model import ApiError

from .daily_brief_api import (
    SingleIssueAnalysisRequest,
    _require_human,
    analyze_single_issue,
    get_brief_run,
)
from .evidence import EvidenceError, owner_base_url, parse_issue_ref


async def analyze_issue_for_assistant(
    request: Request, issue_ref: str, analysis_mode: str = "incremental", analysis_hint: str = "",
):
    _require_human(request)
    if not isinstance(issue_ref, str) or not issue_ref.strip() or len(issue_ref) > 2048:
        return ApiError.malformed_request("请提供 Redmine 工单完整 URL 或数字单号。").to_response()
    try:
        issue_id = parse_issue_ref(issue_ref, owner_base_url(owner_id_from_request(request)))
        payload = SingleIssueAnalysisRequest(
            issue_id=issue_id, analysis_mode=analysis_mode, analysis_hint=analysis_hint,
        )
    except (EvidenceError, ValueError):
        return ApiError.malformed_request("工单引用或分析参数无效；URL 必须属于当前账号配置的 Redmine。").to_response()
    return await analyze_single_issue(request, payload)


async def get_analysis_for_assistant(request: Request, run_id: str):
    return await get_brief_run(request, run_id)
