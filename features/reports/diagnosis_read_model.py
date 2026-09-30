"""报告诊断 → DiagnosisReadModel 投影（统一读模型，全局审查第二十节）。

``diagnose_report_failure`` 的编排结果是 reports 域原生的平铺 dict
（ai_result / source_search_results / knowledge_base_results /
system_background_results / suite_target / mainline_exemptions ...）。
本适配器在 serve 时刻把它投影成 ``foundation.diagnosis_read_model``
的 canonical 形状：CLI / MCP / Assistant 消费 ``read_model`` 章节，
Web 仍直接渲染原生字段，两套形状不再各自演化。

证据分层遵循 ADR 0014：内部案例与源码锚点是 evidence，外部 Android
机制知识永远是 background（``evidence_level`` 为 background 盖章，
``anchor_status`` 承载锚点核验三态），不得
进入 conclusion。
"""

from __future__ import annotations

from typing import Any

from foundation.diagnosis_read_model import (
    build_conclusion,
    build_diagnosis_read_model,
    build_failure_cluster_stub,
)


def _code_search_reference(hit: dict[str, Any]) -> str:
    path = str(hit.get("path") or "").strip()
    line = str(hit.get("line") or "").strip()
    if path and line:
        return f"{path}:{line}"
    return path or line


def _failure_identity_for(diagnosis: dict[str, Any]) -> dict[str, Any] | None:
    """从报告失败上下文派生确定性指纹（跨 feature 经 redmine 公共面）。

    失败原因是 Error Message 优先、堆栈兜底；suite 取 suite_target 的
    匹配套件名。任何异常（含循环 import 防线失效）都降级为 ``None``，
    不阻断诊断主流程。
    """
    try:
        from features.redmine import failure_identity
    except Exception:
        return None
    suite_target = diagnosis.get("suite_target") or {}
    suite = ""
    if isinstance(suite_target, dict):
        suite = str(
            suite_target.get("suite") or suite_target.get("suite_name")
            or suite_target.get("name") or ""
        )
    try:
        return failure_identity(
            [{
                "module": diagnosis.get("module") or "",
                "name": diagnosis.get("test_name") or "",
                "reason": diagnosis.get("error_message") or diagnosis.get("stack_trace") or "",
            }],
            suite=suite,
            android_version=str(diagnosis.get("android_version") or ""),
        )
    except Exception:
        return None


def read_model_from_report_diagnosis(diagnosis: dict[str, Any] | None) -> dict[str, Any] | None:
    """把 ``diagnose_report_failure`` 的编排结果投影为 canonical 读模型。"""
    if not isinstance(diagnosis, dict) or not diagnosis:
        return None
    ai_result = diagnosis.get("ai_result") or {}
    if not isinstance(ai_result, dict):
        ai_result = {}

    # evidence：AI 根因证据 + 源码锚点 + Mainline 豁免。
    evidence: list[dict[str, Any]] = []
    for item in ai_result.get("root_cause_evidence") or []:
        if isinstance(item, dict):
            evidence.append({**item, "kind": "ai_evidence"})
        elif item:
            evidence.append({"kind": "ai_evidence", "fact": str(item)})
    for hit in diagnosis.get("source_search_results") or []:
        if not isinstance(hit, dict):
            continue
        evidence.append({
            "kind": "source_anchor",
            "source": "codesearch",
            "reference": _code_search_reference(hit),
            "fact": hit.get("snippet") or hit.get("content") or hit.get("text") or "",
        })
    for item in diagnosis.get("mainline_exemptions") or []:
        if isinstance(item, dict):
            evidence.append({
                "kind": "mainline_exemption",
                "source": "mainline",
                "reference": str(item.get("issue_id") or item.get("id") or item.get("url") or ""),
                "fact": item.get("reason") or item.get("summary") or item.get("title") or "",
            })
        elif item:
            evidence.append({"kind": "mainline_exemption", "fact": str(item)})

    identity = _failure_identity_for(diagnosis)
    ai_enabled = bool(ai_result.get("ai_enabled"))
    root_cause = ai_result.get("root_cause") or ""
    if ai_enabled:
        status = "likely"
    elif root_cause:
        status = "possible"  # 规则兜底结论只是启发式，不得宣称 likely。
    else:
        status = "unknown"
    missing = [ai_result["ai_error"]] if ai_result.get("ai_error") else []
    if not root_cause and not missing:
        missing = ["no root cause available from AI or rule fallback"]

    return build_diagnosis_read_model(
        producer="reports.diagnose",
        subject={
            "test_name": str(diagnosis.get("test_name") or ""),
            "module": str(diagnosis.get("module") or ""),
            "report_name": str(diagnosis.get("report_name") or ""),
            "failure_index": diagnosis.get("failure_index"),
            "android_version": str(diagnosis.get("android_version") or ""),
        },
        failure_identity=identity,
        evidence=evidence,
        background=diagnosis.get("system_background_results"),
        similar_cases=[
            {**case, "kind": "internal_case"} if isinstance(case, dict) else case
            for case in (diagnosis.get("knowledge_base_results") or [])
        ],
        failure_cluster=build_failure_cluster_stub(identity),
        conclusion=build_conclusion(
            summary=diagnosis.get("summary"),
            root_cause=root_cause,
            status=status,
            risk="high" if diagnosis.get("mainline_exempt") else "",
            missing_information=missing,
            provider={
                "model": ai_result.get("ai_model") or "",
                "provider": ai_result.get("ai_provider") or "",
                "fallback_used": bool(ai_result.get("ai_fallback_used")),
                "attempted": ai_result.get("ai_providers_attempted") or [],
            },
        ),
        recommended_actions=ai_result.get("suggestions"),
    )
