from __future__ import annotations

import json
import os
import re
import shlex
from typing import Any

from foundation.execution_spec import SUITE_MODULE_EXTENSIONS as MODULE_EXTENSIONS

from . import runtime
from .suite_helpers import get_available_test_suites
from .suites import (
    build_suite_info,
    get_default_suites_path,
    is_config_host_local,
    list_local_test_suites,
)
from .tradefed import find_tradefed_binary_local


DEFAULT_SUITE_TYPES = ("cts", "cts-v", "vts", "gts", "sts")


def normalize_module_query(query: str) -> str:
    """Normalize a natural-language module search query into a filesystem keyword."""
    text = str(query or "").strip()
    # An identifier such as CtsPerfettoTestCases is already a precise query.
    # Removing its TestCases suffix turns it into a different, broad search.
    if re.fullmatch(r"[A-Za-z0-9_.-]+", text):
        return text
    text = re.sub(
        r"(相关|测试项|测试模块|模块|用例|有哪些|有那些|列表|列出|查询|查看|显示|包含|\b(?:include|modules?|testcases?|tests?)\b)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    tokens = re.findall(r"[A-Za-z0-9_.-]+", text)
    if tokens:
        return max(tokens, key=len)
    return str(query or "").strip()


def _suite_root_from_tools_path(tools_path: str) -> str:
    tools_path = str(tools_path or "").rstrip("/")
    if tools_path.endswith("/tools"):
        return tools_path[:-len("/tools")]
    return tools_path


def _testcases_path_for_suite(suite: dict[str, Any]) -> str:
    return os.path.join(_suite_root_from_tools_path(suite.get("tools_path", "")), "testcases")


def _suite_sort_key(suite: dict[str, Any]) -> tuple:
    tools_path = str(suite.get("tools_path") or "")
    root = _suite_root_from_tools_path(tools_path)
    try:
        mtime = os.path.getmtime(root)
    except OSError:
        mtime = 0
    return (mtime, str(suite.get("version") or ""), tools_path)


def _select_latest_suites(suites: list[dict[str, Any]], suite_types: list[str]) -> list[dict[str, Any]]:
    selected = []
    for suite_type in suite_types:
        candidates = [
            suite for suite in suites
            if str(suite.get("test_type") or "").lower() == suite_type.lower()
        ]
        if candidates:
            selected.append(max(candidates, key=_suite_sort_key))
    return selected


def _module_name_from_file(file_name: str) -> str:
    lower = file_name.lower()
    for ext in MODULE_EXTENSIONS:
        if lower.endswith(ext):
            return file_name[:-len(ext)]
    return file_name


def _match_file(file_name: str, query: str) -> bool:
    if not query:
        return True
    return query.lower() in file_name.lower()


def _scan_suite_modules_local(suite: dict[str, Any], query: str, per_suite_limit: int) -> list[dict[str, Any]]:
    testcases_path = _testcases_path_for_suite(suite)
    if not os.path.isdir(testcases_path):
        return []

    matches = []
    for root, dirs, files in os.walk(testcases_path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for file_name in sorted(files, key=str.lower):
            if not file_name.lower().endswith(MODULE_EXTENSIONS):
                continue
            if not _match_file(file_name, query):
                continue
            full_path = os.path.join(root, file_name)
            matches.append({
                "suite_type": str(suite.get("test_type") or "").upper(),
                "suite_version": suite.get("version") or "",
                "suite_path": suite.get("tools_path") or "",
                "testcases_path": testcases_path,
                "module": _module_name_from_file(file_name),
                "file_name": file_name,
                "path": full_path,
                "relative_path": os.path.relpath(full_path, testcases_path),
            })
            if len(matches) >= per_suite_limit:
                return matches
    return matches


def _scan_suite_modules_remote(ssh, suite: dict[str, Any], query: str, per_suite_limit: int) -> list[dict[str, Any]]:
    testcases_path = _testcases_path_for_suite(suite)
    query_lower = query.lower()
    script = f"exts = {MODULE_EXTENSIONS!r}\n" + r"""
import json, os, sys
root = sys.argv[1]
query = sys.argv[2].lower()
limit = int(sys.argv[3])
items = []
def emit():
    sys.stdout.write(json.dumps(items, ensure_ascii=False))
if os.path.isdir(root):
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for name in sorted(files, key=str.lower):
            lower = name.lower()
            if not lower.endswith(exts):
                continue
            if query and query not in lower:
                continue
            full = os.path.join(current, name)
            module = name
            for ext in exts:
                if lower.endswith(ext):
                    module = name[:-len(ext)]
                    break
            items.append({
                'module': module,
                'file_name': name,
                'path': full,
                'relative_path': os.path.relpath(full, root),
            })
            if len(items) >= limit:
                emit()
                sys.exit(0)
emit()
"""
    cmd = f"python3 -c {shlex.quote(script)} {shlex.quote(testcases_path)} {shlex.quote(query_lower)} {shlex.quote(str(per_suite_limit))}"
    search_result = runtime.ssh_manager.execute_command(ssh, cmd, timeout=60)
    try:
        raw_items = json.loads(search_result.stdout or "[]")
    except Exception:
        raw_items = []
    return [
        {
            **item,
            "suite_type": str(suite.get("test_type") or "").upper(),
            "suite_version": suite.get("version") or "",
            "suite_path": suite.get("tools_path") or "",
            "testcases_path": testcases_path,
        }
        for item in raw_items
    ]


def _dedupe_module_results(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        key = (str(item.get("suite_type") or ""), str(item.get("module") or ""))
        existing = grouped.get(key)
        file_info = {
            "file_name": item.get("file_name") or "",
            "path": item.get("path") or "",
            "relative_path": item.get("relative_path") or "",
        }
        if not existing:
            grouped[key] = {**item, "files": [file_info]}
            continue
        existing.setdefault("files", []).append(file_info)
        if str(item.get("file_name") or "").lower().endswith(".config"):
            existing["file_name"] = item.get("file_name")
            existing["path"] = item.get("path")
            existing["relative_path"] = item.get("relative_path")
    return list(grouped.values())


def _suite_entry_for_path(base_path: str, suite_path: str) -> dict[str, Any] | None:
    """Build a suite entry for an explicit suite root (or tools) directory.

    Accepts either "<suite_root>/tools" or the directory that contains
    testcases/ directly.  Type and version are derived from the nearest
    "android-*" path segment so results stay compatible with the inventory
    payload shape used by the normal type-filtered flow.
    """
    raw = str(suite_path or "").replace("\\", "/").strip().rstrip("/")
    if not raw:
        return None
    tools_path = raw if raw.endswith("/tools") else f"{raw}/tools"
    launcher = find_tradefed_binary_local(tools_path)
    if not launcher:
        return None
    suite = build_suite_info(launcher)
    if not suite:
        return None
    base_real = os.path.realpath(os.path.expanduser(base_path)) if base_path else ""
    tools_real = os.path.realpath(os.path.expanduser(tools_path))
    if base_real:
        try:
            if os.path.commonpath([base_real, tools_real]) != base_real:
                return None
        except ValueError:
            return None
    return {
        "test_type": suite.get("test_type") or "",
        "version": suite.get("version") or "",
        "tools_path": tools_path,
        "full_path": launcher,
        "binary": os.path.basename(launcher),
    }


def search_latest_suite_modules(
    config: dict[str, Any],
    query: str,
    suite_types: list[str] | None = None,
    per_suite_limit: int = 30,
    suite_path: str = "",
) -> dict[str, Any]:
    """Search latest CTS/CTS-V/VTS/GTS/STS testcases for modules matching query.

    When ``suite_path`` is given, only that suite is scanned (bypassing the
    latest-per-type selection), which lets callers address older or coexisting
    suite copies directly.
    """
    normalized_query = normalize_module_query(query)
    requested_types = [
        str(item).strip().lower()
        for item in (suite_types or list(DEFAULT_SUITE_TYPES))
        if str(item).strip()
    ]
    base_path = config.get("suites_path") or get_default_suites_path(config)
    local = os.path.isdir(base_path) or is_config_host_local(config)
    if suite_path:
        if not local:
            return {
                "query": query,
                "normalized_query": normalized_query,
                "base_path": base_path,
                "source": "ssh",
                "suite_types": [item.upper() for item in requested_types],
                "suite_path": suite_path,
                "searched_suites": [],
                "modules": [],
                "count": 0,
                "error": "suite_path lookup is only supported on the local suites host",
            }
        explicit = _suite_entry_for_path(base_path, suite_path)
        if explicit is None:
            return {
                "query": query,
                "normalized_query": normalized_query,
                "base_path": base_path,
                "source": "local",
                "suite_types": [item.upper() for item in requested_types],
                "suite_path": suite_path,
                "searched_suites": [],
                "modules": [],
                "count": 0,
                "error": f"Suite path not found or no tradefed launcher: {suite_path}",
            }
        latest_suites = [explicit]
    else:
        suites = list_local_test_suites(base_path) if os.path.isdir(base_path) else get_available_test_suites(config, base_path)
        latest_suites = _select_latest_suites(suites, requested_types)

    results = []
    if local:
        for suite in latest_suites:
            results.extend(_scan_suite_modules_local(suite, normalized_query, per_suite_limit))
    else:
        with runtime.ssh_manager.optional_connection(config) as ssh:
            if not ssh:
                raise RuntimeError("SSH connection failed")
            for suite in latest_suites:
                results.extend(_scan_suite_modules_remote(ssh, suite, normalized_query, per_suite_limit))
    results = _dedupe_module_results(results)

    return {
        "query": query,
        "normalized_query": normalized_query,
        "base_path": base_path,
        "source": "local" if local else "ssh",
        "suite_types": [item.upper() for item in requested_types],
        "searched_suites": [
            {
                "suite_type": str(suite.get("test_type") or "").upper(),
                "suite_version": suite.get("version") or "",
                "suite_path": suite.get("tools_path") or "",
                "testcases_path": _testcases_path_for_suite(suite),
            }
            for suite in latest_suites
        ],
        "modules": results,
        "count": len(results),
    }


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
    order (CTS before CTS-V/VTS/GTS/STS) so the choice stays deterministic.
    """
    prefer_ext = "jar" if str(prefer or "").strip().lower() == "jar" else "apk"
    normalized_query = str(payload.get("normalized_query") or "").strip().lower()
    modules = payload.get("modules") or []
    exact = [item for item in modules if str(item.get("module") or "").lower() == normalized_query]
    # An exact native/config-only module must never fall through to another
    # module's APK/JAR. The caller needs the actual native artifact or source.
    if exact:
        modules = exact
    candidates: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
    for item in modules:
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
