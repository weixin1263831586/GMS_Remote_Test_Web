"""Focused evidence-repair guard must reject broad/unauthorized MCP calls."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "mcp_repair_policy.py"
    spec = importlib.util.spec_from_file_location("mcp_repair_policy_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _token(path):
    payload = base64.urlsafe_b64encode(json.dumps({
        "path": path, "source_id": "sdk", "commit": "a" * 40,
    }).encode()).decode().rstrip("=")
    return "src1_" + payload + "_123456789abcdef0"


def test_no_policy_keeps_normal_mcp_behavior():
    policy = _module()
    with patch.dict(os.environ, {"GMS_MCP_REPAIR_POLICY": ""}):
        assert policy.current_policy() is None
    assert policy.authorize("gms_rt_run", {}, None, set(), set()) is None


def test_malformed_or_review_only_policy_denies_all_tools():
    guard = _module()
    for raw in ("not-json", json.dumps({"mode": "deny", "targets": []})):
        with patch.dict(os.environ, {"GMS_MCP_REPAIR_POLICY": raw}):
            active = guard.current_policy()
            assert not guard.tool_visible("gms_rt_sdk_read", active)
            assert guard.authorize("gms_rt_sdk_read", {}, active, set(), set())


def test_targeted_repair_enforces_exact_repo_scope_path_and_call_budget():
    guard = _module()
    target = "frameworks/base/core/java/android/app/Activity.java"
    with patch.dict(os.environ, {"GMS_MCP_REPAIR_POLICY": json.dumps({
        "mode": "targeted", "targets": [target],
    })}):
        policy = guard.current_policy()
        searches, reads = set(), set()
        assert not guard.tool_visible("gms_rt_run", policy)
        assert guard.tool_visible("gms_rt_sdk_search", policy)
        assert guard.authorize("gms_rt_run", {}, policy, searches, reads)
        assert guard.authorize("gms_rt_sdk_search", {
            "query": "class Activity", "path": target,
        }, policy, searches, reads)
        assert guard.authorize("gms_rt_sdk_search", {
            "query": "class Activity", "repo_scope": "frameworks/base",
            "path": "frameworks",
        }, policy, searches, reads)
        correct = {"query": "class Activity", "repo_scope": "frameworks/base",
                   "path": target}
        assert guard.authorize("gms_rt_sdk_search", correct, policy, searches, reads) is None
        assert guard.authorize("gms_rt_sdk_search", correct, policy, searches, reads)
        token = _token(target)
        assert guard.authorize("gms_rt_sdk_read", {"result_id": token}, policy, searches, reads) is None
        assert guard.authorize("gms_rt_sdk_read", {"result_id": token}, policy, searches, reads)
        assert guard.authorize("gms_rt_sdk_read", {"result_id": _token(
            "frameworks/base/unrelated.txt"
        )}, policy, searches, reads)


def test_policy_rejects_parent_traversal_and_unbounded_target():
    guard = _module()
    for target in ("frameworks/base/../secret.txt", "frameworks", "/etc/passwd"):
        with patch.dict(os.environ, {"GMS_MCP_REPAIR_POLICY": json.dumps({
            "mode": "targeted", "targets": [target],
        })}):
            p = guard.current_policy()
            assert p["mode"] == "deny"
