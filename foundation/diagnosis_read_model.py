"""DiagnosisReadModel：跨消费面的统一诊断读模型（全局审查第二十节）。

Web（报告分析页 / 晨报）、CLI（gms-rt-*）、MCP（gms_rt_*）与 Assistant
此前各自消费不同的诊断负载形状（reports 的平铺 ``diagnosis`` dict、
Redmine 的 ``IssueResult`` schema），同一段诊断结论在每个消费面要重复
适配。本模块定义唯一 canonical 读模型形状：生产方（reports 诊断编排、
Redmine 单号/晨报分析）各提供一个适配器把自己已有的分析结果投影成该
形状，消费方只需认得这一种结构。

设计约束：

- **纯函数、无 IO、不依赖任何 feature**：foundation 不得 import
  features（架构门禁 ``tests/architecture/``），本模块只做形状规范与
  防御性归一化。
- **serve-time 投影，不落库**：canonical 模型是读时派生物，持久层
  仍保存各生产方自己的结果（IssueResult 校验会剥离未知字段，读模型
  不得混入 ``validate_issue_result`` 的存储路径）。
- **章节封闭**：``failure_identity`` / ``evidence`` / ``background`` /
  ``similar_cases`` / ``failure_cluster`` / ``conclusion`` /
  ``recommended_actions`` 固定七节，字段缺失用空值显式表达而非省略
  键，方便消费方做 ``model.get(...)`` 式窄化。
- **防御性归一化**：生产方数据来自 AI 输出与多路召回，字段类型不可
  信；归一化函数对非 dict / 缺键 / 非 str 一律降级为空串或字符串化，
  绝不抛异常阻断诊断主流程。
"""

from __future__ import annotations

from typing import Any


#: 读模型 schema 版本（形状变更时递增；消费方按版本窄化）。
DIAGNOSIS_READ_MODEL_SCHEMA_VERSION = 1

#: 章节固定顺序（canonical 键序；dict 构造即按此排列）。
READ_MODEL_SECTIONS = (
    "schema_version",
    "producer",
    "subject",
    "failure_identity",
    "evidence",
    "background",
    "similar_cases",
    "failure_cluster",
    "conclusion",
    "recommended_actions",
)

#: 根因置信度封闭词表（与 Redmine ``root_cause_type`` 同源）。
CONCLUSION_STATUSES = ("confirmed", "likely", "possible", "unknown")

#: 长文本字段的保险上限（召回层已各自截断，这里只兜底异常超长输入）。
_MAX_TEXT = 2000

_PRODUCER_REPORTS = "reports.diagnose"
_PRODUCER_REDMINE = "redmine.issue_analysis"


def _clean_str(value: Any, limit: int = _MAX_TEXT) -> str:
    text = str(value) if value is not None else ""
    return text.strip()[:limit]


def _clean_optional_str(value: Any) -> str:
    text = _clean_str(value)
    return text


def normalize_recommended_actions(items: Any) -> list[dict[str, Any]]:
    """归一化建议行动：dict 形状取 action/step/reason，字符串视为单步建议。"""
    actions: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            action = _clean_str(item.get("action") or item.get("title") or item.get("suggestion"))
            reason = _clean_str(item.get("reason"))
            try:
                step = max(1, int(item.get("step") or len(actions) + 1))
            except (TypeError, ValueError):
                step = len(actions) + 1
        else:
            action = _clean_str(item)
            reason = ""
            step = len(actions) + 1
        if not action:
            continue
        actions.append({"action": action, "step": step, "reason": reason})
    actions.sort(key=lambda item: item["step"])
    return actions


def normalize_evidence_entries(items: Any) -> list[dict[str, Any]]:
    """归一化证据条目：{kind, source, reference, fact}。

    生产方证据形状各异（Redmine ``Evidence{source,reference,fact}``、
    reports 的 root_cause_evidence / 源码锚点 / Mainline 豁免），统一为
    四键；未知键丢弃，字符串条目整体视为 fact。
    """
    entries: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            entry = {
                "kind": _clean_str(item.get("kind"), 64),
                "source": _clean_str(item.get("source")),
                "reference": _clean_str(item.get("reference") or item.get("url") or item.get("path")),
                "fact": _clean_str(item.get("fact") or item.get("detail") or item.get("analysis")),
            }
            if not entry["fact"] and not entry["reference"]:
                continue
        elif isinstance(item, str):
            entry = {"kind": "", "source": "", "reference": "", "fact": _clean_str(item)}
            if not entry["fact"]:
                continue
        else:
            continue
        entries.append(entry)
    return entries


def _aggregate_anchor_status(item: dict[str, Any]) -> str:
    """从 ``anchor_verifications`` 聚合锚点核验状态（封闭三态）。

    顶层 ``evidence_level`` 是联邦层的 background 盖章（恒为
    "background"，``federation`` 强制重写），不是核验状态；三态结论只
    存在于内嵌 ``anchor_verifications[].evidence_level``。聚合规则：
    任一 path_matched → path_matched，否则存在 path_missing →
    path_missing，否则（含无验证行）unknown。
    """
    verifications = item.get("anchor_verifications")
    if not isinstance(verifications, list):
        return "unknown"
    levels = [
        str(v.get("evidence_level") or "")
        for v in verifications
        if isinstance(v, dict)
    ]
    if "path_matched" in levels:
        return "path_matched"
    if "path_missing" in levels:
        return "path_missing"
    return "unknown"


def _normalize_section(item: dict[str, Any]) -> dict[str, Any] | None:
    """归一化 wiki section 行号锚点 ``{heading, start_line, end_line}``。

    schema v4 的 section 检索给每条命中提供源文件精确锚点（heading 行
    起至正文末行，全局审查 off-by-one 修复后的语义）。生产方形状两种：
    ``item.extra.section``（联邦 ``to_dict`` 白名单透传）或平铺
    ``item.section``；行号非正整数视为缺失，返回 ``None``——锚点是
    Agent 引用用途，坏锚点宁可不给也不能给错的。
    """
    extra = item.get("extra")
    raw = None
    if isinstance(extra, dict):
        raw = extra.get("section")
    if not isinstance(raw, dict):
        raw = item.get("section")
    if not isinstance(raw, dict):
        return None
    try:
        start_line = int(raw.get("start_line") or 0)
        end_line = int(raw.get("end_line") or 0)
    except (TypeError, ValueError):
        return None
    if start_line <= 0 or end_line < start_line:
        return None
    return {
        "heading": _clean_str(raw.get("heading"), 200),
        "start_line": start_line,
        "end_line": end_line,
    }


def normalize_background_entries(items: Any) -> list[dict[str, Any]]:
    """归一化背景知识条目（ADR 0014：永远是 background，不作根因证据）。

    ``evidence_level`` 保留联邦层盖章（恒为 ``"background"``，标识证据
    层级而非核验状态）；锚点核验结论聚合进 ``anchor_status``（封闭三态
    path_matched / path_missing / unknown）。provenance 键
    （source_revision / license / last_verified）随条目透传：消费方引用
    外部知识时必须能履行 license 署名义务并判断条目新鲜度。
    ``section`` 携带 wiki 源文件行号锚点（schema v4 闭环，全局审查第七
    节：索引层已 section-aware，canonical 读模型必须同样携带）。
    显式不带 fact 语义字段——背景条目回答「Android 为什么这样工作」，
    不进入 conclusion。
    """
    entries: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        title = _clean_str(item.get("title") or item.get("heading") or item.get("name"))
        source = _clean_str(item.get("source") or item.get("provider"), 64)
        if not title and not source:
            continue
        entries.append({
            "title": title,
            "source": source,
            "path": _clean_str(item.get("path") or item.get("url") or item.get("source_path")),
            "detail": _clean_str(item.get("detail") or item.get("snippet") or item.get("summary")),
            "section": _normalize_section(item),
            "evidence_level": _clean_str(item.get("evidence_level"), 32),
            "anchor_status": _aggregate_anchor_status(item),
            "source_revision": _clean_str(item.get("source_revision"), 64),
            "license": _clean_str(item.get("license"), 64),
            "last_verified": _clean_str(item.get("last_verified"), 32),
        })
    return entries


def normalize_similar_cases(items: Any) -> list[dict[str, Any]]:
    """归一化相似案例：{kind, id, title, relation, fix}。

    ``relation`` 保留生产方原生词汇（Redmine similarity 的
    same/similar/related），不臆造 Failure Identity 的封闭关系分类——
    那是 ``features.redmine.failure_identity`` 的职责，合并判定不得
    从检索分数直接升级。
    """
    entries: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        case_id = item.get("issue_id", item.get("id"))
        title = _clean_str(item.get("subject") or item.get("title"))
        if case_id in (None, "") and not title:
            continue
        entries.append({
            "kind": _clean_str(item.get("kind"), 64),
            "id": None if case_id is None else (
                case_id if isinstance(case_id, (int, str)) else str(case_id)
            ),
            "title": title,
            "relation": _clean_str(item.get("relation") or item.get("similarity"), 64),
            "fix": _clean_str(item.get("fix") or item.get("reusable_fix") or item.get("resolution")),
            "fact": _clean_str(item.get("fact") or item.get("reference_fact")),
        })
    return entries


def build_conclusion(
    *,
    summary: Any = "",
    root_cause: Any = "",
    status: str = "unknown",
    confidence: float | None = None,
    risk: str = "",
    missing_information: Any = (),
    provider: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 conclusion 节（根因结论 + AI provider 元信息）。

    ``status`` 必须落在 :data:`CONCLUSION_STATUSES` 封闭词表内，未知值
    fail-closed 归为 ``unknown``，防止生产方自由词汇污染消费方判断。
    """
    if status not in CONCLUSION_STATUSES:
        status = "unknown"
    missing: list[str] = []
    for item in missing_information or ():
        text = _clean_str(item)
        if text:
            missing.append(text)
    return {
        "summary": _clean_str(summary),
        "root_cause": _clean_str(root_cause),
        "status": status,
        "confidence": confidence if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else None,
        "risk": _clean_str(risk, 16),
        "missing_information": missing,
        "provider": provider or {},
    }


def build_failure_cluster_stub(
    identity: dict[str, Any] | None, *, related_members: list[Any] | None = None
) -> dict[str, Any] | None:
    """构造 failure_cluster 节的保守形态。

    聚合 Cluster 需要 exact fingerprint 分组（``build_failure_clusters``），
    单条诊断在读取时刻没有聚合上下文，因此只暴露本条身份指纹与显式给出
    的已知同簇成员；没有指纹时整节为 ``None``。检索召回的相似案例**不得**
    计入 members——那是 SIMILAR_SYMPTOM 级候选，合并需过 relation judge。
    """
    if not identity or identity.get("clusterable") is False:
        return None
    fingerprint = _clean_str(identity.get("fingerprint"), 128)
    if not fingerprint:
        return None
    members = list(related_members or [])
    return {
        "fingerprint": fingerprint,
        "member_count": 1 + len(members),
        "members": members,
        "aggregated": bool(related_members),
    }


def build_diagnosis_read_model(
    *,
    producer: str,
    subject: dict[str, Any],
    failure_identity: dict[str, Any] | None = None,
    evidence: Any = (),
    background: Any = (),
    similar_cases: Any = (),
    failure_cluster: dict[str, Any] | None = None,
    conclusion: dict[str, Any] | None = None,
    recommended_actions: Any = (),
) -> dict[str, Any]:
    """从归一化后的章节构造 canonical 读模型。

    ``producer`` 是生产方标识（``reports.diagnose`` /
    ``redmine.issue_analysis``）；``subject`` 携带主体标识
    （issue_id / test_name / module 等），由适配器决定字段。
    """
    return {
        "schema_version": DIAGNOSIS_READ_MODEL_SCHEMA_VERSION,
        "producer": _clean_str(producer, 64),
        "subject": subject or {},
        "failure_identity": failure_identity or None,
        "evidence": normalize_evidence_entries(evidence),
        "background": normalize_background_entries(background),
        "similar_cases": normalize_similar_cases(similar_cases),
        "failure_cluster": failure_cluster,
        "conclusion": conclusion,
        "recommended_actions": normalize_recommended_actions(recommended_actions),
    }


def validate_diagnosis_read_model(model: Any) -> list[str]:
    """校验读模型形状，返回问题列表（空列表 = 合法）。

    只做形状与封闭词表校验，不校验内容语义；适配器测试与消费方契约
    测试共用本函数，保证「声明为 canonical ≠ 真的合法」在 CI 暴露。
    """
    if not isinstance(model, dict):
        return ["model must be a dict"]
    problems: list[str] = []
    if model.get("schema_version") != DIAGNOSIS_READ_MODEL_SCHEMA_VERSION:
        problems.append(f"schema_version must be {DIAGNOSIS_READ_MODEL_SCHEMA_VERSION}")
    for key in ("producer", "subject"):
        if not model.get(key):
            problems.append(f"missing field: {key}")
    conclusion = model.get("conclusion")
    if conclusion is not None:
        if not isinstance(conclusion, dict):
            problems.append("conclusion must be a dict or None")
        elif conclusion.get("status") not in CONCLUSION_STATUSES:
            problems.append("conclusion.status outside closed vocabulary")
    for key in ("evidence", "background", "similar_cases", "recommended_actions"):
        value = model.get(key)
        if not isinstance(value, list):
            problems.append(f"{key} must be a list")
    cluster = model.get("failure_cluster")
    if cluster is not None and not isinstance(cluster, dict):
        problems.append("failure_cluster must be a dict or None")
    identity = model.get("failure_identity")
    if identity is not None and not isinstance(identity, dict):
        problems.append("failure_identity must be a dict or None")
    return problems
