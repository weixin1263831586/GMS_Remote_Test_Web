"""Report lookup includes native results through the authenticated Worker queue."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from features.auth import require_authenticated_user

from .execution_result import ToolResult
from .report_access import bind_report_access
from .route_invocation import call_router_function
from .tools import registry


class ReportQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test_type: str = ""
    worker_id: str = ""
    latest: bool = False
    limit: int = Field(default=10, ge=1, le=100)
    user_only: bool = False
    cursor: str = ""
    report_timestamp: str = ""
    cluster_job_id: str = ""
    attempt_id: str = ""
    automation_run_id: str = ""
    status: str = ""


def apply_report_goal_params(tool_name, params, message):
    """Keep explicit read-only type/latest constraints from the user's goal."""
    result = dict(params)
    if tool_name != "reports_list":
        return result
    types = set(re.findall(r"(?<![A-Za-z])(CTS|GTS|VTS|STS)(?![A-Za-z])", message, re.I))
    if len(types) == 1:
        result["test_type"] = types.pop().upper()
    if re.search(r"最新|\blatest\b|\bmost recent\b", message, re.I):
        count = re.search(r"最新\s*(\d+)\s*(?:份|个|次)", message)
        result.update(latest=True, limit=min(int(count.group(1)), 100) if count else 1)
    return result


def report_time(report):
    for key in ("start_ms", "start", "source_timestamp", "result_directory", "start_time", "timestamp", "report_name"):
        value = str(report.get(key) or "")
        if not value:
            continue
        if value.isdigit():
            return int(value) if len(value) >= 13 else int(value) * 1000
        match = re.search(r"(\d{4})\.(\d{2})\.(\d{2})_(\d{2})\.(\d{2})\.(\d{2})(?:\.(\d{3}))?", value)
        try:
            if match:
                parts = [int(x) for x in match.groups()[:6]]
                return int(datetime(*parts, microsecond=int(match.group(7) or 0) * 1000).timestamp() * 1000)
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            # Do not replace an unparseable test time with a newer upload time.
            if key == "start_time":
                return None
            continue
    return None


async def _call(name, session, request, params):
    return await call_router_function(registry.get(name), session, request, params)


async def _native_reports(session, request, query):
    inventory = await _call("cluster_suites", session, request, {"worker_id": query.worker_id})
    if not inventory.success:
        return [], [{"source": "native_suite", "error": inventory.error}], 0
    suites = (inventory.data or {}).get("suites")
    if not isinstance(suites, list):
        return [], [{"source": "native_suite", "error": "套件清单返回格式无效"}], 0
    suites = [suite for suite in suites if not query.test_type or
              str(suite.get("test_type") or suite.get("suite_type") or "").upper() == query.test_type]
    semaphore = asyncio.Semaphore(4)

    async def collect(suite):
        params = {"worker_id": suite.get("worker_id") or query.worker_id,
                  "suite_path": suite.get("tools_path") or suite.get("full_path"),
                  "metadata_only": True, "test_type": query.test_type, "limit": query.limit}
        async with semaphore:
            result = await _call("cluster_suite_results", session, request, params)
        return params, result

    reports, errors, scanned = [], [], 0
    for params, result in await asyncio.gather(*(collect(suite) for suite in suites)):
        identity = {"worker_id": params["worker_id"], "suite_path": params["suite_path"]}
        if not result.success:
            errors.append({**identity, "error": result.error})
            continue
        data = result.data or {}
        scanned += int(data.get("scanned_results") or 0)
        if data.get("coverage_complete") is not True:
            errors.append({**identity, "error": "部分原生结果不可读", "details": data.get("errors", [])})
        for report in data.get("reports") or []:
            report = {**report, **identity, "source": "native_suite", "registered": False}
            reports.append(report)
    return reports, errors, scanned


async def query_reports(session, request, params):
    try:
        query = ReportQuery(**params)
    except ValidationError:
        return ToolResult(success=False, tool_name="reports_list", error="报告查询参数无效，请按工具定义修正。")
    query.test_type = query.test_type.strip().upper()
    principal = require_authenticated_user(request)
    indexed_params = query.model_dump(exclude={"latest"})
    # The latest test can have been registered long before a recently uploaded
    # old report. Read all pages before comparing actual test timestamps.
    if query.latest:
        indexed_params.update(limit=100, cursor="")
    reports, errors, cursors = [], [], set()
    while True:
        indexed = await _call("reports_list", session, request, indexed_params)
        if not indexed.success:
            return indexed
        data = indexed.data or {}
        reports.extend({**item, "source": "registered", "registered": True} for item in data.get("reports", []))
        if not query.latest or not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor or cursor in cursors:
            errors.append({"source": "registered", "error": "报告分页未完整返回"})
            break
        cursors.add(cursor)
        indexed_params["cursor"] = cursor
    registered_count = len(reports)
    native_scanned = 0
    if query.latest:
        # Native directories have no account/job provenance. Do not treat them
        # as owned or as belonging to an explicitly filtered durable job.
        native_allowed = (not query.user_only and not any((query.report_timestamp, query.cluster_job_id,
                                                          query.attempt_id, query.automation_run_id, query.status)))
        if native_allowed:
            native, native_errors, native_scanned = await _native_reports(session, request, query)
            errors.extend(native_errors)
            reports.extend(native)
        else:
            native_scanned = None
        for report in reports:
            if report_time(report) is None:
                errors.append({"source": report["source"], "error": "报告缺少可比较的测试时间",
                               "timestamp": report.get("timestamp", "")})
        reports.sort(key=lambda report: (report_time(report) or 0, str(report.get("timestamp") or "")), reverse=True)
        # Avoid showing the same physical result twice.
        unique = {}
        for report in reports:
            key = (report.get("worker_id", ""), report.get("suite_path", ""),
                   report.get("result_directory") or report.get("source_timestamp") or report.get("timestamp"))
            unique.setdefault(key, report)
        reports = list(unique.values())[:query.limit]
    scope = {"registered_accounts": "accessible_accounts" if principal.role == "admin" and not query.user_only else "current_account",
             "worker_id": query.worker_id, "test_type": query.test_type,
             "native_suite_access": bool(query.latest and native_scanned is not None)}
    scope_text = (f"Worker {query.worker_id}" if query.worker_id else "可访问 Worker") + (f" / {query.test_type}" if query.test_type else "")
    actions = []
    for report in reports:
        action = bind_report_access(report)
        if action and action not in actions:
            actions.append(action)
    if reports:
        lines = []
        for report in reports:
            location = report.get("result_dir") or report.get("report_name") or report.get("timestamp", "")
            lines.append(f"- {report.get('test_type', '?')} {report.get('suite_version', '')} | {report.get('start_display') or report.get('start_time') or report.get('timestamp', '')} | pass {report.get('pass', 0)} fail {report.get('fail', 0)} total {report.get('total', 0)}\n  {location}")
            lines.append("  " + report["access"]["instruction"])
        text = f"查询范围：{scope_text}。\n" + "\n".join(lines)
    else:
        text = f"查询范围：{scope_text}；已登记报告未命中。"
        if query.latest and native_scanned is not None:
            text += "已检查可访问的原生套件结果。"
    if errors:
        text += "\n部分来源未能完整查询，当前结果不能确认为该范围的最新报告。"
    elif not query.latest:
        text += "\n本次仅查询已登记报告，未检查原生套件结果。"
    payload = {"reports": reports, "count": len(reports), "scope": scope,
               "coverage_complete": not errors, "errors": errors,
               "registered_count": registered_count, "native_scanned": native_scanned,
               "latest_confirmed": bool(query.latest and reports and not errors),
               "has_more": data.get("has_more", False) if not query.latest else False,
               "next_cursor": data.get("next_cursor", "") if not query.latest else ""}
    return ToolResult(success=bool(reports) or not errors, tool_name="reports_list", data=payload,
                      formatted_text=text, error="部分报告来源查询失败" if errors and not reports else "",
                      kind="table", page=actions[0]["page"] if actions else "",
                      entities={"reports": [r.get("timestamp", "") for r in reports if r["registered"]]},
                      quick_actions=actions)
