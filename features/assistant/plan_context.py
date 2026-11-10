"""Plan resource selection for conversation plans (devices / suites).

从 api.py 拆出（file-size baseline）：计划生成期的设备定位与套件匹配
保持纯查询语义——不锁定资源、不替换用户指定的设备。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from features.devices import device_lock_manager, device_manager
from foundation.config import config_manager


def local_worker_id() -> str:
    from foundation.cluster_port import get_local_worker_id

    return get_local_worker_id()


def is_local_worker_id(worker_id: str | None) -> bool:
    return not worker_id or worker_id == local_worker_id()


def _select_devices(
    intent: dict[str, Any], workspace_context: dict[str, Any] | None = None
) -> tuple[list[str], list[dict[str, Any]]]:
    workspace = workspace_context or {}
    worker_id = workspace.get("worker_id") or local_worker_id()
    is_remote = workspace.get("scope_mode") == "cluster" and not is_local_worker_id(worker_id)
    workspace_devices = [str(item) for item in workspace.get("device_ids") or []]
    if is_remote:
        from features.cluster import get_cluster_service

        rows = get_cluster_service().repository.list_devices(worker_id)
        details = [{
            "device_id": item.get("id") or f"{worker_id}:{item.get('serial', '')}",
            "serial": item.get("serial", ""),
            "worker_id": worker_id,
            "state": item.get("state", "unknown"),
            "locked": item.get("state") != "available",
            "locked_by": item.get("lease_owner", ""),
        } for item in rows]
        available = {item["device_id"]: item for item in details if not item["locked"]}

        def remote_id(value: str) -> str:
            return value if value.startswith(f"{worker_id}:") else f"{worker_id}:{value}"

        explicit = [remote_id(item) for item in intent.get("devices") or []]
        preferred = [remote_id(item) for item in workspace_devices]
        requested = explicit or preferred
        if requested:
            return [item for item in requested if item in available], details
        unlocked = list(available)
        count = int(intent.get("device_count") or 1)
        return unlocked if count <= 0 else unlocked[:count], details

    device_ids = device_manager.get_connected_devices(force_refresh=True)
    details = []
    for device_id in device_ids:
        lock_status = device_lock_manager.get_lock_status(device_id)
        details.append({
            "device_id": device_id,
            "locked": bool(lock_status),
            "locked_by": lock_status.get("locked_by", "") if lock_status else "",
        })

    requested = intent.get("devices") or [
        item.split(":", 1)[1]
        if item.startswith(f"{local_worker_id()}:") else item
        for item in workspace_devices
    ]
    if requested:
        available = {item["device_id"]: item for item in details}
        return [dev for dev in requested if dev in available and not available[dev]["locked"]], details

    unlocked = [item["device_id"] for item in details if not item["locked"]]
    count = int(intent.get("device_count") or 1)
    return unlocked if count <= 0 else unlocked[:count], details


def _score_suite(suite: dict[str, Any], test_type: str, module: str) -> int:
    score = 0
    haystack = " ".join(str(suite.get(key, "")) for key in (
        "test_type", "suite_type", "version", "suite_version", "tools_path", "binary"
    )).lower()
    if test_type and test_type.lower() in haystack:
        score += 20
    if module:
        module_prefix = re.sub(r"TestCases?$", "", module, flags=re.IGNORECASE).lower()
        if module_prefix and module_prefix in haystack:
            score += 5
    if suite.get("tools_path"):
        score += 1
    return score


def _list_suites(workspace_context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    workspace = workspace_context or {}
    worker_id = workspace.get("worker_id") or local_worker_id()
    if workspace.get("scope_mode") == "cluster" and not is_local_worker_id(worker_id):
        from features.cluster import get_cluster_service

        return [
            item for item in get_cluster_service().repository.list_suites(worker_id)
            if item.get("available")
        ]
    from features.test_execution import get_available_test_suites, get_default_suites_path

    config = config_manager.load_config()
    base_path = config.get("suites_path") or get_default_suites_path(config)
    return get_available_test_suites(config, base_path)


def _select_suite(
    intent: dict[str, Any], workspace_context: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    workspace = workspace_context or {}
    suites = _list_suites(workspace)
    test_type = intent.get("test_type") or ""
    module = intent.get("test_module") or ""
    if not suites:
        return None
    selected_path = workspace.get("suite_path") or ""
    selected_key = workspace.get("suite_key") or ""
    exact = next((suite for suite in suites if selected_path and
                  (suite.get("tools_path") or suite.get("full_path")) == selected_path), None)
    if not exact:
        exact = next((suite for suite in suites if selected_key and
                      suite.get("suite_key") == selected_key), None)
    if exact:
        return exact
    return max(suites, key=lambda suite: _score_suite(suite, test_type, module))


# ==================== 计划构建与摘要（自 api.py 迁入） ====================

def build_test_plan(
    intent: dict[str, Any], selected_devices: list[str],
    device_details: list[dict[str, Any]], suite: dict[str, Any] | None,
    workspace_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from features.test_execution import detect_test_type_from_suite_path

    workspace = workspace_context or {}
    test_suite = (suite.get("tools_path") or suite.get("full_path") or "") if suite else ""
    test_type = intent.get("test_type") or (suite.get("test_type") or suite.get("suite_type") or "" if suite else "")
    if not test_type and test_suite:
        test_type = detect_test_type_from_suite_path(test_suite).upper()

    steps = [
        {"title": "刷新设备", "detail": f"发现 {len(device_details)} 台设备，选择 {len(selected_devices)} 台可用设备"},
        {"title": "匹配测试套件", "detail": test_suite or "未找到可用测试套件"},
    ]
    pre_actions = []
    if intent.get("connect_wifi"):
        _wifi = config_manager.get_wifi_defaults()
        pre_actions.append({
            "type": "connect_wifi",
            "ssid": intent.get("wifi_ssid") or _wifi["ssid"],
            "password": intent.get("wifi_password") or _wifi["password"],
        })
        steps.append({"title": "连接 WiFi", "detail": f"测试前连接到 {_wifi['ssid']}"})
    steps.append({"title": "启动测试", "detail": intent.get("test_module") or intent.get("test_case") or "按测试套件执行"})
    retry_count = int(intent.get("retry_count") or 0)
    if retry_count:
        steps.append({"title": "失败后自动 retry", "detail": f"最多 retry {retry_count} 次"})
    if intent.get("analyze_on_failure"):
        steps.append({"title": "失败后报告分析", "detail": "测试仍失败时定位最近报告并给出分析入口"})
    if intent.get("apk_source_analysis"):
        steps.append({"title": "APK/源码分析", "detail": "根据失败信息定位套件 APK/JAR 并建议反编译入口"})

    return {
        "type": "test_execution",
        "operation_id": str(uuid.uuid4()),
        "submit_state": "pending",
        "intent": intent,
        "steps": steps,
        "request": {
            "worker_id": workspace.get("worker_id") or local_worker_id(),
            "test_type": test_type,
            "test_module": intent.get("test_module", ""),
            "test_case": intent.get("test_case", ""),
            "retry_dir": "",
            "test_suite": test_suite,
            "local_server": config_manager.load_config().get("local_server", ""),
            "devices": selected_devices,
        },
        "pre_actions": pre_actions,
        "policy": {
            "retry_count": retry_count,
            "analyze_on_failure": bool(intent.get("analyze_on_failure")),
            "apk_source_analysis": bool(intent.get("apk_source_analysis")),
        },
        "device_details": device_details,
        "suite": suite or {},
        "workspace_context": workspace,
    }


def summarize_test_plan(plan: dict[str, Any]) -> str:
    req = plan.get("request", {})
    policy = plan.get("policy", {})
    lines = [
        "我已经生成执行计划，确认后开始：",
        f"- Worker: {req.get('worker_id') or local_worker_id()}",
        f"- 设备: {', '.join(req.get('devices') or []) or '未选择'}",
        f"- 测试前连接 WiFi: {'是' if plan.get('pre_actions') else '否'}",
        f"- 测试类型: {req.get('test_type') or '自动识别'}",
        f"- 测试模块: {req.get('test_module') or '未指定'}",
        f"- 测试用例: {req.get('test_case') or '未指定'}",
        f"- 测试套件: {req.get('test_suite') or '未找到'}",
        f"- 失败 retry: {policy.get('retry_count', 0)} 次",
        f"- 失败后报告分析: {'是' if policy.get('analyze_on_failure') else '否'}",
    ]
    if not req.get("devices"):
        lines.append("当前没有可用未占用设备，不能执行。")
    if not req.get("test_suite") and not req.get("retry_dir"):
        lines.append("当前没有匹配到测试套件，不能执行。")
    lines.append("输入“确认执行”或点击执行按钮后启动。")
    return "\n".join(lines)


# WorkflowDraft 执行与 plan 操作已拆至 workflow_execution.py（file-size
# baseline）；此处仅保留兼容别名供既有调用点/测试引用。


