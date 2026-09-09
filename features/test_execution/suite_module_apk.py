"""Resolve a suite test module keyword to its APK/JAR artifact for decompilation.

Feeds the CLI `gms-rt-apk-resolve` / `gms-rt-apk-analyze` flow: search the
latest CTS/VTS/GTS/STS suites for module artifacts and return the suite path
plus the relative artifact path consumed by
`POST /api/test/suites/apk/analyze`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query

from foundation.errors import handle_api_errors

from . import runtime
from .api_support import ApiResponse
from .suite_modules import search_latest_suite_modules


router = APIRouter()

_APK_ARTIFACT_EXTENSIONS = (".apk", ".jar")


def _module_artifact_files(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the APK/JAR entries of a suite-module search result."""
    return [
        file_info
        for file_info in item.get("files") or []
        if str(file_info.get("file_name") or "").lower().endswith(_APK_ARTIFACT_EXTENSIONS)
    ]


def _pick_suite_module_artifact(payload: dict[str, Any], prefer: str) -> dict[str, Any] | None:
    """Auto-pick the best APK/JAR artifact from a module search result.

    Ranking: exact module-name match beats substring match, a longer module
    name beats a shorter one (CtsCameraTestCases over CtsCamera), and the
    requested artifact extension wins within one module. Ties keep the scan
    order (CTS before VTS/GTS/STS) so the choice stays deterministic.
    """
    prefer_ext = "jar" if str(prefer or "").strip().lower() == "jar" else "apk"
    normalized_query = str(payload.get("normalized_query") or "").strip().lower()
    candidates: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
    for item in payload.get("modules") or []:
        files = _module_artifact_files(item)
        if not files:
            continue
        chosen = min(
            files,
            key=lambda f: (
                0 if str(f.get("file_name") or "").lower().endswith(f".{prefer_ext}") else 1,
                len(str(f.get("file_name") or "")),
            ),
        )
        module_name = str(item.get("module") or "")
        name_lower = module_name.lower()
        if normalized_query and name_lower == normalized_query:
            match_score = 2
        elif normalized_query and normalized_query in name_lower:
            match_score = 1
        else:
            match_score = 0
        candidates.append((match_score, len(module_name), item, chosen))
    if not candidates:
        return None
    _, _, item, chosen = max(candidates, key=lambda entry: (entry[0], entry[1]))

    def candidate_payload(entry: tuple[int, int, dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
        _, _, cand_item, cand_file = entry
        return {
            "module": str(cand_item.get("module") or ""),
            "suite_type": cand_item.get("suite_type"),
            "suite_version": cand_item.get("suite_version"),
            "file_name": cand_file.get("file_name"),
            "analyze_suite_path": cand_item.get("suite_path"),
            "analyze_path": f"testcases/{cand_file.get('relative_path')}",
        }

    return {
        "query": payload.get("query"),
        "prefer": prefer_ext,
        "module": str(item.get("module") or ""),
        "suite_type": item.get("suite_type"),
        "suite_version": item.get("suite_version"),
        "suite_path": item.get("suite_path"),
        "file_name": chosen.get("file_name"),
        # POST /api/test/suites/apk/analyze expects a path relative to the
        # suite root (the directory that contains testcases/ and tools/).
        "analyze_suite_path": item.get("suite_path"),
        "analyze_path": f"testcases/{chosen.get('relative_path')}",
        "candidates": [
            candidate_payload(entry)
            for entry in sorted(candidates, key=lambda e: (e[0], e[1]), reverse=True)[:8]
        ],
        "searched_suites": payload.get("searched_suites") or [],
    }


@router.get("/api/test/suites/modules/apk")
@handle_api_errors
async def resolve_suite_module_apk_artifact(
    query: str = Query(..., description="模块关键词，例如 CtsCamera"),
    suite_types: str = Query("cts,vts,gts,sts", description="逗号分隔套件类型，例如 cts,vts,gts,sts"),
    prefer: str = Query("apk", description="优先产物类型：apk 或 jar"),
):
    """Resolve a test module keyword to its APK/JAR artifact for decompilation."""
    config = runtime.config_manager.load_config()
    types = [item.strip() for item in suite_types.split(",") if item.strip()]
    payload = await asyncio.to_thread(search_latest_suite_modules, config, query, types, 30)
    resolved = _pick_suite_module_artifact(payload, prefer)
    if not resolved:
        return ApiResponse.error(
            f"No APK/JAR artifact found for module query: {query}",
            status_code=404,
            data={
                "query": query,
                "modules": (payload.get("modules") or [])[:10],
                "searched_suites": payload.get("searched_suites") or [],
            },
        )
    return ApiResponse.success(resolved)
