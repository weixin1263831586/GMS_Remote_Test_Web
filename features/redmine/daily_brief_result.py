"""Single schema for new model output and the generated prompt contract."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


MAX_SIMILAR_ISSUES = 4
CONFIDENCE_HUMAN_REVIEW_THRESHOLD = 0.6


class ResultObject(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", allow_inf_nan=False)


class Evidence(ResultObject):
    source: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    fact: str = Field(min_length=1)


class RecommendedAction(ResultObject):
    action: str
    step: int = Field(default=1, ge=1)
    reason: str = ""


class SimilarIssue(ResultObject):
    issue_id: int = Field(gt=0)
    subject: str = ""
    similarity: Literal["same", "similar", "related"]
    reusable_fix: str = ""
    reference_fact: str = ""


class IssueResult(ResultObject):
    problem_summary: str = Field(min_length=1)
    customer_request: str = Field(min_length=1)
    current_blocker: str
    root_cause: str
    root_cause_type: Literal["confirmed", "likely", "possible", "unknown"]
    recommended_actions: list[RecommendedAction] = Field(max_length=5)
    suggested_solution: str = Field(min_length=1)
    detailed_report: str = Field(min_length=1)
    evidence: list[Evidence] = Field(max_length=5)
    similar_issues: list[SimilarIssue] = Field(max_length=MAX_SIMILAR_ISSUES)
    confidence: float = Field(ge=0, le=1, description="Model self-estimate, not verified evidence confidence")
    risk: Literal["high", "medium", "low"]
    missing_information: list[str] = Field(max_length=5)
    suggested_reply_en: str
    suggested_reply_zh: str


ISSUE_RESULT_SCHEMA = IssueResult.model_json_schema()
ISSUE_RESULT_REQUIRED_FIELDS = tuple(ISSUE_RESULT_SCHEMA["required"])
ROOT_CAUSE_TYPES = tuple(ISSUE_RESULT_SCHEMA["properties"]["root_cause_type"]["enum"])
RISK_LEVELS = tuple(ISSUE_RESULT_SCHEMA["properties"]["risk"]["enum"])
SIMILARITY_LEVELS = tuple(ISSUE_RESULT_SCHEMA["$defs"]["SimilarIssue"]["properties"]["similarity"]["enum"])


RESULT_SCHEMA_VERSION = 2


def validate_issue_result(result: dict[str, Any]) -> list[str]:
    """Validate new output and canonicalize it in place.

    ``extra="ignore"`` 会让未知字段被 Pydantic 丢弃，但原始
    dict 仍可能继续带着 legacy/hallucinated 字段进入持久层。这里在验证成功后
    用 ``model_dump()`` 覆盖原 dict，只保留 schema 内字段（并写入
    ``result_schema_version``），保证持久化表示 canonical。运行时附加字段
    （history_checked / evidence_gate / needs_human_review）在验证后由调用方
    写回，不受影响。
    """
    try:
        parsed = IssueResult.model_validate(result)
    except ValidationError as exc:
        return [
            ("missing field: " if error["type"] == "missing" else "invalid field: ")
            + ".".join(str(part) for part in error["loc"])
            + ("" if error["type"] == "missing" else f": {error['msg']}")
            for error in exc.errors(include_input=False)
        ]
    canonical = parsed.model_dump(mode="json")
    canonical["result_schema_version"] = RESULT_SCHEMA_VERSION
    result.clear()
    result.update(canonical)
    if confidence_below_review_threshold(result):
        result["needs_human_review"] = True
    return []


def confidence_below_review_threshold(result: dict[str, Any]) -> bool:
    value = result.get("confidence")
    return isinstance(value, bool) or not isinstance(value, (int, float)) or not (
        CONFIDENCE_HUMAN_REVIEW_THRESHOLD <= value <= 1
    )
