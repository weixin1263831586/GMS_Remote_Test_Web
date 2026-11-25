"""Structural Markdown must not consume factual revision attempts."""

import json
from unittest.mock import AsyncMock

import pytest

from features.redmine.analysis.kkagent.analyzer import KkAgentRedmineAnalyzer, _StreamFallback
from features.redmine.analysis.kkagent.claim_review import report_units, validate_review
from features.redmine.analysis.kkagent.native_summary import native_summary_result
from features.redmine.analysis.kkagent.preflight import McpHealthProbe
from features.redmine.tests.analysis.kkagent.review_support import review_payload
from features.redmine.tests.analysis.kkagent.test_native_summary import ENTRY, complete_trace, final_trace


ENTRY_SUMMARY = {**ENTRY, 'output_style': 'summary'}
REPORT = ('## 机制与归因证据（本会话直接核验）\n\n'
          '已读取本次工单的证据，根因仍待验证。\n\n'
          '## 剩余不确定性 / 不可得证据\n\n'
          '实际构建属性尚待核实。')


def test_headings_and_separators_are_context_for_their_prose_without_losing_text():
    report = '# 工单分析\n\n' + REPORT + '\n\n---\n\n## 下一步\n\n读取实际构建属性。\n\n## 附录'
    units = report_units(report)
    assert len(units) == 3
    assert '\n\n'.join(unit['text'] for unit in units) == report
    assert units[0]['text'].startswith('# 工单分析\n\n## 机制')
    assert units[1]['text'].startswith('## 剩余不确定性')
    assert units[-1]['text'].endswith('## 附录')


def test_setext_headings_are_grouped_with_body_too():
    units = report_units('诊断结论\n=====\n\n证据待补充。')
    assert len(units) == 1 and units[0]['text'].endswith('证据待补充。')


@pytest.mark.asyncio
async def test_structural_headings_do_not_require_separate_content_evidence_or_a_rewrite(monkeypatch):
    analyzer = KkAgentRedmineAnalyzer()
    payload = review_payload(REPORT, assessment='supported')
    for receipt, unit in zip(payload['units'], report_units(REPORT), strict=True):
        if '\n' not in unit['text'] and unit['text'].startswith('#'):
            receipt.update(evidence_ids=[], reason='标题，无事实断言')
    stream = AsyncMock(side_effect=[
        (complete_trace(report=REPORT), _StreamFallback(), False),
        (final_trace(report=json.dumps(payload)), _StreamFallback(), False),
    ])
    monkeypatch.setattr(analyzer, '_run_stream', stream)
    monkeypatch.setattr('features.redmine.analysis.kkagent.analyzer.probe_kkagent_mcp_health',
                        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)))
    outcome = await analyzer.analyze(ENTRY_SUMMARY)
    assert outcome.ok and outcome.result['detailed_report'] == REPORT
    assert outcome.result['analysis_review']['version'] == 2
    assert len(outcome.result['claims']) == 2
    assert stream.await_count == 2


def test_assertions_in_headings_still_require_causal_evidence():
    report = '## 平台无责\n\n仅有一次 RSS 观测。'
    trace = complete_trace(report=report)
    result = native_summary_result(trace, ENTRY_SUMMARY)
    payload = review_payload(report, assessment='supported', category='responsibility')
    _review, errors = validate_review(payload, result, trace, ENTRY_SUMMARY)
    assert any('correlation cannot confirm' in error for error in errors)
    payload = review_payload(report, assessment='contradicted', category='responsibility')
    assert any('contradicts evidence' in error for error in validate_review(payload, result, trace, ENTRY_SUMMARY)[1])


@pytest.mark.parametrize('report', ['## 结论\n\n### 证据', '---'])
def test_a_report_with_only_structure_cannot_be_published(report):
    trace = complete_trace(report=report)
    result = native_summary_result(trace, ENTRY_SUMMARY)
    assert any('no substantive paragraphs' in error
               for error in validate_review(review_payload(report), result, trace, ENTRY_SUMMARY)[1])


def test_heading_like_code_is_not_exempt_from_evidence_requirements():
    report = '```text\n## 平台无责\n```'
    trace = complete_trace(report=report)
    result = native_summary_result(trace, ENTRY_SUMMARY)
    payload = review_payload(report, assessment='supported', refs=[])
    assert any('no content-bearing evidence' in error
               for error in validate_review(payload, result, trace, ENTRY_SUMMARY)[1])
