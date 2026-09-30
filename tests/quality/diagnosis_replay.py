"""Offline replay through production adapters; tool facts never come from AI claims."""

from typing import Any

from features.redmine.diagnosis_read_model import read_model_from_issue_result
from features.reports.diagnosis_read_model import read_model_from_report_diagnosis
from foundation.diagnosis_read_model import validate_diagnosis_read_model


def replay_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    native = fixture["native_result"]
    if fixture["purpose"] == "redmine_diagnosis":
        model = read_model_from_issue_result(native, issue_id=fixture["issue_id"])
    elif fixture["purpose"] == "report_failure":
        model = read_model_from_report_diagnosis(native)
    else:
        raise ValueError("unsupported replay purpose")
    if not model or validate_diagnosis_read_model(model):
        raise ValueError("invalid replay read model")
    # Freeze the relevant consumer fields separately from the native payload.
    for field, expected in fixture["read_model"].items():
        if model[field] != expected:
            raise ValueError(f"{fixture['case_id']}: read-model drift in {field}")
    traces = [event for event in fixture["tool_trace"]
              if event.get("ok") is True and event.get("result")]
    refs = {item["reference"] for item in model["evidence"] if item["reference"]}
    sources = {
        event["source"] for event in traces
        if event.get("reference") in refs
    }
    retrieved = {int(case["id"]) for case in model["similar_cases"]
                 if str(case.get("id") or "").isdigit()}
    history = [event for event in traces if event["source"] == "history"]
    observed = {int(issue_id) for event in history
                for issue_id in event["result"].get("issue_ids", [])}
    conclusion = model["conclusion"] or {}
    return {
        "history_checked": bool(history),
        "retrieved_issue_ids": sorted(retrieved & observed),
        "evidence_sources": sorted(sources),
        "root_cause_class": conclusion.get("root_cause", ""),
        "conclusion": conclusion.get("summary", ""),
        "claims": [*fixture.get("claims", []), conclusion.get("root_cause", "")],
        "evidence_refs": sorted(refs),
    }
