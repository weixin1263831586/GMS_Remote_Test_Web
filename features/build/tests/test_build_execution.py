"""End-to-end build execution tests: local backend, concurrency, passwords."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from features.build.repository import BuildStore
from features.build.service import BuildService


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


def test_runtime_password_persists_encrypted_with_ttl(tmp_path: Path):
    """审核意见 P2：runtime password 加密落盘 + TTL，跨进程/重启可 poll。

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
    expired._load()
    blob, _ = expired._cache["job-1"]
    expired._cache["job-1"] = (blob, time.time() - 1)
    assert expired.get("job-1") == ""

    second.pop("job-1")
    assert RuntimePasswordStore(store_path).get("job-1") == ""
