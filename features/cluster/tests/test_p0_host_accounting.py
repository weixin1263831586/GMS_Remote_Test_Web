from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.cluster import api as cluster_api
from features.cluster.repository import ClusterRepository
from features.cluster.service import ClusterService
from worker_agent.process_inventory import (
    _collect_adb_descendant_argv,
    _extract_devices,
    _is_tradefed,
)


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


def test_device_extraction_does_not_treat_relative_frida_script_as_serial():
    argv = [
        "/suite/tools/cts-tradefed",
        "run",
        "cts",
        "-s",
        "agent.js",
        "--serial",
        "192.0.2.10:5555",
    ]

    assert _extract_devices(argv) == {"192.0.2.10:5555"}


def test_interactive_console_detected_via_adb_descendant():
    """An interactive Tradefed Console (user typed ``run vts`` at the prompt)
    runs invocations in-process — no child JVM with ``CompatibilityConsole
    run`` or ``tf_runtime_info`` on its command line.  A live ``adb`` child
    must therefore serve as the signal for active device interaction.
    """
    processes = {
        100: {"pid": 100, "ppid": 1, "argv": ["./vts-tradefed"],
              "comm": "vts-tradefed"},
        101: {"pid": 101, "ppid": 100,
              "argv": ["java", "-cp", "tradefed.jar",
                       "com.android.compatibility.common.tradefed.command.CompatibilityConsole"],
              "comm": "java"},
        102: {"pid": 102, "ppid": 101,
              "argv": ["adb", "-s", "INTERACTIVE-SERIAL", "shell", "some-command"],
              "comm": "adb"},
    }
    group_pids = {100, 101}
    adb_argv = _collect_adb_descendant_argv(processes, group_pids)
    assert adb_argv, "adb descendant should be detected"
    assert "INTERACTIVE-SERIAL" in _extract_devices(adb_argv)


def test_idle_console_without_adb_descendant_is_skipped():
    """An idle Tradefed Console with no adb children must not be reported."""
    processes = {
        100: {"pid": 100, "ppid": 1, "argv": ["./vts-tradefed"],
              "comm": "vts-tradefed"},
        101: {"pid": 101, "ppid": 100,
              "argv": ["java", "-cp", "tradefed.jar",
                       "com.android.compatibility.common.tradefed.command.CompatibilityConsole"],
              "comm": "java"},
    }
    group_pids = {100, 101}
    assert _collect_adb_descendant_argv(processes, group_pids) == []


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
        with patch.dict(
            "os.environ", {"GMS_WORKER_TOKENS_FILE": str(tokens_path)}
        ):
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


def test_worker_tests_visible_to_non_admin(tmp_path):
    """External tests have no owner and must remain visible to non-admin users.

    Regression: previously the ownership filter dropped every external test
    because their ``job_id`` is empty, so the cluster UI showed
    "检测到 N 个外部测试，详情暂不可用" even though the details were collected.
    """
    repository = _registered_repository(tmp_path)
    repository.heartbeat("worker-1", {
        "running_jobs": [
            {"worker_job_id": "external-123", "job_id": "", "attempt_id": "",
             "status": "running", "pid": 123, "devices": ["ABC"],
             "source": "external", "suite_type": "CTS"},
            {"worker_job_id": "managed-own", "job_id": "job-own", "attempt_id": "",
             "status": "running", "pid": 456, "devices": ["FREE"],
             "source": "managed", "suite_type": "GTS"},
            {"worker_job_id": "managed-other", "job_id": "job-other", "attempt_id": "",
             "status": "running", "pid": 789, "devices": ["BUSY"],
             "source": "managed", "suite_type": "VTS"},
        ],
        "devices": [{"serial": "ABC", "state": "available"},
                    {"serial": "FREE", "state": "available"},
                    {"serial": "BUSY", "state": "available"}],
    })
    # Attach owners to the managed jobs so the ownership filter can run.
    repository.get_job = lambda job_id: (
        {"owner_id": "tester"} if job_id == "job-own"
        else {"owner_id": "someone-else"} if job_id == "job-other"
        else None
    )

    previous = cluster_api.cluster_service
    cluster_api.cluster_service = ClusterService(repository)
    app = FastAPI()
    app.include_router(cluster_api.router)
    client = TestClient(app)
    try:
        non_admin = CurrentUser(id="tester", username="tester", role="user")
        with patch("features.cluster.api.get_authenticated_user",
                   return_value=non_admin), \
             patch("features.cluster.api.authentication_required",
                   return_value=False):
            response = client.get("/api/cluster/worker-tests")
        assert response.status_code == 200, response.text
        tests = response.json()["tests"]
        sources = {item["worker_job_id"]: item["source"] for item in tests}
        # External test is visible despite having no owner.
        assert "external-123" in sources
        # Managed test owned by this user is visible.
        assert "managed-own" in sources
        # Managed test owned by someone else is filtered out.
        assert "managed-other" not in sources
    finally:
        client.close()
        cluster_api.cluster_service = previous
