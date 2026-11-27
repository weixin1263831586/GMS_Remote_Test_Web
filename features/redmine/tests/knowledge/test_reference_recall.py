"""Official references must reach Redmine analysis from owner-scoped evidence."""

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.gms import extract_reference_identifiers, init_gms_update_db, recall_mainline_references
from features.gms import query_mainline_security_patches as lookup_patches
from features.redmine import reference_recall
from features.redmine.analysis.kkagent.analyzer import KkAgentRedmineAnalyzer
from features.redmine.analysis.kkagent.evidence_gate import evaluate_evidence_gate, gate_errors
from features.redmine.analysis.kkagent.evidence_preflight import EvidencePreflight
from features.redmine.analysis.kkagent.trace import KkAgentTrace, ToolTrace, consume_line
from features.redmine.evidence import api as evidence_api
from features.redmine.evidence.store import EvidenceStore


@pytest.fixture
def references(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    data_root.mkdir()
    with sqlite3.connect(data_root / "gms_update.sqlite3") as conn:
        init_gms_update_db(conn)
        for index, release in enumerate(("RC9", "RC10", "RC12", "RC13", "PRELOAD")):
            conn.execute(
                "INSERT INTO mainline_security_patches (source_url,source_title,release_year,release_label,"
                "product_section,cve_id,mainline_module,version_note,first_seen_at,last_seen_at) "
                "VALUES (?, 'Official notes',2026,?,'Android','CVE-2026-58835','Bluetooth',"
                "'Fix ag/40830952 is included in the October Bluetooth module','2026-11-14','2026-11-14')",
                (f"https://example.com/notes/{index}", release),
            )
        conn.execute(
            "INSERT INTO mainline_known_issues (source_url,source_title,release_year,release_label,"
            "product_section,issue_type,android_versions,category,test_module,test_case,exemption_id,"
            "issue_text,first_seen_at,last_seen_at) VALUES ('https://example.com/exemption','Notes',2026,"
            "'October','Android','CTS','17','test','CtsBluetoothTestCases','pkg.bluetooth.Test#testFoo',"
            "'568181732','Known failure','2026-11-14','2026-11-14')"
        )
    with sqlite3.connect(data_root / "gms_update.sqlite3") as conn:
        conn.execute("INSERT INTO gms_update_bulletin_patches "
                     "(source_key,item_key,year,patch_id,description,bulletin_month,deadline,bulletin_url,content_hash,first_seen_at,last_seen_at) "
                     "VALUES ('gms_bulletins','fixture','2026','A-568181732','Bluetooth CVE-2026-58835 fix','November','','https://example.com/bulletin','hash','t','t')")
    owners = tmp_path / "owners"
    monkeypatch.setattr(reference_recall, "owner_redmine_root", lambda owner: owners / owner)
    monkeypatch.setattr(reference_recall, "recall_mainline_references",
                        lambda identifiers, **kwargs: recall_mainline_references(identifiers, data_root=data_root))
    store = EvidenceStore(owners / "owner-a")
    snapshot = store.create_snapshot(issue_id=661620, download_policy="all")
    store.update_snapshot(snapshot["snapshot_id"], status="ready",
                          manifest_json={"subject": "BTS warning", "description": "", "journals": []})
    return data_root, store, snapshot["snapshot_id"]


def add_text(store, snapshot, text, *, missing=False):
    artifact = store.create_artifact(snapshot_id=snapshot, attachment_id="1", filename="log.txt",
                                     original_filename="log.txt", content_type="text/plain", kind="text")
    if not missing:
        relative = f"661620/{snapshot}/{artifact['artifact_id']}.txt"
        path = store.resolve_internal(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        store.update_artifact(artifact["artifact_id"], status="ready", derived_text_path=relative)
    return artifact


def test_cve_in_ocr_tail_recalls_preload_and_other_official_sources(references):
    _root, store, snapshot = references
    add_text(store, snapshot, "x" * 65522 + " CVE-2026-58835 CtsBluetoothTestCases")
    result = reference_recall.collect_snapshot_references("owner-a", snapshot)
    assert result["coverage_complete"]
    assert "CVE-2026-58835" in result["identifiers"]
    assert "CVE-2026-5883" not in result["identifiers"]
    rows = result["sources"]["security_patches"]["items"]
    assert len(rows) == 5
    assert any(row["release_label"] == "PRELOAD" for row in rows)
    assert all(row["mainline_module"] == "Bluetooth" for row in rows)
    assert result["sources"]["exemptions"]["status"] == "matched"
    assert result["sources"]["critical_patches"]["status"] == "matched"


def test_missing_attachment_does_not_skip_later_readable_evidence(references):
    _root, store, snapshot = references
    add_text(store, snapshot, "", missing=True)
    add_text(store, snapshot, "CVE-2026-58835")
    result = reference_recall.collect_snapshot_references("owner-a", snapshot)
    assert result["coverage_complete"] is False
    assert result["warnings"]
    assert len(result["sources"]["security_patches"]["items"]) == 5


def test_partial_extraction_keeps_hits_but_reports_incomplete_coverage(references):
    _root, store, snapshot = references
    artifact = add_text(store, snapshot, "CVE-2026-58835")
    store.update_artifact(artifact["artifact_id"], derived_status="partial")
    result = reference_recall.collect_snapshot_references("owner-a", snapshot)
    assert result["coverage_complete"] is False
    assert result["warnings"]
    assert len(result["sources"]["security_patches"]["items"]) == 5


def test_exemption_bug_cross_references_critical_fix(references):
    root, _store, _snapshot = references
    result = recall_mainline_references(["pkg.bluetooth.Test#testFoo"], data_root=root)
    assert result["sources"]["exemptions"]["items"][0]["exemption_id"] == "568181732"
    assert result["sources"]["critical_patches"]["items"][0]["patch_id"] == "A-568181732"
    assert result["sources"]["security_patches"]["status"] == "not_applicable"


def test_latest_preload_precedes_older_rc_with_limited_recall(references):
    root, _store, _snapshot = references
    with sqlite3.connect(root / "gms_update.sqlite3") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE mainline_security_patches SET source_url=? WHERE release_label='RC13'",
                     ("https://example.com/notes-RC-2026-11-05-v0-1",))
        conn.execute("UPDATE mainline_security_patches SET source_url=? WHERE release_label='PRELOAD'",
                     ("https://example.com/notes-PRELOAD-2026-11-12-v0-1",))
        rows = lookup_patches(conn, ["CVE-2026-58835"], limit=1)
    assert rows[0]["release_label"] == "PRELOAD"


def test_change_id_also_recalls_its_security_patch(references):
    root, _store, _snapshot = references
    result = recall_mainline_references(["ag/40830952"], data_root=root)
    assert result["sources"]["security_patches"]["status"] == "matched"


def test_mixed_identifiers_do_not_turn_exact_cve_into_prefix_match(references):
    root, _store, _snapshot = references
    result = recall_mainline_references(["CVE-2026-5883", "ag/99999999"], data_root=root)
    assert result["sources"]["security_patches"]["status"] == "no_match"


def test_no_match_and_unavailable_are_distinct(references):
    root, _store, _snapshot = references
    result = recall_mainline_references(["CVE-2026-99999"], data_root=root)
    assert result["sources"]["security_patches"]["status"] == "no_match"
    missing = recall_mainline_references(["CVE-2026-58835"], data_root=root / "missing")
    assert missing["sources"]["security_patches"]["status"] == "unavailable"
    assert not (root / "missing").exists()


def test_cve_next_to_chinese_text_is_not_lost():
    assert extract_reference_identifiers("处理CVE-2026-58835的补丁") == ["CVE-2026-58835"]
    assert extract_reference_identifiers("合入ag/40830952后解决A-568181732问题，CtsBluetoothTestCases失败") == [
        "ag/40830952", "A-568181732", "CtsBluetoothTestCases",
    ]


def test_source_path_escape_is_not_read(references, tmp_path):
    _root, store, snapshot = references
    artifact = add_text(store, snapshot, "CVE-2026-58835")
    outside = tmp_path / "outside.txt"
    outside.write_text("CVE-2026-99999")
    store.update_artifact(artifact["artifact_id"], derived_text_path=str(outside))
    result = reference_recall.collect_snapshot_references("owner-a", snapshot)
    assert result["coverage_complete"] is False
    assert "CVE-2026-99999" not in result["identifiers"]


def test_reference_api_enforces_both_scopes_and_owner(references):
    _root, store, snapshot = references
    add_text(store, snapshot, "CVE-2026-58835")
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        owner = request.headers.get("x-owner", "owner-a")
        request.state.current_user = CurrentUser(
            id="agent:fixture", username="agent", role="agent_service", resource_owner_id=owner,
            extra_permissions=frozenset(request.headers.get("x-scopes", "").split(",")),
        )
        return await call_next(request)

    app.include_router(evidence_api.router)
    with TestClient(app) as client:
        path = f"/api/redmine-agent/evidence/{snapshot}/references"
        assert client.get(path).status_code == 403
        assert client.get(path, headers={"x-scopes": "redmine.read"}).status_code == 403
        headers = {"x-scopes": "redmine.read,artifacts.read_own"}
        response = client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        assert len(response.json()["data"]["sources"]["security_patches"]["items"]) == 5
        assert client.get(path, headers={**headers, "x-owner": "owner-b"}).status_code == 404


async def test_controller_baseline_reaches_prompt_and_cannot_be_omitted(references):
    _root, store, snapshot = references
    add_text(store, snapshot, "CVE-2026-58835")
    entry = {"issue_id": 661620, "subject": "BTS warning", "analysis_mode": "diagnostic"}
    preflight = EvidencePreflight(snapshot_id=snapshot)
    await reference_recall.append_reference_preflight("owner-a", entry, preflight, Mock())
    prompt = KkAgentRedmineAnalyzer().build_prompt(entry)
    assert "PRELOAD" in prompt and "ag/40830952" in prompt
    assert "untrusted evidence DATA" in prompt
    trace = KkAgentTrace(tool_calls=[
        ToolTrace(tool_name="gms_rt_redmine_issue_fetch", status="succeeded",
                  tool_input={"issue_id": 661620}, snapshot_ids=[snapshot]), *preflight.traces,
    ])
    gate = evaluate_evidence_gate(trace, entry)
    assert gate["official_reference_attempted"]
    assert trace.source_evidence_tool_count == 0
    errors = gate_errors(gate, {"detailed_report": "缺少官方修复资料。"})
    assert any("were omitted" in error for error in errors)
    citations = " ".join(gate["official_reference_refs"])
    assert not any("were omitted" in error for error in gate_errors(gate, {"detailed_report": "官方版本说明 " + citations}))
    assert trace.evidence_ledger()[-1]["kind"] == "reference"


def test_model_claim_cannot_fake_reference_lookup():
    trace = KkAgentTrace()
    gate = evaluate_evidence_gate(trace, {"subject": "CVE-2026-58835", "issue_id": 661620})
    assert any("were not checked" in error for error in gate_errors(gate, {"official_reference_attempted": True}))


def test_reference_trace_survives_native_tool_results(references):
    _root, store, snapshot = references
    add_text(store, snapshot, "CVE-2026-58835")
    data = reference_recall.collect_snapshot_references("owner-a", snapshot)
    trace = KkAgentTrace()
    consume_line(trace, json.dumps({"type": "tool_call", "tool_call_id": "r1",
                                   "tool_name": "gms_rt_redmine_references", "input": {"snapshot_id": snapshot}}))
    consume_line(trace, json.dumps({"type": "tool_result", "tool_call_id": "r1", "is_error": False,
                                   "output": json.dumps({"success": True, "data": data})}))
    assert trace.tool_calls[0].reference_lookup["sources"]["security_patches"]["count"] == 5


async def test_recovered_issue_fetch_collects_official_references_once_without_renumbering(references):
    _root, store, snapshot = references
    add_text(store, snapshot, 'CVE-2026-58835')
    entry = {'issue_id': 661620, '_reference_owner_id': 'owner-a', '_reference_lookup_required': True}
    trace = KkAgentTrace(tool_calls=[
        ToolTrace(tool_name='gms_rt_redmine_issue_fetch', status='failed', tool_input={'issue_id': 661620}),
        ToolTrace(tool_name='gms_rt_redmine_issue_fetch', status='succeeded',
                  tool_input={'issue': '661828'}, snapshot_ids=['peer-snapshot']),
        ToolTrace(tool_name='gms_rt_redmine_issue_fetch', status='succeeded',
                  tool_input={'issue': '661620'}, snapshot_ids=[snapshot]),
    ])
    original = trace.evidence_ledger()
    await reference_recall.recover_reference_preflight(entry, trace)
    assert trace.evidence_ledger()[:len(original)] == original
    assert trace.evidence_ledger()[-1]['evidence_id'] == 'EV-004'
    assert len(entry['_official_reference_context']['sources']['security_patches']['items']) == 5
    await reference_recall.recover_reference_preflight(entry, trace)
    assert len(trace.tool_calls) == 4
    gate = evaluate_evidence_gate(trace, entry)
    assert gate['official_reference_attempted']


async def test_recovery_keeps_owner_boundary(references):
    _root, _store, snapshot = references
    entry = {'issue_id': 661620, '_reference_owner_id': 'owner-b'}
    trace = KkAgentTrace(tool_calls=[ToolTrace(
        tool_name='gms_rt_redmine_issue_fetch', status='succeeded',
        tool_input={'issue_id': 661620}, snapshot_ids=[snapshot],
    )])
    await reference_recall.recover_reference_preflight(entry, trace)
    assert entry['_official_reference_context']['status'] == 'unavailable'
    assert trace.tool_calls[-1].status == 'failed'
    assert not trace.tool_calls[-1].reference_lookup


async def test_successful_native_reference_lookup_is_not_repeated(references, monkeypatch):
    _root, store, snapshot = references
    add_text(store, snapshot, 'CVE-2026-58835')
    data = reference_recall.collect_snapshot_references('owner-a', snapshot)
    trace = KkAgentTrace(tool_calls=[
        ToolTrace(tool_name='gms_rt_redmine_issue_fetch', status='succeeded',
                  tool_input={'issue_id': 661620}, snapshot_ids=[snapshot]),
        ToolTrace(tool_name='gms_rt_redmine_references', status='succeeded',
                  tool_input={'snapshot_id': snapshot}, reference_lookup=reference_recall.reference_check_summary(data)),
    ])
    collector = Mock(side_effect=AssertionError('lookup must not repeat'))
    monkeypatch.setattr(reference_recall, 'collect_snapshot_references', collector)
    await reference_recall.recover_reference_preflight({'issue_id': 661620, '_reference_owner_id': 'owner-a'}, trace)
    collector.assert_not_called()
    assert len(trace.tool_calls) == 2


async def test_failed_preflight_still_requires_official_lookup_after_recovery(references, monkeypatch):
    from features.redmine.daily_brief import service as daily_brief_service

    evidence = EvidencePreflight()
    monkeypatch.setattr(daily_brief_service, 'collect_deep_analysis_evidence', AsyncMock(return_value=evidence))
    entry = {'issue_id': 661620}
    await daily_brief_service.precollect_deep_evidence(
        repository=SimpleNamespace(is_cancel_requested=lambda _run: False),
        run=SimpleNamespace(run_id='run', owner_id='owner-a'), issue_id=661620,
        analyzer=SimpleNamespace(env_extra={'GMS_RT_PROFILE': 'explicit-owner-profile'}), entry=entry,
    )
    assert entry['_reference_lookup_required'] is True
    assert entry['_reference_owner_id'] == 'owner-a'


async def test_analyzer_recovers_official_context_before_repair_and_review(references, monkeypatch):
    from features.redmine.analysis.kkagent.analyzer import _StreamFallback
    from features.redmine.analysis.kkagent.preflight import McpHealthProbe
    from features.redmine.tests.analysis.kkagent.review_support import review_trace
    from features.redmine.tests.analysis.kkagent.test_native_summary import complete_trace, final_trace

    _root, store, snapshot = references
    add_text(store, snapshot, 'CVE-2026-58835')
    initial = complete_trace(report='尚未引用官方记录。')
    initial.tool_calls[0].tool_input = {'issue_id': 661620}
    initial.tool_calls[0].snapshot_ids = [snapshot]
    initial.tool_calls[1].tool_input = {'snapshot_id': snapshot}
    report = '官方资料列出修复，设备是否集成仍待验证（EV-006）。'
    stream = AsyncMock(side_effect=[
        (initial, _StreamFallback(), False),
        (final_trace(report=report), _StreamFallback(), False),
        (review_trace(report, refs=['EV-006']), _StreamFallback(), False),
    ])
    analyzer = KkAgentRedmineAnalyzer()
    monkeypatch.setattr(analyzer, '_run_stream', stream)
    monkeypatch.setattr('features.redmine.analysis.kkagent.analyzer.probe_kkagent_mcp_health',
                        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)))
    outcome = await analyzer.analyze({
        'issue_id': 661620, 'subject': 'BTS warning', 'analysis_mode': 'diagnostic', 'output_style': 'summary',
        '_reference_lookup_required': True, '_reference_owner_id': 'owner-a',
    })
    assert outcome.ok
    assert 'ag/40830952' in stream.await_args_list[1].args[0][-1]
    assert len([tool for tool in outcome.trace['tools'] if 'redmine_references' in tool['tool_name']]) == 1





def test_public_policy_version_uses_retained_build_properties(references, monkeypatch):
    _root, store, snapshot = references
    add_text(store, snapshot, 'ro.build.version.sdk=36\nCtsBluetoothTestCases')
    web_recall = Mock(return_value={'status': 'matched', 'items': [], 'coverage_complete': False})
    monkeypatch.setattr(reference_recall, 'recall_web_references', web_recall)
    data = reference_recall.collect_snapshot_references('owner-a', snapshot)
    assert web_recall.call_args.kwargs['android_version'] == 16
    assert data['sources']['web_documents']['target_android_version'] == 16
    assert data['sources']['web_documents']['version_resolution'] == 'matched'


def test_snapshot_recall_keeps_policy_import_query_internal(references):
    _root, _store, snapshot = references
    public = reference_recall.collect_snapshot_references('owner-a', snapshot)
    assert '_policy_query' not in public
    assert 'owner_policy_imports' not in public['sources']
    assert 'gms_requirements' not in public['sources']
    internal = reference_recall.collect_snapshot_references('owner-a', snapshot, include_policy_query=True)
    assert internal.pop('_policy_query').strip()
    assert internal['sources'] == public['sources']


def test_policy_owner_refuses_ambiguous_redmine_directory(monkeypatch):
    users = [
        {'id': 'owner.a', 'disabled': False},
        {'id': 'owner_a', 'disabled': False},
    ]
    monkeypatch.setattr(reference_recall.auth_service, 'list_users', lambda: users)
    assert reference_recall._policy_owner('owner_a') is None
    users.pop()
    assert reference_recall._policy_owner('owner_a') == 'owner.a'


def test_official_reference_revision_and_shared_evidence_trace(references):
    from features.redmine.analysis.kkagent.trace import trace_from_summary

    root, store, snapshot = references
    add_text(store, snapshot, "CVE-2026-58835")
    first = recall_mainline_references(["CVE-2026-58835"], data_root=root)
    second = recall_mainline_references(["ag/40830952"], data_root=root)
    by_reference = {row["reference"]: row["source_revision"]
                    for row in first["sources"]["security_patches"]["items"]}
    assert by_reference
    assert by_reference == {row["reference"]: row["source_revision"]
                            for row in second["sources"]["security_patches"]["items"]}
    data = reference_recall.collect_snapshot_references("owner-a", snapshot)
    trace = KkAgentTrace()
    consume_line(trace, json.dumps({"type": "tool_call", "tool_call_id": "r1",
                                   "tool_name": "gms_rt_redmine_references",
                                   "input": {"snapshot_id": snapshot}}))
    consume_line(trace, json.dumps({"type": "tool_result", "tool_call_id": "r1",
                                   "is_error": False,
                                   "output": json.dumps({"success": True, "data": {"data": data}})}))
    evidence = trace.tool_calls[0].retrieval_evidence
    assert evidence["references"]
    assert not evidence["coverage_complete"]
    assert trace_from_summary(trace.to_summary()).tool_calls[0].retrieval_evidence == evidence
