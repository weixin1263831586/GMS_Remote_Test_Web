import json
import threading
import time
from pathlib import Path

import pytest

from features.build.executor import BuildExecutionError, build_command_from_template
from features.build.repository import JOB_COLUMNS, BuildStore
from features.build.service import BuildService


def test_build_command_renders_workspace_init_and_command():
    server = {"workspace_root": "/home/hcq"}
    template = {
        "workspace": "{workspace}",
        "init_commands": ["source build/envsetup.sh", "lunch {lunch_target}"],
        "command": "{build_command}",
        "parameters_schema": {
            "workspace": {"required": True},
            "lunch_target": {"required": True},
            "build_command": {"default": "./build.sh -J 8"},
        },
    }

    prepared = build_command_from_template(
        template,
        server,
        {
            "workspace": "/home/hcq/rk/android",
            "lunch_target": "rk3566_rgo-userdebug",
        },
    )

    assert prepared.workspace == "/home/hcq/rk/android"
    assert prepared.init_commands[-1] == "lunch rk3566_rgo-userdebug"
    assert prepared.command == "./build.sh -J 8"


def test_build_command_rejects_workspace_escape():
    with pytest.raises(BuildExecutionError):
        build_command_from_template(
            {
                "workspace": "/tmp/other",
                "command": "true",
                "parameters_schema": {},
            },
            {"workspace_root": "/home/hcq"},
            {},
        )


def test_build_command_rejects_parent_segment_workspace_escape():
    for workspace in ('/srv/build/../secrets', '../secrets'):
        with pytest.raises(BuildExecutionError, match='workspace escapes'):
            build_command_from_template(
                {
                    'workspace': workspace,
                    'command': 'true',
                },
                {'workspace_root': '/srv/build'},
                {},
            )


def test_parse_lunch_options_from_rkbuild_output():
    output = """
You're building on Linux
Lunch menu... pick a combo:
  1. rk3566_rgo-userdebug
  2) rk3588-userdebug
  - rk3576_s-user
Which would you like? [Default 1]
TARGET_PRODUCT=rk3566_rgo TARGET_BUILD_VARIANT=userdebug
-------------------------------------------
rk3326-evb-lp3-v11-avb
"""

    assert BuildService._parse_lunch_options(output) == [
        "rk3566_rgo-userdebug",
        "rk3588-userdebug",
        "rk3576_s-user",
    ]


def test_parse_scoped_lunch_options_ignores_shell_noise():
    output = """profile noise rk3326-userdebug
__GMS_LUNCH_BEGIN__
pk30_u-bp2a-user
pk30_u-bp2a-userdebug
__GMS_LUNCH_END__
post noise rk3588-userdebug
"""
    assert BuildService._parse_scoped_lunch_options(output) == [
        "pk30_u-bp2a-user",
        "pk30_u-bp2a-userdebug",
    ]


def test_delete_build_history_only_allows_terminal_jobs(tmp_path: Path):
    store = BuildStore(tmp_path / "build.sqlite3")
    config_path = tmp_path / "build_servers.json"
    config_path.write_text('{"servers": [], "templates": []}', encoding="utf-8")
    service = BuildService(store=store, config_path=config_path)
    base = {column: "" for column in JOB_COLUMNS}
    base.update({"id": "done", "server_id": "s", "template_id": "t", "status": "completed"})
    store.create_job(base)
    running = dict(base, id="running", status="running")
    store.create_job(running)

    service.delete_job("done")
    assert store.get_job("done") is None
    with pytest.raises(BuildExecutionError, match="只能删除"):
        service.delete_job("running")


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
    assert job['id'] not in service._runtime_passwords
