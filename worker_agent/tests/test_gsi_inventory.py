from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from worker_agent.inventory import browse_directory, copy_image_into, flash_gsi, scan_suites
from worker_agent.suite_detection import suite_details


def test_browse_directory_lists_entries_with_metadata(tmp_path: Path) -> None:
    (tmp_path / "images" / "nested").mkdir(parents=True)
    (tmp_path / "images" / "gsi.img").write_bytes(b"abc")

    result = browse_directory(str(tmp_path / "images"))

    assert result["path"] == str((tmp_path / "images").resolve())
    assert [(item["name"], item["type"]) for item in result["files"]] == [
        ("nested", "directory"),
        ("gsi.img", "file"),
    ]


def test_browse_directory_defaults_to_suite_root(tmp_path: Path) -> None:
    suite_root = tmp_path / "GMS-Suite"
    suite_root.mkdir()
    (suite_root / "android-cts").mkdir()

    result = browse_directory("", default_path=suite_root)

    assert result["path"] == str(suite_root.resolve())
    assert [item["name"] for item in result["files"]] == ["android-cts"]


def test_browse_directory_falls_back_to_home_without_default(tmp_path: Path) -> None:
    with patch("worker_agent.device_actions.Path.home", return_value=tmp_path):
        result = browse_directory("")

    assert result["path"] == str(tmp_path.resolve())


def test_copy_image_into_reports_size_and_sha256(tmp_path: Path) -> None:
    source = tmp_path / "a.img"
    source.write_bytes(b"12345")
    target = tmp_path / "staging" / "system.img"
    target.parent.mkdir(parents=True)

    staged = copy_image_into(source, target, 1024)

    assert staged["size_bytes"] == 5
    assert staged["sha256"] == hashlib.sha256(b"12345").hexdigest()
    assert target.read_bytes() == b"12345"


def test_copy_image_into_enforces_size_limit(tmp_path: Path) -> None:
    import pytest

    source = tmp_path / "big.img"
    source.write_bytes(b"0" * 16)
    with pytest.raises(ValueError, match="staging limit"):
        copy_image_into(source, tmp_path / "out.img", 8)


def test_suite_detection_covers_verifier_launcher() -> None:
    """CTS Verifier 的主机端启动器也应被识别为可部署套件。"""
    verifier = Path(
        "/suites/android-cts-verifier-17_r1/android-cts-verifier/"
        "android-cts-v-host/tools/cts-v-host-tradefed"
    )
    assert suite_details(verifier) == ("CTS_V", "17_r1")
    regular = Path("/suites/android-cts-17_r1/android-cts/tools/cts-tradefed")
    assert suite_details(regular) == ("CTS", "17_r1")


def test_scan_suites_reports_verifier_launcher(tmp_path: Path) -> None:
    tools = tmp_path / "android-cts-verifier-17_r1" / "android-cts-verifier" / "android-cts-v-host" / "tools"
    tools.mkdir(parents=True)
    launcher = tools / "cts-v-host-tradefed"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o755)
    config = SimpleNamespace(suite_roots=[tmp_path])
    suites = scan_suites(config)
    assert [(s["suite_type"], s["suite_version"], s["available"]) for s in suites] == [
        ("CTS_V", "17_r1", True)
    ]


def test_worker_gsi_uses_python_preparation_and_thin_script(tmp_path: Path) -> None:
    firmware_dir = tmp_path / "firmware"
    firmware_dir.mkdir()
    system_img = firmware_dir / "system.img"
    vendor_img = firmware_dir / "boot-debug.img"
    system_img.write_bytes(b"system")
    vendor_img.write_bytes(b"vendor")
    config = SimpleNamespace(data_root=tmp_path)
    prepared = SimpleNamespace(oem_argument=lambda action: "board:unlock")
    completed = subprocess.CompletedProcess(
        [],
        0,
        stdout="done",
        stderr="",
    )

    with (
        patch(
            "worker_agent.device_actions.probe_devices",
            return_value=[{"serial": "RK3572GMS1"}],
        ),
        patch("worker_agent.device_actions.FastbootPreparer") as preparer,
        patch(
            "worker_agent.device_actions.subprocess.run",
            return_value=completed,
        ) as run,
    ):
        preparer.return_value.prepare_gsi_fastbootd.return_value = prepared
        result = flash_gsi(
            config,
            system_img,
            vendor_img,
            ["RK3572GMS1"],
        )

    argv = run.call_args.args[0]
    assert argv[1:5] == [
        "RK3572GMS1",
        "board:unlock",
        str(system_img),
        str(Path("tools/misc.img").resolve()),
    ]
    assert argv[5:] == ["boot", str(vendor_img)]
    assert result["success"] is True


def test_worker_gsi_accepts_vendor_only_image(tmp_path: Path) -> None:
    firmware_dir = tmp_path / "firmware"
    firmware_dir.mkdir()
    vendor_img = firmware_dir / "vendor_boot.img"
    vendor_img.write_bytes(b"vendor")
    config = SimpleNamespace(data_root=tmp_path)
    prepared = SimpleNamespace(oem_argument=lambda action: "board:unlock")

    with (
        patch(
            "worker_agent.device_actions.probe_devices",
            return_value=[{"serial": "RK3572GMS1"}],
        ),
        patch("worker_agent.device_actions.FastbootPreparer") as preparer,
        patch(
            "worker_agent.device_actions.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout="done", stderr=""),
        ) as run,
    ):
        preparer.return_value.prepare_gsi_fastbootd.return_value = prepared
        result = flash_gsi(config, None, vendor_img, ["RK3572GMS1"])

    argv = run.call_args.args[0]
    assert argv[3] == ""
    assert argv[5:] == ["vendor_boot", str(vendor_img)]
    assert result["success"] is True
