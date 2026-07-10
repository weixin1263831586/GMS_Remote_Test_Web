import json
import time
from pathlib import Path

import pytest

from features.build.executor import BuildExecutionError, build_command_from_template
from features.build.repository import BuildStore
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
