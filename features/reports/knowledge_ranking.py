"""Relevance helpers for report knowledge-base recall."""

from __future__ import annotations

import re

from .api_models import ReportDiagnosisRequest


def android_version_from_request(request: ReportDiagnosisRequest) -> str:
    """Best-effort Android major version from a suite version."""
    raw = (getattr(request, "suite_version", "") or "").strip()
    match = re.match(r"(\d+)", raw)
    return match.group(1) if match else ""


def test_method_and_class(test_name: str) -> tuple[str, str]:
    """Return method and simple class from ``a.b.C#method`` or ``a.b.C``."""
    test_name = (test_name or "").strip()
    method = ""
    class_name = test_name
    if "#" in test_name:
        class_name, method = test_name.split("#", 1)
    return method.strip(), class_name.rsplit(".", 1)[-1].strip()


def rank_kb_hits(hits: list[dict], probe: dict) -> list[dict]:
    """Filter and re-rank recalled cases against the diagnosed failure."""
    method, simple_class = test_method_and_class(probe.get("test_name") or "")
    module = (probe.get("module") or "").strip()
    probe_android = (probe.get("android_version") or "").strip()

    anchor_platform = ""
    if method:
        for hit in hits:
            if method.lower() in (hit.get("subject") or "").lower():
                anchor_platform = (hit.get("chip_platform") or "").upper()
                if anchor_platform:
                    break

    def score(hit: dict) -> tuple[float, bool]:
        subject = (hit.get("subject") or "").lower()
        root = (hit.get("root_cause") or "").lower()
        searchable = f"{subject} {root}"
        value = 0.0
        case_hit = bool(method and method.lower() in searchable)
        class_hit = bool(simple_class and simple_class.lower() in searchable)
        if case_hit:
            value += 100
        if class_hit:
            value += 40
        if module and module.lower() in searchable:
            value += 15
        if probe_android:
            theirs = (hit.get("android_version") or "").strip()
            if theirs and probe_android in theirs:
                value += 20
        if (hit.get("status_name") or "").lower() in (
            "closed", "confirmed", "已关闭", "已解决", "resolved",
        ):
            value += 10
        if hit.get("source") == "case_facts":
            value += 15
        solution = hit.get("solution_summary") or hit.get("root_cause") or ""
        if len(solution.strip()) >= 40:
            value += 8
        platform = (hit.get("chip_platform") or "").upper()
        keep = not (
            anchor_platform and platform and platform != anchor_platform
            and not (case_hit or class_hit)
        )
        return value, keep

    scored = []
    for hit in hits:
        value, keep = score(hit)
        if not keep:
            continue
        output = dict(hit)
        output["score"] = round(value, 1)
        output["similarity_level"] = (
            "exact" if value >= 100 else "high" if value >= 50
            else "medium" if value >= 30 else "low"
        )
        scored.append((value, output))

    if not scored:
        return [
            {**hit, "score": 0.0, "similarity_level": "low"}
            for hit in hits[:5]
        ]

    scored.sort(key=lambda item: item[0], reverse=True)
    ordered = [hit for _, hit in scored]
    if any(hit["similarity_level"] == "exact" for hit in ordered):
        precise = [
            hit for hit in ordered
            if hit["similarity_level"] in ("exact", "high")
        ]
        supporting = [
            hit for hit in ordered
            if hit["similarity_level"] not in ("exact", "high")
        ]
        return (precise + supporting)[:5]
    return ordered[:5]
