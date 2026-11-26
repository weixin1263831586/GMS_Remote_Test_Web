from features.reports.failure_analysis import ReportFailureAnalyzer
from features.reports.rag_context import public_cdd_context


def _hit(**extra):
    url = "https://source.android.com/docs/compatibility/16/android-16-cdd#section"
    reference = "POL-" + "a" * 24
    return {"source": "web_documents", "source_path": reference, "source_revision": "revision",
            "snippet": "A public CDD candidate", "applicable_versions": "16",
            "source_anchors": [{"url": url, "revision": "revision", "evidence_type": "official"}],
            "extra": {"source_kind": "cdd", "status": "indexed", "reference": reference,
                      "source_url": url, "requirement_id": "C-1-1", **extra}}


def test_only_current_public_cdd_is_included_in_model_context():
    cdd = _hit()
    result = public_cdd_context([
        _hit(source_kind="partner"), _hit(status="stale"),
        _hit(source_url="https://docs.partner.android.com/gms/policies/domains/reqs"),
        {**cdd, "source": "android_internals"},
        {**cdd, "source_revision": "other"}, cdd, cdd,
    ])
    assert len(result) == 1
    assert result[0]["reference"] == cdd["source_path"]
    assert result[0]["evidence_level"] == "background" and not result[0]["applicability_verified"]


def test_report_prompt_contains_cdd_only_when_provided():
    analyzer = ReportFailureAnalyzer({})
    context = public_cdd_context([_hit()])
    prompt = analyzer._build_prompt("Class", "method", "error", "", None, retrieval_context=context)
    assert context[0]["reference"] in prompt and "适用性尚未验证" in prompt
    assert "公开 CDD 检索候选" not in analyzer._build_prompt("Class", "method", "error", "", None)
