"""Replay must exercise adapters and reject missing runtime evidence."""

import copy
import json
from pathlib import Path

import pytest

from tests.quality.diagnosis_corpus import grade_result, load_corpus
from tests.quality.diagnosis_replay import replay_fixture


def fixtures():
    return [json.loads(line) for line in Path(__file__).with_name(
        "diagnosis_replay_fixtures.jsonl").read_text().splitlines() if line.strip()]


def test_all_cases_have_one_replay_and_development_passes_projection():
    cases = {case["id"]: case for case in load_corpus()}
    rows = fixtures()
    assert len(rows) == len(cases)
    assert {row["case_id"] for row in rows} == set(cases)
    for row in rows:
        assert row["split"] == cases[row["case_id"]]["split"]
        assert row["purpose"] == cases[row["case_id"]]["purpose"]
        if row["split"] != "development":
            continue  # Holdout scoring runs only in the nightly/manual gate.
        report = grade_result(replay_fixture(row), cases[row["case_id"]])
        assert report.passed, report.failures


@pytest.mark.parametrize("source", ["history", "test_source"])
def test_missing_or_failed_tool_trace_cannot_be_claimed_by_ai(source):
    row = copy.deepcopy(fixtures()[0])
    row["native_result"]["history_checked"] = True
    for event in row["tool_trace"]:
        if event["source"] == source:
            event["ok"] = False
    case = next(case for case in load_corpus() if case["id"] == row["case_id"])
    assert not grade_result(replay_fixture(row), case).passed


def test_evidence_mapping_drift_fails_replay():
    row = copy.deepcopy(fixtures()[0])
    row["native_result"]["evidence"][0]["reference"] = ""
    with pytest.raises(ValueError, match="read-model drift"):
        replay_fixture(row)


def test_background_cannot_satisfy_test_source_evidence():
    row = copy.deepcopy(fixtures()[0])
    row["native_result"]["evidence"] = []
    row["read_model"]["evidence"] = []
    case = next(case for case in load_corpus() if case["id"] == row["case_id"])
    assert not grade_result(replay_fixture(row), case).passed
