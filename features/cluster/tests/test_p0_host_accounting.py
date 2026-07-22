from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.cluster import api as cluster_api
from features.cluster.repository import ClusterRepository
from features.cluster.service import ClusterService
from worker_agent.process_inventory import _extract_devices, _is_tradefed


def _registered_repository(tmp_path: Path) -> ClusterRepository:
    repository = ClusterRepository(tmp_path / "cluster.sqlite3")
    repository.register_worker({
        "worker_id": "worker-1", "name": "worker-1", "hostname": "host-1",
        "address": "127.0.0.1", "agent_version": "0.2.0", "max_jobs": 1,
        "capabilities": {},
    })
    return repository


def test_tradefed_detection_extracts_cli_and_runtime_devices(tmp_path):
    runtime_info = tmp_path / "tf_runtime_info"
    runtime_info.write_text(
        '{"invocations":[{"deviceIds":["RUNTIME-SERIAL"]}]}', encoding="utf-8"
    )
    argv = [
        "/suite/tools/cts-tradefed", "run", "cts", "-s", "CLI-SERIAL",
        f"-javaagent=x={runtime_info}:results",
    ]

    assert _is_tradefed(argv, "cts-tradefed")
    assert _extract_devices(argv) == {"CLI-SERIAL", "RUNTIME-SERIAL"}


def test_external_tradefed_marks_device_busy_and_counts_host(tmp_path):
    repository = _registered_repository(tmp_path)
    worker = repository.heartbeat("worker-1", {
        "running_jobs": [{
            "worker_job_id": "external-123", "job_id": "", "attempt_id": "",
            "status": "running", "pid": 123, "devices": ["ABC"],
            "source": "external", "suite_type": "CTS",
        }],
        "devices": [{"serial": "ABC", "state": "available"},
                    {"serial": "FREE", "state": "available"}],
    })

    assert worker["status"] == "busy"
    assert worker["running_jobs"] == 1
    assert worker["external_jobs"] == 1
    states = {item["serial"]: item["state"] for item in repository.list_devices()}
    assert states == {"ABC": "external_busy", "FREE": "available"}
    assert repository.list_worker_tests()[0]["source"] == "external"


def test_unknown_external_tradefed_drains_worker(tmp_path):
    repository = _registered_repository(tmp_path)
    worker = repository.heartbeat("worker-1", {
        "running_jobs": [{
            "worker_job_id": "external-unknown", "job_id": "", "attempt_id": "",
            "status": "running", "pid": 456, "devices": [], "source": "external",
            "warning": "device could not be identified",
        }],
        "devices": [{"serial": "ABC", "state": "available"}],
    })

    assert worker["status"] == "draining"
    try:
        repository.create_job_with_leases({"worker_id": "worker-1", "devices": ["ABC"]})
    except ValueError as exc:
        assert "not online" in str(exc)
    else:
        raise AssertionError("draining Worker accepted a new job")


def test_artifact_chunks_resume_and_complete_atomically(tmp_path):
    repository = _registered_repository(tmp_path)
    repository.heartbeat("worker-1", {
        "running_jobs": [], "devices": [{"serial": "ABC", "state": "available"}],
    })
    job = repository.create_job_with_leases({
        "worker_id": "worker-1", "devices": ["ABC"], "owner_id": "tester",
    })
    previous = cluster_api.cluster_service
    cluster_api.cluster_service = ClusterService(repository)
    app = FastAPI()
    app.include_router(cluster_api.router)
    client = TestClient(app)
    headers = {"Authorization": "Bearer token", "X-GMS-Worker-ID": "worker-1"}
    content = b"A" * 65536 + b"B" * 65536 + b"tail"
    digest = hashlib.sha256(content).hexdigest()
    try:
        tokens_path = tmp_path / "cluster.json"
        tokens_path.write_text(
            json.dumps({"worker_tokens": {"worker-1": "token"}}), encoding="utf-8"
        )
        with patch.dict("os.environ", {"GMS_CLUSTER_CONFIG": str(tokens_path)}):
            initialized = client.post(
                f"/api/cluster/jobs/{job['id']}/artifacts/uploads",
                headers=headers,
                json={"attempt_id": job["current_attempt_id"], "filename": "result.zip",
                      "artifact_type": "file", "size_bytes": len(content),
                      "sha256": digest, "chunk_size": 65536, "chunk_count": 3},
            )
            assert initialized.status_code == 200, initialized.text
            upload_id = initialized.json()["upload"]["id"]
            for index, block in ((0, content[:65536]), (2, content[131072:])):
                response = client.put(
                    f"/api/cluster/jobs/{job['id']}/artifacts/uploads/{upload_id}/chunks/{index}",
                    headers={**headers, "X-Chunk-SHA256": hashlib.sha256(block).hexdigest()},
                    content=block,
                )
                assert response.status_code == 200, response.text
            status = client.get(
                f"/api/cluster/jobs/{job['id']}/artifacts/uploads/{upload_id}",
                headers=headers,
            ).json()
            assert status["uploaded_chunks"] == [0, 2]
            middle = content[65536:131072]
            client.put(
                f"/api/cluster/jobs/{job['id']}/artifacts/uploads/{upload_id}/chunks/1",
                headers={**headers, "X-Chunk-SHA256": hashlib.sha256(middle).hexdigest()},
                content=middle,
            ).raise_for_status()
            completed = client.post(
                f"/api/cluster/jobs/{job['id']}/artifacts/uploads/{upload_id}/complete",
                headers=headers, json={"chunk_count": 3},
            )
            assert completed.status_code == 200, completed.text
    finally:
        client.close()
        cluster_api.cluster_service = previous

    artifact = repository.list_artifacts(job["id"])[0]
    stored = repository.db_path.parent / "artifacts" / artifact["relative_path"]
    assert stored.read_bytes() == content
    assert not (repository.db_path.parent / "artifact-uploads" / upload_id).exists()
