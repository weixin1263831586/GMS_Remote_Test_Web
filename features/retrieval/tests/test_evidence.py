from features.retrieval import evidence_plan_context, summarize_evidence


def test_evidence_plan_distinguishes_missing_and_partial_sources():
    evidence = summarize_evidence("android_internals_search", {"success": True, "data": {
        "results": [{"source": "web_documents", "source_path": "POL-abc", "source_revision": "rev"}],
        "sources_status": [{"source": "web_documents", "status": "ready",
                            "retrieval": {"status": "partial", "coverage_complete": False}},
                           {"source": "android_internals", "status": "unavailable"}]}})
    assert evidence["returned"] == 1 and not evidence["coverage_complete"]
    assert evidence["references"] == [{"source": "web_documents", "reference": "POL-abc", "revision": "rev"}]
    assert "semantic:web_documents:partial" in evidence["gaps"]
    assert "source:android_internals:unavailable" in evidence["gaps"]
    assert evidence_plan_context([{"evidence": evidence}])["has_unresolved_scope"]


def test_no_match_and_unverified_scope_not_equivalent_to_absence():
    evidence = summarize_evidence("knowledge_search", {"data": {"items": [], "coverage_complete": False}})
    assert set(evidence["gaps"]) == {"incomplete_coverage", "no_match_in_returned_scope"}
    assert summarize_evidence("device_reboot", {"success": True, "data": {"status": "done"}}) is None


def test_context_is_bounded_and_has_omissions():
    evidence = {"coverage_complete": False, "gaps": ["no_match_in_returned_scope"]}
    context = evidence_plan_context([{"evidence": evidence}] * 12)
    assert len(context["retrievals"]) == 8
    assert context["omitted_retrievals"] == 4
