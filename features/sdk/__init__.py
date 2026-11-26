"""Android SDK registration and reproducible source search public surface."""

from . import source_api, source_manage_api
from .source_codesearch import (
    search_indexed_paths,
    search_indexed_source,
)
from .source_provider import initialize_source_runtime, registry_state, sdk_sources_available
from .source_repo_rank import configure_codesearch_hints, configure_knowledge_hints


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
    "configure_codesearch_hints",
    "configure_knowledge_hints",
    "initialize_source_runtime",
    "list_sdk_sources",
    "registry_state",
    "sdk_sources_available",
    "search_indexed_paths",
    "search_indexed_source",
    "source_api",
    "source_manage_api",
]
