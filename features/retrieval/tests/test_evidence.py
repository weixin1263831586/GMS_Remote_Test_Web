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
    assert not evidence_followup_required([*observations, {"evidence_followup": True}])
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


def test_earlier_gap_survives_later_success():
    observations = [
        {"evidence": {"gaps": ["source:policy:unavailable"]}},
        {"evidence": {"gaps": [], "coverage_complete": True}},
    ]
    assert evidence_followup_required(observations)
    assert not evidence_followup_required([*observations, {"evidence_followup": True}])


def test_source_gap_prevents_complete_coverage_claim():
    result = summarize_evidence("knowledge_search", {"data": {
        "coverage_complete": True, "results": [],
        "sources_status": [{"source": "policy", "status": "ready", "coverage_complete": False}],
    }})
    assert not result["coverage_complete"]
    assert "source_coverage:policy" in result["gaps"]


def test_omitted_earlier_gap_and_nested_semantic_status():
    failed = summarize_evidence("knowledge_search", {"data": {
        "results": [{"reference": "r", "source_revision": "v"}],
        "coverage_complete": True, "retrieval": {"status": "partial", "coverage_complete": False},
    }})
    assert not failed["coverage_complete"]
    assert "semantic:knowledge_search:partial" in failed["gaps"]
    observations = [{"evidence": failed}] + [{"evidence": {"coverage_complete": True}}] * 10
    assert evidence_plan_context(observations)["has_unresolved_scope"]
    assert evidence_followup_required(observations)


def test_nested_source_coverage_and_pagination_require_followup():
    evidence = summarize_evidence("knowledge_search", {"data": {
        "coverage_complete": True, "results": [{"reference": "r", "source_revision": "v"}],
        "sources_status": [{"source": "cdd", "status": "ready",
                            "retrieval": {"status": "ready", "coverage_complete": False}}],
        "page_complete": False,
    }})
    assert not evidence["coverage_complete"]
    assert {"incomplete_evidence_index", "more_result_pages"} <= set(evidence["gaps"])
    assert evidence_followup_required([{"evidence": evidence}])


def test_batch_scope_and_invalid_matches_do_not_hide_gaps():
    evidence = summarize_evidence("redmine_evidence_search", {"data": {
        "coverage_complete": True,
        "queries": [{"matches": [{"reference": "r", "source_revision": "v"}],
                     "source_coverage_complete": False, "index_complete": False},
                    {"matches": 42, "page_complete": False}],
    }})
    assert evidence["returned"] == 1
    assert not evidence["coverage_complete"]
    assert {"incomplete_source_scope", "incomplete_evidence_index", "bounded_or_unconfirmed",
            "more_result_pages"} <= set(evidence["gaps"])


def test_summary_limits_cannot_claim_complete_coverage():
    evidence = summarize_evidence("knowledge_search", {"data": {
        "coverage_complete": True,
        "results": [{"reference": str(i), "source_revision": "v"} for i in range(31)],
    }})
    assert evidence["returned"] == 31
    assert len(evidence["references"]) == 16
    assert not evidence["coverage_complete"]
    assert "bounded_or_unconfirmed" in evidence["gaps"]


def test_nested_mcp_reference_groups_keep_source_coverage_and_citations():
    payload = {"success": True, "data": {"success": True, "data": {
        "coverage_complete": True,
        "sources": {
            "security_patches": {"status": "matched", "items": [
                {"reference": "ref:patch", "source_revision": "digest", "description": "private text"}
            ]},
            "critical_patches": {"status": "unavailable", "items": []},
            "exemptions": {"status": "not_applicable", "items": []},
        },
    }}}
    evidence = summarize_evidence("gms_rt_redmine_references", payload)
    assert evidence["references"] == [{"source": "security_patches", "reference": "ref:patch",
                                      "revision": "digest"}]
    assert not evidence["coverage_complete"]
    assert "source:critical_patches:unavailable" in evidence["gaps"]
    assert "source_coverage:exemptions" not in evidence["gaps"]
    assert "private text" not in str(evidence)


def test_nested_failure_is_not_hidden_by_outer_success():
    result = summarize_evidence("knowledge_search", {"success": True, "data": {
        "success": False, "data": {"results": [], "coverage_complete": True},
    }})
    assert "tool_failed" in result["gaps"]
    assert not result["coverage_complete"]


def test_sdk_gerrit_relation_requires_exact_project_and_current_commit():
    commit = "a" * 40
    sdk = summarize_evidence("sdk_search", {"data": {
        "source_id": "sdk", "commit": commit, "reproducible": True,
        "repo_scope": "frameworks/base", "matches": [{"path": "Test.java", "line": 12}],
    }})
    gerrit = summarize_evidence("gerrit_dashboard_changes", {"data": {
        "configured": True, "items": [
            {"project": "frameworks/base", "_number": 1, "current_revision": commit},
            {"project": "other", "number": 2, "currentPatchSet": {"revision": commit}},
            {"project": "frameworks/base", "_number": 3, "current_revision": "b" * 40,
             "revisions": {commit: {}}},
        ],
    }})
    links = evidence_plan_context([{"evidence": sdk}, {"evidence": gerrit}])["sdk_gerrit_links"]
    assert links == [{"project": "frameworks/base", "revision": commit,
                      "sdk_reference": "frameworks/base/Test.java:12",
                      "gerrit_reference": "gerrit:frameworks/base:1",
                      "relation": "same_project_and_commit", "applicability_verified": False}]


def test_index_integrity_and_truncation_survive_context_budget():
    evidence = summarize_evidence("knowledge_search", {"data": {
        "coverage_complete": True, "index_consistent": False, "truncated": True,
        "results": [{"reference": "r", "source_revision": "v"}],
    }})
    assert not evidence["coverage_complete"]
    assert {"incomplete_evidence_index", "bounded_or_unconfirmed"} <= set(evidence["gaps"])
    context = evidence_plan_context([{"evidence": evidence}] +
                                    [{"evidence": {"coverage_complete": True}}] * 9)
    assert "incomplete_evidence_index" in context["unresolved_gaps"]
    assert context["has_unresolved_scope"]
