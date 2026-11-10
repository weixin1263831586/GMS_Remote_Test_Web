"""Downloads stay visible and cannot outlive cancellation or a scheduler claim."""

import json
from types import SimpleNamespace

import pytest

from features.automation.artifact_progress import artifact_download_progress, artifact_progress_callback
from features.automation.executor_contract import StubAutomationExecutor
from features.automation.models import AutomationRunCreateRequest
from features.automation.orchestrator import AutomationOrchestrator
from features.automation.repository import AutomationStore
from features.automation.telemetry import run_progress


@pytest.fixture
def download_run(tmp_path):
    store = AutomationStore(tmp_path / "automation.sqlite3")
    request = AutomationRunCreateRequest(test_plan={"flash": {"firmware_share_id": "share-1"}})
    store.create_run(request.to_run_dict("ats_download"))
    orchestrator = AutomationOrchestrator(store, StubAutomationExecutor())
    assert orchestrator.advance_run("ats_download")["status"] == "artifact_fetching"
    return store, orchestrator


def counters(done):
    return {"downloaded_bytes": done, "total_bytes": 100, "bytes_per_second": 10, "elapsed_seconds": done / 10}


def test_download_stage_and_final_counters_survive_transition_and_list_projection(download_run):
    store, orchestrator = download_run

    def resolve(run, *, on_progress):
        assert store.get_run(run["id"])["current_stage"] == "artifact_fetching"
        on_progress(counters(0))
        on_progress(counters(100))
        live = run_progress(store.get_run(run["id"]), SimpleNamespace())
        assert live["available"] and live["artifact_download"]["percent"] == 100
        return {"success": True, "artifact_path": "/staged/update.img", "firmware_sha256": "digest"}

    orchestrator.executor.resolve_firmware_share = resolve
    result = orchestrator.advance_run("ats_download")
    assert result["status"] == "waiting_device"
    assert artifact_download_progress(result)["downloaded_bytes"] == 100
    assert json.loads(result["result_json"])["firmware_sha256"] == "digest"
    item = store.list_run_page()["items"][0]
    assert item["artifact_download"]["percent"] == 100
    assert "result_json" not in item and "artifact_progress_json" not in item


def test_callback_throttles_intermediate_writes_but_flushes_completion(download_run, monkeypatch):
    store, orchestrator = download_run
    store.claim_run("ats_download", orchestrator.claim_owner)
    clock = iter([0, 0.1, 2.1, 2.2])
    monkeypatch.setattr("features.automation.artifact_progress.time.monotonic", lambda: next(clock))
    report = artifact_progress_callback(store, "ats_download", orchestrator.claim_owner)
    report(counters(0))
    report(counters(10))
    assert artifact_download_progress(store.get_run("ats_download"))["downloaded_bytes"] == 0
    report(counters(20))
    assert artifact_download_progress(store.get_run("ats_download"))["downloaded_bytes"] == 20
    report(counters(100))
    assert artifact_download_progress(store.get_run("ats_download"))["percent"] == 100


@pytest.mark.parametrize("lose_claim", [False, True])
def test_cancel_or_claim_loss_stops_progress_without_overwriting_new_state(download_run, lose_claim):
    store, orchestrator = download_run

    def resolve(run, *, on_progress):
        on_progress(counters(0))
        if lose_claim:
            store.release_claim(run["id"], orchestrator.claim_owner)
            assert store.claim_run(run["id"], "other-scheduler")
        else:
            store.update_run_if_status(run["id"], "artifact_fetching", status="cancelled")
        with pytest.raises(RuntimeError, match="cancelled or claim lost"):
            on_progress(counters(100))
        return {"success": False, "error": "cancelled or claim lost"}

    orchestrator.executor.resolve_firmware_share = resolve
    result = orchestrator.advance_run("ats_download")
    assert result["status"] == ("artifact_fetching" if lose_claim else "cancelled")
    assert artifact_download_progress(result)["downloaded_bytes"] == 0


def test_progress_cas_rejects_a_stale_claim(download_run):
    store, _ = download_run
    assert store.claim_run("ats_download", "new-scheduler")
    _, applied = store.update_run_if_status(
        "ats_download", "artifact_fetching", expected_lease_owner="old-scheduler", result_json='{"stale":true}',
    )
    assert applied is False
    assert "stale" not in store.get_run("ats_download")["result_json"]


@pytest.mark.parametrize("value", [None, [], {"total_bytes": -1}, {
    "total_bytes": 100, "downloaded_bytes": 50, "bytes_per_second": float("nan"),
}])
def test_malformed_progress_is_unavailable(value):
    assert artifact_download_progress({"result_json": json.dumps({"artifact_download": value})}) == {}
