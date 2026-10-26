"""End-to-end build execution tests: local backend, concurrency, passwords."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from features.build.executor import BuildExecutionError
from features.build.repository import BuildStore
from features.build.service import BuildNotFoundError, BuildService


def test_local_build_job_completes_and_discovers_artifact(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = tmp_path / "build_servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "id": "local",
                        "backend": "local",
                        "workspace_root": str(tmp_path),
                    }
                ],
                "templates": [
                    {
                        "id": "demo",
                        "server_id": "local",
                        "workspace": str(workspace),
                        "command": "mkdir -p out && echo firmware > out/update.img && echo done",
                        "timeout_sec": 30,
                        "artifact_patterns": ["out/*.img"],
                        "parameters_schema": {},
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    service = BuildService(
        store=BuildStore(tmp_path / "build.sqlite3"),
        config_path=config_path,
    )

    job = service.create_job({"server_id": "local", "template_id": "demo"})
    for _ in range(20):
        job = service.poll_job(job["id"])
        if job["status"] != "running":
            break
        time.sleep(0.2)

    assert job["status"] == "completed"
    assert job["artifacts"][0]["path"].endswith("out/update.img")
    assert "done" in service.tail_log(job["id"], lines=20)


def test_separate_workers_start_a_queued_job_only_once(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    config_path = tmp_path / 'build_servers.json'
    config_path.write_text(
        json.dumps({
            'servers': [{
                'id': 'local',
                'backend': 'local',
                'workspace_root': str(tmp_path),
            }],
            'templates': [{
                'id': 'demo',
                'server_id': 'local',
                'workspace': str(workspace),
                'command': 'true',
                'parameters_schema': {},
                'enabled': True,
            }],
        }),
        encoding='utf-8',
    )
    db_path = tmp_path / 'build.sqlite3'
    creator = BuildService(store=BuildStore(db_path), config_path=config_path)
    job = creator.create_job(
        {'server_id': 'local', 'template_id': 'demo'},
        start=False,
    )

    class CountingBackend:
        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()

        def start(self, **_kwargs):
            with self.lock:
                self.calls += 1
            return {'session': 'session', 'log_path': '/tmp/build.log'}

    backend = CountingBackend()
    services = [
        BuildService(store=BuildStore(db_path), config_path=config_path)
        for _ in range(2)
    ]
    for service in services:
        service.backends['local'] = backend
    barrier = threading.Barrier(2)

    def start(service):
        barrier.wait()
        service.start_job(job['id'])

    threads = [threading.Thread(target=start, args=(service,)) for service in services]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert backend.calls == 1
    assert creator.get_job(job['id'])['status'] == 'running'


def test_concurrent_workers_respect_server_capacity(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    config_path = tmp_path / 'build_servers.json'
    config_path.write_text(
        json.dumps({
            'servers': [{
                'id': 'local', 'backend': 'local',
                'workspace_root': str(tmp_path), 'max_concurrent_jobs': 1,
            }],
            'templates': [{
                'id': 'demo', 'server_id': 'local',
                'workspace': str(workspace), 'command': 'true',
                'parameters_schema': {}, 'enabled': True,
            }],
        }),
        encoding='utf-8',
    )
    db_path = tmp_path / 'build.sqlite3'
    creator = BuildService(store=BuildStore(db_path), config_path=config_path)
    jobs = [
        creator.create_job({'server_id': 'local', 'template_id': 'demo'}, start=False)
        for _ in range(2)
    ]

    class CountingBackend:
        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()

        def start(self, **kwargs):
            with self.lock:
                self.calls += 1
            return {
                'session': kwargs['job_id'],
                'log_path': f"/tmp/{kwargs['job_id']}.log",
            }

    backend = CountingBackend()
    services = [
        BuildService(store=BuildStore(db_path), config_path=config_path)
        for _ in range(2)
    ]
    for service in services:
        service.backends['local'] = backend
    barrier = threading.Barrier(2)

    def start(index):
        barrier.wait()
        services[index].start_job(jobs[index]['id'])

    threads = [threading.Thread(target=start, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    statuses = [creator.get_job(job['id'])['status'] for job in jobs]
    assert backend.calls == 1
    assert sorted(statuses) == ['queued', 'running']


def test_runtime_password_is_removed_when_start_fails(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    config_path = tmp_path / 'build_servers.json'
    config_path.write_text(
        json.dumps({
            'servers': [{
                'id': 'local', 'backend': 'local',
                'workspace_root': str(tmp_path),
            }],
            'templates': [{
                'id': 'demo', 'server_id': 'local',
                'workspace': str(workspace), 'command': 'true',
                'parameters_schema': {}, 'enabled': True,
            }],
        }),
        encoding='utf-8',
    )
    service = BuildService(
        store=BuildStore(tmp_path / 'build.sqlite3'),
        config_path=config_path,
    )

    class FailingBackend:
        @staticmethod
        def start(**_kwargs):
            raise RuntimeError('cannot connect')

    service.backends['local'] = FailingBackend()
    job = service.create_job({
        'server_id': 'local',
        'template_id': 'demo',
        'server_password': 'secret',
    })

    assert job['status'] == 'failed'
    assert service._runtime_passwords.get(job['id']) == ""


def test_runtime_password_persists_encrypted_with_ttl(tmp_path: Path, monkeypatch):
    """runtime password 加密落盘 + TTL，跨进程/重启可 poll。

    进程内 dict 会因多 Uvicorn worker 或 Controller 重启而丢失，使密码
    认证的远端 tmux 构建无法再 poll/cancel。改为加密落盘（明文不落库）
    并带硬 TTL；此处验证跨实例可读、明文不落盘、pop 后清除。
    """
    from features.build.runtime_passwords import RuntimePasswordStore

    store_path = tmp_path / "runtime_passwords.json"
    first = RuntimePasswordStore(store_path)
    first.set("job-1", "s3cr3t-password")

    raw = store_path.read_text(encoding="utf-8")
    assert "s3cr3t-password" not in raw  # 明文绝不落盘
    assert oct(store_path.stat().st_mode)[-3:] == "600"

    # 另一个进程/实例（独立对象）可读到同一密码。
    second = RuntimePasswordStore(store_path)
    assert second.get("job-1") == "s3cr3t-password"

    # 过期即不可读（无解密尝试）。
    expired = RuntimePasswordStore(store_path)
    with monkeypatch.context() as clock:
        clock.setattr("features.build.runtime_passwords.time.time", lambda: 10**12)
        assert expired.get("job-1") == ""

    second.pop("job-1")
    assert RuntimePasswordStore(store_path).get("job-1") == ""


def _completed_local_job(tmp_path: Path, **request_extra) -> tuple[BuildService, dict]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    config_path = tmp_path / "build_servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "id": "local",
                        "backend": "local",
                        "workspace_root": str(tmp_path),
                    }
                ],
                "templates": [
                    {
                        "id": "demo",
                        "server_id": "local",
                        "workspace": str(workspace),
                        "command": "mkdir -p out && echo firmware > out/update.img && echo done",
                        "timeout_sec": 30,
                        "artifact_patterns": ["out/*.img"],
                        "parameters_schema": {},
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    service = BuildService(
        store=BuildStore(tmp_path / "build.sqlite3"),
        config_path=config_path,
    )
    job = service.create_job(
        {"server_id": "local", "template_id": "demo", **request_extra}
    )
    for _ in range(20):
        job = service.poll_job(job["id"])
        if job["status"] not in ("running", "queued"):
            break
        time.sleep(0.2)
    assert job["status"] == "completed"
    assert job["artifacts"], "demo build must discover out/update.img"
    return service, job


def test_open_build_artifact_streams_completed_job_artifact(tmp_path: Path):
    service, job = _completed_local_job(tmp_path)
    artifact_path = job["artifacts"][0]["path"]

    with service.open_build_artifact(job["id"], artifact_path) as (stream, size):
        data = stream.read()

    assert data == b"firmware\n"
    assert size == len(data)


def test_open_build_artifact_enforces_completion_ownership_and_allowlist(tmp_path: Path):
    service, job = _completed_local_job(tmp_path, automation_run_id="ats_run_1")
    artifact_path = job["artifacts"][0]["path"]

    # Job owned by another automation run must not be readable.
    with pytest.raises(BuildExecutionError, match="another automation run"), service.open_build_artifact(
        job["id"], artifact_path, automation_run_id="ats_run_2"
    ):
        pass

    # Paths the job never reported as artifacts stay unreadable even when
    # the file exists on the build server.
    with pytest.raises(BuildExecutionError, match="not part of the completed build job"), service.open_build_artifact(
        job["id"], artifact_path + ".shadow", automation_run_id="ats_run_1"
    ):
        pass

    # Owning run and listed path stream fine.
    with service.open_build_artifact(
        job["id"], artifact_path, automation_run_id="ats_run_1"
    ) as (stream, size):
        assert stream.read() == b"firmware\n"
        assert size > 0

    # Incomplete jobs expose nothing.
    queued = service.create_job(
        {"server_id": "local", "template_id": "demo"}, start=False
    )
    with pytest.raises(BuildExecutionError, match="not completed"), service.open_build_artifact(
        queued["id"], artifact_path
    ):
        pass

    with pytest.raises(BuildNotFoundError), service.open_build_artifact("build_missing", artifact_path):
        pass
