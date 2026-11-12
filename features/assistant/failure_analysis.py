"""Saved-report diagnosis and suite APK source lookup for the Agent."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from features.assistant.executor import _json_body
from features.assistant.session_log import _append_message, _append_step
from features.assistant.session_registry import AgentRequestShim
from features.reports import ReportDiagnosisRequest, test_report_db
from features.test_execution import SuiteApkAnalyzeRequest
from features.users import owner_id_from_request
from foundation.error_model import record_internal_error


logger = logging.getLogger(__name__)


def _latest_report_for_client(client_id: str, exclude_timestamp: str | None = None) -> dict[str, Any] | None:
    reports = test_report_db.get_reports(limit=20, owner_id=client_id)
    if exclude_timestamp:
        for report in reports:
            if report.get("timestamp") == exclude_timestamp:
                break
            return report
        return None
    return reports[0] if reports else None


def _report_failed(report: dict[str, Any] | None) -> bool:
    if not report:
        return False
    fail_count = report.get("fail", report.get("fail_count", 0)) or 0
    try:
        return int(fail_count) > 0
    except Exception:
        return False


def _report_for_cluster_job(
    job_id: str,
    attempt_id: str = "",
    owner_id: str = "",
) -> dict[str, Any] | None:
    """Resolve only the report produced by one durable Worker attempt."""
    if not job_id or not owner_id:
        return None
    report = test_report_db.get_report_by_timestamp(
        f"cluster-{job_id}",
        owner_id=owner_id,
    )
    if not report or report.get("cluster_job_id") != job_id:
        return None
    if attempt_id and report.get("attempt_id") != attempt_id:
        return None
    return report


def _normalize_failure(raw_failure: dict[str, Any], index: int = 0) -> dict[str, Any]:
    """Return one failure in the shape expected by diagnosis APIs."""
    raw_failure = raw_failure or {}
    test_name = raw_failure.get("name") or raw_failure.get("test_name") or raw_failure.get("test") or ""
    reason = raw_failure.get("reason") or raw_failure.get("error_message") or raw_failure.get("message") or ""
    stack_trace = raw_failure.get("stack_trace") or raw_failure.get("trace") or reason
    return {
        "index": index,
        "test_name": str(test_name or ""),
        "module": str(raw_failure.get("module") or raw_failure.get("test_module") or ""),
        "error_message": str(reason or ""),
        "stack_trace": str(stack_trace or ""),
    }


async def _analyze_saved_report(
    session: dict[str, Any], report_timestamp: str, request: Any = None
) -> dict[str, Any] | None:
    """Analyze a saved report and append an Agent step."""
    from features.reports import test_report_manager

    _append_step(session, "报告分析", "running", f"正在分析报告 {report_timestamp}")
    try:
        # 报告按账号分区（ADR 0010）：owner 一律取请求主体的
        # resource_owner_id（executor._query_reports 同款），agent 主体映射
        # 回注册账号；会话里残留的 client_id 只作无请求时的兜底。
        owner_id = str(
            owner_id_from_request(request)
            if request is not None
            else (session.get("owner_id") or session.get("client_id") or "")
        )
        if not owner_id:
            _append_step(session, "报告分析", "warning", "缺少当前用户身份")
            return None
        report = test_report_db.get_report_by_timestamp(
            report_timestamp,
            owner_id=owner_id,
        )
        if not report:
            _append_step(session, "报告分析", "warning", "报告不存在或不属于当前用户")
            return None
        analysis = await asyncio.to_thread(
            test_report_manager.analyze_report_by_id,
            str(report["report_id"]),
            owner_id=owner_id,
        )
        if not analysis:
            _append_step(session, "报告分析", "warning", "报告存在，但未能解析 test_result.xml")
            return None

        summary = analysis.get("summary") or {}
        detail = f"总计 {summary.get('total', 0)}，通过 {summary.get('pass', 0)}，失败 {summary.get('fail', summary.get('failed', 0))}"
        _append_step(session, "报告分析", "done", detail, {"report_analysis": analysis})
        return analysis
    except Exception:
        message = record_internal_error(logger, "报告分析", "[Agent] report analysis failed")
        _append_step(session, "报告分析失败", "error", message)
        return None


async def _diagnose_report_failure(session: dict[str, Any], report: dict[str, Any], analysis: dict[str, Any], failure: dict[str, Any]) -> dict[str, Any] | None:
    """Run the existing report diagnosis pipeline for one failure."""
    from features.reports import diagnose_report_failure

    report_timestamp = report.get("timestamp", "")
    details = analysis.get("details") or {}
    req = ReportDiagnosisRequest(
        test_name=failure.get("test_name") or "Unknown",
        error_message=failure.get("error_message") or "",
        stack_trace=failure.get("stack_trace") or "",
        report_name=report_timestamp,
        failure_index=int(failure.get("index") or 0),
        module=failure.get("module") or report.get("test_module") or "",
        test_type=details.get("test_type") or report.get("test_type") or "",
        suite_version=details.get("suite_version") or report.get("suite_version") or "",
    )

    _append_step(session, "失败诊断", "running", req.test_name)
    try:
        response = await diagnose_report_failure(req)
        payload = _json_body(response)
        if not payload.get("success"):
            _append_step(session, "失败诊断失败", "warning", payload.get("error") or "诊断失败", payload)
            return None
        diagnosis = payload.get("data") or {}
        root_cause = (diagnosis.get("ai_result") or {}).get("root_cause") or diagnosis.get("summary") or "诊断完成"
        suite_target = diagnosis.get("suite_target") or {}
        artifact = suite_target.get("artifact") or {}
        _append_step(
            session,
            "失败诊断",
            "done",
            root_cause,
            {
                "diagnosis": diagnosis,
                "artifact": artifact,
                "source_path": diagnosis.get("source_path") or (suite_target.get("source_guess") or {}).get("source_path", ""),
            },
        )
        return diagnosis
    except Exception:
        message = record_internal_error(logger, "失败诊断", "[Agent] failure diagnosis failed")
        _append_step(session, "失败诊断异常", "error", message)
        return None


def _extract_symbols_for_apk_lookup(diagnosis: dict[str, Any], failure: dict[str, Any]) -> list[str]:
    symbols: list[str] = []
    for class_name in diagnosis.get("class_names") or []:
        simple = str(class_name).split(".")[-1].split("$")[0]
        if simple and simple not in symbols:
            symbols.append(simple)
    test_name = failure.get("test_name") or ""
    if "#" in test_name:
        cls, method = test_name.split("#", 1)
        for value in (cls.split(".")[-1].split("$")[0], method):
            if value and value not in symbols:
                symbols.append(value)
    return symbols[:5]


async def _wait_for_apk_analysis(
    task_id: str, request: AgentRequestShim, timeout_seconds: int = 180
) -> dict[str, Any] | None:
    from features.firmware import get_apk_status

    deadline = time.time() + timeout_seconds
    last_status = None
    while time.time() < deadline:
        payload = _json_body(await get_apk_status(task_id, request))
        data = payload.get("data") or {}
        last_status = data
        if data.get("status") == "completed":
            return data
        if data.get("status") == "error":
            return data
        await asyncio.sleep(3)
    return last_status


async def _read_apk_source_snippet(
    task_id: str, diagnosis: dict[str, Any], failure: dict[str, Any],
    request: AgentRequestShim,
) -> dict[str, Any] | None:
    """Find a likely decompiled source file and read a short snippet."""
    from features.firmware import find_apk_symbol_definition, get_apk_source

    symbols = _extract_symbols_for_apk_lookup(diagnosis, failure)
    definition = None
    for symbol in symbols:
        payload = _json_body(await find_apk_symbol_definition(
            task_id, request, symbol=symbol
        ))
        if payload.get("success"):
            definition = (payload.get("data") or {}).get("definition")
            if definition:
                break

    if not definition:
        return None

    path = definition.get("path") or ""
    if not path:
        return None

    payload = _json_body(await get_apk_source(
        task_id, request, path=path, view=True
    ))
    if not payload.get("success"):
        return {"definition": definition, "path": path, "error": payload.get("error") or "源码读取失败"}

    source = payload.get("data") or {}
    content = source.get("content") or ""
    line = int(definition.get("line") or 1)
    lines = content.splitlines()
    start = max(0, line - 8)
    end = min(len(lines), line + 12)
    snippet = "\n".join(f"{idx + 1}: {lines[idx]}" for idx in range(start, end))
    return {"definition": definition, "path": path, "line": line, "snippet": snippet}


async def _run_apk_source_analysis(
    session: dict[str, Any], plan: dict[str, Any], diagnosis: dict[str, Any],
    failure: dict[str, Any], request: AgentRequestShim,
) -> dict[str, Any] | None:
    """Import a suite APK/JAR, decompile it, and read a likely source snippet."""
    from features.firmware import analyze_apk
    from features.test_execution import create_suite_apk_analysis_task

    suite_target = diagnosis.get("suite_target") or {}
    artifact = suite_target.get("artifact") or {}
    artifact_path = artifact.get("path") or ""
    suite_path = suite_target.get("suite_path") or (plan.get("request") or {}).get("test_suite") or ""

    if not suite_path or not artifact_path:
        _append_step(session, "APK/源码分析", "warning", "未定位到可反编译的套件 APK/JAR", {"suite_target": suite_target})
        return None

    _append_step(session, "APK/源码分析", "running", f"导入构件 {artifact_path}")
    try:
        req = SuiteApkAnalyzeRequest(suite_path=suite_path, path=artifact_path)
        create_payload = _json_body(await create_suite_apk_analysis_task(req, request))
        if not create_payload.get("success"):
            _append_step(session, "APK/源码分析失败", "warning", create_payload.get("error") or "构件导入失败", create_payload)
            return None

        task = create_payload.get("data") or {}
        task_id = task.get("task_id")
        if not task_id:
            _append_step(session, "APK/源码分析失败", "warning", "反编译任务 ID 为空", create_payload)
            return None

        start_payload = _json_body(await analyze_apk(task_id, request))
        if not start_payload.get("success"):
            _append_step(session, "APK/源码分析失败", "warning", start_payload.get("error") or "反编译启动失败", start_payload)
            return None

        status = await _wait_for_apk_analysis(task_id, request)
        if not status or status.get("status") != "completed":
            _append_step(session, "APK/源码分析", "warning", f"反编译未完成: {(status or {}).get('status', 'timeout')}", {"task_id": task_id, "status": status})
            return {"task_id": task_id, "status": status}

        snippet = await _read_apk_source_snippet(task_id, diagnosis, failure, request)
        detail = f"反编译完成: {task.get('filename') or artifact_path}"
        if snippet and snippet.get("path"):
            detail += f"，命中源码 {snippet['path']}:{snippet.get('line', '')}"
        result = {"task_id": task_id, "task": task, "status": status, "snippet": snippet, "artifact": artifact}
        _append_step(session, "APK/源码分析", "done", detail, result)
        return result
    except Exception:
        message = record_internal_error(logger, "APK/源码分析", "[Agent] APK source analysis failed")
        _append_step(session, "APK/源码分析异常", "error", message)
        return None


async def _run_failure_analysis_pipeline(
    session: dict[str, Any], plan: dict[str, Any], report: dict[str, Any],
    request: AgentRequestShim,
) -> dict[str, Any]:
    """Analyze report failures, diagnose the first failure, and optionally inspect APK source."""
    report_timestamp = report.get("timestamp", "")
    analysis = await _analyze_saved_report(session, report_timestamp, request)
    if not analysis:
        return {"report_timestamp": report_timestamp, "report_analysis": None}

    failures = [
        _normalize_failure(item, idx)
        for idx, item in enumerate(analysis.get("failures") or [])
    ]
    failures = [item for item in failures if item.get("test_name") or item.get("error_message")]
    result = {"report_timestamp": report_timestamp, "report_analysis": analysis, "failures": failures[:10]}
    if not failures:
        _append_message(session, "assistant", f"报告 {report_timestamp} 已分析，但没有提取到明确失败用例。")
        return result

    primary_failure = failures[0]
    diagnosis = await _diagnose_report_failure(session, report, analysis, primary_failure)
    result["diagnosis"] = diagnosis

    apk_result = None
    if diagnosis and (plan.get("policy") or {}).get("apk_source_analysis"):
        apk_result = await _run_apk_source_analysis(
            session, plan, diagnosis, primary_failure, request
        )
        result["apk_source_analysis"] = apk_result

    root_cause = ((diagnosis or {}).get("ai_result") or {}).get("root_cause") or (diagnosis or {}).get("summary") or "未生成根因"
    message_lines = [
        f"报告分析完成：{report_timestamp}",
        f"- 失败用例数: {len(failures)}",
        f"- 首个失败: {primary_failure.get('test_name') or 'Unknown'}",
        f"- 模块: {primary_failure.get('module') or '未知'}",
        f"- 根因: {root_cause}",
    ]
    if apk_result:
        snippet = apk_result.get("snippet") or {}
        message_lines.append(f"- APK任务: {apk_result.get('task_id')}")
        if snippet.get("path"):
            message_lines.append(f"- 源码命中: {snippet.get('path')}:{snippet.get('line')}")
    message_lines.append("后台分析已完成，当前仍保留在对话Agent页面；需要查看详情时点击“打开报告分析”或“打开APK分析”。")
    _append_message(
        session,
        "assistant",
        "\n".join(message_lines),
        kind="analysis",
        data={
            "report_timestamp": report_timestamp,
            "analysis": result,
            "action": "open_report_analysis",
        },
    )
    return result
