from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_CONTROL = ROOT / "web/static/js/pages/test-control.js"


def test_start_request_uses_frozen_worker_and_device_snapshot():
    source = TEST_CONTROL.read_text(encoding="utf-8")
    assert "const currentWorker = workspaceWorkerId();" in source
    assert "const requestedDevices = Array.from(state.selectedDevices);" in source
    assert "worker_id: currentWorker" in source
    assert "devices: requestedDevices" in source
    assert "worker_id: currentWorker," in source
    assert "device_ids: requestedDevices," in source
    assert "clearWorkerLogs(currentWorker);" in source
    assert "addWorkerLog(currentWorker, '测试已启动', 'success');" in source


def test_worker_selector_is_frozen_only_while_start_request_is_in_flight():
    source = TEST_CONTROL.read_text(encoding="utf-8")
    assert (
        "workerSelect.disabled = state.testStarting || !clusterEnabled || !workersLoaded;"
        in source
    )
    # RUNNING is deliberately absent from this expression: operators may browse
    # another Worker while the durable cluster job continues by clusterJobId.
    assert "state.testStarting || state.testing || !clusterEnabled" not in source
