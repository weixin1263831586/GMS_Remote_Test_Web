from features.retrieval import evidence_plan_context, summarize_evidence
from features.retrieval.evidence import evidence_followup_required


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


def test_batch_evidence_references_and_scope_gaps():
    evidence = summarize_evidence("redmine_evidence_search", {"data": {
        "coverage_complete": True, "index_complete": False, "source_coverage_complete": False,
        "queries": [{"total": 3, "next_offset": 1, "matches": [
            {"reference": "snapshot/artifact/1", "source_revision": "sha", "source": "redmine_evidence"}]},
            {"total": 0, "matches": []}]}})
    assert evidence["returned"] == 1
    assert not evidence["coverage_complete"]
    assert evidence["references"][0]["revision"] == "sha"
    assert {"incomplete_source_scope", "incomplete_evidence_index", "more_result_pages",
            "batch_query_without_match"} <= set(evidence["gaps"])
    observations = [{"evidence": evidence}]
    assert evidence_followup_required(observations)
    assert not evidence_followup_required(observations + [{"evidence_followup": True}])
    assert not evidence_followup_required([{"evidence": {"gaps": []}}])


def test_sdk_and_gerrit_scope_and_pinned_source_citations():
    commit = "a" * 40
    sdk = summarize_evidence("sdk_search", {"data": {
        "source_id": "sdk", "commit": commit, "reproducible": True,
        "coverage_complete": True, "global_coverage_complete": False,
        "repo_scope": "frameworks/base", "matches": [{"path": "Test.java", "line": 12}]}})
    assert sdk["references"] == [{"source": "sdk", "reference": "frameworks/base/Test.java:12",
                                  "revision": commit}]
    assert "selected_sdk_scope_only" in sdk["gaps"]
    dynamic = summarize_evidence("sdk_search", {"data": {
        "source_id": "opengrok", "revision": "Android 16", "commit": "", "reproducible": False,
        "matches": [{"path": "Test.java", "revision": "Android 16", "line": 12}]}})
    assert not dynamic["references"]
    assert "sdk_revision_unpinned" in dynamic["gaps"]
    gerrit = summarize_evidence("gerrit_changes", {"data": {
        "configured": True, "items": [{"number": 42}], "stats": {"moreChanges": True}}})
    assert "more_result_pages" in gerrit["gaps"]


def test_existing_history_reference_reaches_common_evidence_plan():
    evidence = summarize_evidence("redmine_history_search", {"data": {
        "items": [{"issue_id": 42, "journal_reference": {"reference": "journal-token",
                                                         "source_revision": "rev"}}],
        "coverage_complete": False}})
    assert evidence["references"] == [{"source": "redmine_journals",
                                       "reference": "journal-token", "revision": "rev"}]
