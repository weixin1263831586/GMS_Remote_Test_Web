"""跨模块反向关联：根据笔记的 related_module（module::test_case）聚合
历史测试报告 + Redmine 成熟案例。

聚合层只**只读调用**现有方法，不修改 reports / redmine 的任何后端代码，
避免破坏 /api/reports/diagnose 与 redmine-agent 工作流。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def parse_related_module(related_module: str) -> tuple[str, str]:
    """拆分 `module::test_case` → (module, test_case)。任一可能为空。"""
    text = (related_module or "").strip()
    if "::" in text:
        module, _, test_case = text.partition("::")
        return module.strip(), test_case.strip()
    return text, ""


def _safe_json(raw: Any) -> Any:
    """redmine 的 *_json 字段可能是 str 或已解码对象，统一还原。"""
    if isinstance(raw, str):
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw


def find_related_reports(module: str, test_case: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """按 module（精确）+ test_case（可选精排）过滤历史报告。

    reports 记录字段名是 test_module / test_case（report_store.py），这里做映射。
    单源失败返回空列表，不抛异常。
    """
    if not module:
        return []
    try:
        from features.reports.repository import test_report_db

        reports = test_report_db.get_reports(limit=500)
    except Exception as e:  # reports 模块不可用时不阻塞关联
        logger.debug("[relations] reports lookup failed: %s", e)
        return []

    results: list[dict[str, Any]] = []
    for r in reports:
        try:
            if (r.get("test_module") or "") != module:
                continue
            # test_case 非空时二次精排（不要求，module 命中即可）。
            if test_case and (r.get("test_case") or "") not in ("", test_case):
                # 模糊：report 的 test_case 可能含类名前缀，放宽匹配。
                if test_case not in (r.get("test_case") or ""):
                    continue
            results.append(
                {
                    "timestamp": r.get("timestamp"),
                    "test_type": r.get("test_type"),
                    "test_module": r.get("test_module"),
                    "test_case": r.get("test_case"),
                    "devices": r.get("devices"),
                    "result_dir": r.get("result_dir"),
                    "status": r.get("status"),
                    "source": "report",
                }
            )
            if len(results) >= limit:
                break
        except Exception:
            continue
    return results


def find_related_redmine_cases(request: Any, module: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """按 module 过滤 Redmine 成熟案例。

    list_mature_cases 没有 module 参数（只有 search），所以全量取后在内存过滤。
    solution_json / source_issue_ids_json 强制 json.loads 容错。单源失败返回空。
    """
    if not module:
        return []
    try:
        from features.redmine.api import get_redmine_service_for_request

        service = get_redmine_service_for_request(request)
        payload = service.knowledge.list_mature_cases(limit=200, search="")
        items = payload.get("items", []) if isinstance(payload, dict) else (payload or [])
    except Exception as e:  # redmine 模块不可用时不阻塞关联
        logger.debug("[relations] redmine mature-cases lookup failed: %s", e)
        return []

    results: list[dict[str, Any]] = []
    for c in items:
        try:
            if (c.get("module") or "") != module:
                continue
            results.append(
                {
                    "case_id": c.get("case_id"),
                    "title": c.get("title"),
                    "module": c.get("module"),
                    "chip_platform": c.get("chip_platform"),
                    "android_version": c.get("android_version"),
                    "canonical_error_signature": c.get("canonical_error_signature"),
                    "solution": _safe_json(c.get("solution_json")),
                    "source_issue_ids": _safe_json(c.get("source_issue_ids_json")) or [],
                    "status": c.get("status"),
                    "source": "redmine_case",
                }
            )
            if len(results) >= limit:
                break
        except Exception:
            continue
    return results


def build_related(request: Any, related_module: str, note_id: str = "") -> dict[str, Any]:
    """聚合反向关联：reports + redmine mature-cases。"""
    module, test_case = parse_related_module(related_module)
    reports = find_related_reports(module, test_case)
    redmine_cases = find_related_redmine_cases(request, module)
    return {
        "note_id": note_id,
        "related_module": related_module,
        "module": module,
        "test_case": test_case,
        "reports": reports,
        "redmine_cases": redmine_cases,
        "total": len(reports) + len(redmine_cases),
    }
