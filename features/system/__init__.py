"""System-owned public feature interfaces."""

from foundation.security_audit import security_audit_logger

from .api_docs_list import API_DOCS_LIST
from .health_api import health_check
from .mainline_issues import (
    init_db as init_mainline_issues_db,
)
from .mainline_issues import (
    query_exemption_match as query_mainline_exemption_match,
)
from .models import VNCStartRequest, VPNConnectRequest
from .network import check_local_vpn_connected
from .notifications import queue_notification
from .source_manage_api import router as sdk_manage_router
from .source_provider import (
    initialize_source_runtime,
    registry_state,
    sdk_sources_available,
)
from .ssh import ssh_manager
from .vnc import novnc_url, vnc_manager


_SDK_SOURCE_HIDDEN_KEYS = frozenset({
    "repo_root", "password", "token", "secret", "private_key",
})


def list_sdk_sources(request):
    """已登记 SDK 源码源清单，供对话 Agent 只读查询。

    与 ``GET /api/sdk/sources`` 同一份 registry 结果；展平为 ``sources``，
    便于工具层直接展示。路径与凭据不进入工具结果。
    """
    from fastapi.responses import JSONResponse

    from .source_api import list_sdk_sources as list_registered

    payload = list_registered(request)
    if isinstance(payload, JSONResponse):
        return payload
    sources = []
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and isinstance(data.get("sources"), list):
        sources = [
            {
                key: value
                for key, value in item.items()
                if key not in _SDK_SOURCE_HIDDEN_KEYS
            }
            if isinstance(item, dict) else item
            for item in data["sources"]
        ]
    return {"success": True, "sources": sources}


__all__ = [
    "API_DOCS_LIST",
    "VNCStartRequest",
    "VPNConnectRequest",
    "check_local_vpn_connected",
    "health_check",
    "init_mainline_issues_db",
    "initialize_source_runtime",
    "list_sdk_sources",
    "novnc_url",
    "query_mainline_exemption_match",
    "queue_notification",
    "registry_state",
    "sdk_manage_router",
    "sdk_sources_available",
    "security_audit_logger",
    "ssh_manager",
    "vnc_manager",
]
