"""Redmine 诊断 → DiagnosisReadModel 投影（统一读模型，全局审查第二十节）。

单号分析 / 晨报共用的 AI 结果 schema（``daily_brief_models.IssueResult``）
是 Redmine 域的原生存储契约；本适配器在 serve 时刻把它投影成
``foundation.diagnosis_read_model`` 的 canonical 形状，供 CLI / MCP /
Assistant 消费，Web 仍可直接用原生 ``result`` 渲染完整字段。

投影语义：

- ``failure_identity`` / ``failure_cluster`` 取自该 issue 最近一次 AI
  执行轨迹里落库的确定性指纹（``daily_brief_service._analyze_one`` 写入
  ``record_ai_execution``），读取时刻不做聚合，成员永远只有本条。
- ``similar_cases`` 来自 ``IssueResult.similar_issues``，``relation``
  保留 same/similar/related 原生词汇；检索召回不改变 Failure Cluster
  归属（那是 relation judge 的职责）。
- ``IssueResult`` 的 current_blocker / suggested_reply_* 等客户沟通字段
  不进 canonical 模型——那是 Redmine 展示面语义，留在原生 ``result``。
"""

from __future__ import annotations

from typing import Any

from foundation.diagnosis_read_model import (
    _PRODUCER_REDMINE,
    build_conclusion,
    build_diagnosis_read_model,
    build_failure_cluster_stub,
)


def read_model_from_issue_result(
    result: dict[str, Any] | None,
    *,
    issue_id: int,
    subject: str = "",
    execution: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """把一条 IssueResult 投影为 canonical 读模型；无结果时返回 ``None``。

    ``execution`` 是最近一次 AI 执行记录（含 ``failure_identity`` 指纹），
    可选；缺指纹时对应章节为 ``None`` 而非空 dict，消费方按 ``is None``
    区分「无指纹」与「指纹为空」。
    """
    if not isinstance(result, dict) or not result:
        return None
    identity = execution.get("failure_identity") if isinstance(execution, dict) else None
    if not isinstance(identity, dict) or not identity.get("fingerprint"):
        identity = None
    return build_diagnosis_read_model(
        producer=_PRODUCER_REDMINE,
        subject={"issue_id": issue_id, "title": str(subject or result.get("subject") or "").strip()},
        failure_identity=identity,
        evidence=result.get("evidence"),
        similar_cases=[
            {**case, "kind": "internal_case"} if isinstance(case, dict) else case
            for case in (result.get("similar_issues") or [])
        ],
        failure_cluster=build_failure_cluster_stub(identity),
        conclusion=build_conclusion(
            summary=result.get("problem_summary"),
            root_cause=result.get("root_cause"),
            status=str(result.get("root_cause_type") or "unknown"),
            confidence=result.get("confidence"),
            risk=result.get("risk"),
            missing_information=result.get("missing_information"),
        ),
        recommended_actions=result.get("recommended_actions"),
    )
