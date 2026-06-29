from __future__ import annotations

import os
import re
import shlex
import json
from typing import Any

from . import runtime
from .suite_helpers import _get_available_test_suites
from .suites import get_default_suites_path, is_config_host_local, list_local_test_suites


DEFAULT_SUITE_TYPES = ("cts", "vts", "gts", "sts")
MODULE_EXTENSIONS = (".apk", ".jar", ".config", ".xml")


def normalize_module_query(query: str) -> str:
    """Normalize a natural-language module search query into a filesystem keyword."""
    text = str(query or "").strip()
    text = re.sub(
        r"(相关|测试项|测试模块|模块|用例|有哪些|有那些|列表|列出|查询|查看|显示|包含|include|modules?|testcases?|tests?)",
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
    script = r"""
import json, os, sys
root = sys.argv[1]
query = sys.argv[2].lower()
limit = int(sys.argv[3])
exts = ('.apk', '.jar', '.config', '.xml')
items = []
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
                print(json.dumps(items, ensure_ascii=False))
                sys.exit(0)
print(json.dumps(items, ensure_ascii=False))
"""
    cmd = "python3 -c {} {} {} {}".format(
        shlex.quote(script),
        shlex.quote(testcases_path),
        shlex.quote(query_lower),
        shlex.quote(str(per_suite_limit)),
    )
    output, _, _ = runtime.ssh_manager.execute_command(ssh, cmd, timeout=60)
    try:
        raw_items = json.loads(output or "[]")
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


def search_latest_suite_modules(
    config: dict[str, Any],
    query: str,
    suite_types: list[str] | None = None,
    per_suite_limit: int = 30,
) -> dict[str, Any]:
    """Search latest CTS/VTS/GTS/STS testcases for modules matching query."""
    normalized_query = normalize_module_query(query)
    requested_types = [
        str(item).strip().lower()
        for item in (suite_types or list(DEFAULT_SUITE_TYPES))
        if str(item).strip()
    ]
    base_path = config.get("suites_path") or get_default_suites_path(config)
    local = os.path.isdir(base_path) or is_config_host_local(config)
    suites = list_local_test_suites(base_path) if os.path.isdir(base_path) else _get_available_test_suites(config, base_path)
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
