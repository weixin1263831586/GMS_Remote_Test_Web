"""Diagnosis Golden Corpus 语料契约与确定性评分引擎（全局审查：诊断质量工程）。

Wiki 侧已有 Golden Query quality gate（knowledge-quality CI）；本模块把同一
思想扩展到 AI 诊断质量：

- ``load_corpus``：读取 ``tests/quality/*.jsonl``（development/holdout 分池）；
- ``grade_result``：对一个诊断结果载荷做**确定性**检查——该找的证据有没有
  找（evidence recall）、有没有漏查历史案例、根因类是否在允许集合内、
  是否做出禁止声明（幻觉防护）。不比较生成 Markdown 的相似度。

评测脚本（``tools/scripts/testing/eval_diagnosis_quality.py``）复用本引擎；
``tests/quality/test_diagnosis_corpus_contract.py`` 保证语料本身永远合法。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CORPUS_DIR = Path(__file__).resolve().parent

CORPUS_FILES = ("redmine_diagnosis_cases.jsonl", "report_failure_cases.jsonl")

VALID_SPLITS = ("development", "holdout")
VALID_PURPOSES = ("redmine_diagnosis", "report_failure")

#: root-cause / claim 的封闭词表：防语料漂移出无法统计的自由文本。
VALID_ROOT_CAUSE_CLASSES = frozenset(
    {
        "resource_overlay",
        "dynamic_color_calculation",
        "test_environment",
        "memory_pressure",
        "lmkd_kill",
        "test_setup",
        "rendering_pipeline",
        "vsync_misalignment",
        "device_specific",
        "xml_truncation",
        "encoding_error",
        "report_format_change",
        "worker_transfer_failure",
        "staging_cleanup",
        "job_timeout",
    }
)
VALID_FORBIDDEN_CLAIMS = frozenset(
    {"hardware_failure", "device_failure", "root_cause_unknown_without_evidence"}
)


@dataclass
class GradeReport:
    """单条语料的评分结果；``passed=False`` 时 failures 说明缺什么。"""

    case_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "failures": self.failures,
            "checks": self.checks,
        }


def load_corpus(split: str | None = None) -> list[dict[str, Any]]:
    """读取全部语料；``split`` 过滤 development/holdout 池。"""
    cases: list[dict[str, Any]] = []
    for name in CORPUS_FILES:
        path = CORPUS_DIR / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            case = json.loads(line)
            if split is None or case.get("split") == split:
                cases.append(case)
    return cases


def grade_result(result: dict[str, Any], case: dict[str, Any]) -> GradeReport:
    """按 expected 对诊断结果做确定性评分。

    ``result`` 形状（DiagnosisReadModel 消费面，缺失字段按未满足计）：

        {
          "issue_id": 648526,
          "history_checked": true,          # 运行时注入，非模型自报
          "evidence_sources": ["test_source", "console_logs", ...],
          "retrieved_issue_ids": [648526, ...],
          "root_cause_class": "resource_overlay",
          "claims": ["overlay mismatch on Android 17"],
          "evidence_refs": ["issue:648526", "test_source:..."],
        }
    """
    expected = case.get("expected") or {}
    failures: list[str] = []
    checks: dict[str, bool] = {}

    def _record(name: str, ok: bool, why: str) -> None:
        checks[name] = ok
        if not ok:
            failures.append(why)

    # 1. evidence recall：应找到的历史案例。
    must_ids = [int(i) for i in expected.get("must_retrieve_issue_ids") or []]
    if must_ids:
        retrieved = {
            int(i)
            for i in (result.get("retrieved_issue_ids") or [])
            if str(i).strip().lstrip("-").isdigit()
        }
        missing = [i for i in must_ids if i not in retrieved]
        _record(
            "evidence_recall",
            not missing,
            f"missing historical evidence: {missing}",
        )

    # 2. history：是否真的查过历史（运行时注入的工具轨迹，不可被模型编造）。
    if expected.get("must_search_history"):
        _record(
            "history_searched",
            bool(result.get("history_checked")),
            "history search trace missing (history_checked=false)",
        )

    # 3. 取证来源门禁：test source / console logs 必须真的用过。
    sources = set(result.get("evidence_sources") or [])
    if expected.get("must_use_test_source"):
        _record(
            "test_source_used",
            "test_source" in sources,
            "expected test-source evidence but none was collected",
        )
    if expected.get("must_use_console_logs"):
        _record(
            "console_logs_used",
            "console_logs" in sources,
            "expected console-log evidence but none was collected",
        )

    # 4. root cause 分类必须落在允许集合内（同用例不同根因要能区分）。
    allowed = set(expected.get("allowed_root_cause_classes") or [])
    if allowed:
        actual = str(result.get("root_cause_class") or "")
        _record(
            "root_cause_class_allowed",
            actual in allowed,
            f"root_cause_class {actual!r} not in {sorted(allowed)}",
        )

    # 5. 幻觉防护：禁止声明不得出现在 claims 或结论文本里。
    forbidden = [str(c) for c in expected.get("forbidden_claims") or []]
    if forbidden:
        claims = [str(c) for c in result.get("claims") or []]
        claims.append(str(result.get("conclusion") or ""))
        leaked = [claim for claim in forbidden if any(claim in text for text in claims)]
        _record(
            "no_forbidden_claims",
            not leaked,
            f"forbidden claims present: {leaked}",
        )

    # 6. 结论必须挂证据（evidence gate 的最终表达）。
    refs = result.get("evidence_refs") or []
    _record(
        "conclusion_anchored",
        bool(refs),
        "conclusion carries no evidence anchors",
    )

    return GradeReport(
        case_id=str(case.get("id") or ""),
        passed=not failures,
        failures=failures,
        checks=checks,
    )
