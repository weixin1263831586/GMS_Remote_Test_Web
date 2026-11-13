"""Source identity is evidence metadata, not a successful-tool counter."""

from __future__ import annotations

import json
import re


def source_context(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    while isinstance(value, dict) and isinstance(value.get("data"), dict):
        value = value["data"]
    if not isinstance(value, dict):
        return {}
    # Only structural metadata is retained; never retain source/log bodies.
    keys = ("task_id", "module", "filename", "file_name", "suite_version", "suite_path",
            "commit", "revision", "source_id", "provider", "path", "query", "match_type")
    return {key: value[key] for key in keys if isinstance(value.get(key), (str, int))}


def expected_module(entry):
    explicit = entry.get("test_module") or entry.get("module")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    found = re.search(r"\b(?:Cts|Gts|Vts|Sts)[A-Za-z0-9_]*(?:TestCases|HostTestCases)\b",
                      str(entry.get("subject") or ""))
    return found.group() if found else ""


def contexts_by_task(trace):
    contexts = {}
    resolved = {}
    for call in trace.tool_calls:
        if not call.succeeded:
            continue
        context = dict(call.source_context)
        query = str(call.tool_input.get("query") or context.get("query") or "")
        if "apk_resolve" in call.tool_name and query:
            resolved[query] = context
        if "apk_analyze" in call.tool_name and query in resolved:
            context = {**resolved[query], **context}
        task = str(context.get("task_id") or call.tool_input.get("task_id") or "")
        if task:
            contexts[task] = {**contexts.get(task, {}), **context}
    return contexts


def call_source_context(call, trace):
    task = str(call.tool_input.get("task_id") or call.source_context.get("task_id") or "")
    return {**contexts_by_task(trace).get(task, {}), **call.source_context}


def module_matches(call, trace, module):
    if not module:
        return True
    context = call_source_context(call, trace)
    resolved_module = str(context.get("module") or "")
    if not resolved_module:
        filename = str(context.get("filename") or context.get("file_name") or "")
        resolved_module = re.sub(r"\.(?:apk|jar)$", "", filename, flags=re.I)
    return not resolved_module or resolved_module.casefold() == module.casefold()
