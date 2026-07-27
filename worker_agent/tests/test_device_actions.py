from __future__ import annotations

import io
import subprocess
import zipfile
from unittest.mock import patch

from worker_agent.android_inspection import _aapt2_path
from worker_agent.config import WorkerConfig
from worker_agent.inventory import (
    execute_device_action,
    execute_suite_action,
    execute_usbip_action,
    flash_firmware,
    prepare_suite_export,
)
from worker_agent.process_inventory import _is_active_invocation


def test_aapt2_path_accepts_configured_worker_binary(tmp_path):
    binary = tmp_path / "aapt2"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    with patch.dict("os.environ", {"GMS_WORKER_AAPT2_PATH": str(binary)}):
        assert _aapt2_path() == str(binary)


def test_idle_ats_console_is_not_reported_as_running_test():
    assert not _is_active_invocation([
        "/bin/bash", "./cts-tradefed", "com.google.devtools.mobileharness.infra.ats.console.AtsConsole"
    ])
    assert _is_active_invocation([
        "java", "com.android.compatibility.common.tradefed.command.CompatibilityConsole",
        "run", "commandAndExit", "cts", "-s", "SERIAL",
    ])


def test_device_action_strips_worker_namespace_and_uses_argv():
    probe = [{"serial": "ABC", "state": "available"}]
    completed = subprocess.CompletedProcess([], 0, stdout="rebooting\n", stderr="")
    with patch("worker_agent.inventory.probe_devices", return_value=probe), patch(
        "worker_agent.inventory.subprocess.run", return_value=completed
    ) as run:
        result = execute_device_action("reboot_bootloader", ["worker-246:ABC"])
    assert result["summary"] == {"total": 1, "success": 1, "failed": 0}
    assert run.call_args.args[0] == ["adb", "-s", "ABC", "reboot", "bootloader"]
    assert run.call_args.kwargs["check"] is False


def test_device_action_rejects_device_not_attached_to_worker():
    with patch("worker_agent.inventory.probe_devices", return_value=[]):
        try:
            execute_device_action("reboot", ["worker-246:OTHER"])
        except ValueError as exc:
            assert "not attached" in str(exc)
        else:
            raise AssertionError("expected unattached device rejection")


def test_usbip_attach_uses_root_owned_helper_and_validated_argv():
    completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    with patch("worker_agent.inventory.subprocess.run", return_value=completed) as run, patch(
        "worker_agent.inventory.time.sleep"
    ), patch("worker_agent.inventory.probe_devices", return_value=[]):
        result = execute_usbip_action("attach", "192.0.2.10", ["1-2"])
    assert result["attached_busids"] == ["1-2"]
    assert run.call_args.args[0] == [
        "sudo", "-n", "/usr/local/libexec/gms-worker-usbip",
        "attach", "192.0.2.10", "1-2",
    ]
    assert run.call_args.kwargs["check"] is False


def test_usbip_attach_retries_transient_export_busy_error():
    busy = subprocess.CompletedProcess(
        [], 1, stdout="",
        stderr="usbip: error: Attach Request for 1-2 failed - Device busy (exported)",
    )
    completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    with patch(
        "worker_agent.inventory.subprocess.run",
        side_effect=[completed, busy, completed],
    ) as run, patch("worker_agent.inventory.time.sleep") as sleep, patch(
        "worker_agent.inventory.probe_devices", return_value=[]
    ):
        result = execute_usbip_action("attach", "192.0.2.10", ["1-2"])
    assert result["attached_busids"] == ["1-2"]
    assert run.call_count == 3
    sleep.assert_any_call(2)


def test_usbip_attach_reuses_matching_existing_worker_port():
    port_output = """Port 00: <Port in Use> at High Speed(480Mbps)
       usbip://192.0.2.10:3240/1-2
"""
    completed = subprocess.CompletedProcess([], 0, stdout=port_output, stderr="")
    with patch("worker_agent.inventory.subprocess.run", return_value=completed) as run, patch(
        "worker_agent.inventory.time.sleep"
    ), patch("worker_agent.inventory.probe_devices", return_value=[]):
        result = execute_usbip_action("attach", "192.0.2.10", ["1-2"])
    assert result["attached_busids"] == ["1-2"]
    assert result["already_attached_busids"] == ["1-2"]
    assert run.call_count == 1
    assert run.call_args.args[0][-1] == "port"


def test_usbip_attach_rollback_preserves_preexisting_ports():
    existing_ports = """Port 00: <Port in Use>
       usbip://192.0.2.10:3240/1-1
"""
    ports_after_partial_attach = """Port 00: <Port in Use>
       usbip://192.0.2.10:3240/1-1
Port 01: <Port in Use>
       usbip://192.0.2.10:3240/1-2
"""
    completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    failed = subprocess.CompletedProcess(
        [], 1, stdout="", stderr="permanent attach failure"
    )
    with patch(
        "worker_agent.inventory.subprocess.run",
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout=existing_ports, stderr=""),
            completed,
            failed,
            subprocess.CompletedProcess(
                [], 0, stdout=ports_after_partial_attach, stderr=""
            ),
            completed,
        ],
    ) as run, patch("worker_agent.inventory.probe_devices", return_value=[]):
        try:
            execute_usbip_action(
                "attach", "192.0.2.10", ["1-1", "1-2", "1-3"]
            )
        except RuntimeError as exc:
            assert "已回滚" in str(exc)
        else:
            raise AssertionError("expected atomic attach failure")

    detach_calls = [
        call.args[0]
        for call in run.call_args_list
        if call.args[0][-2:-1] == ["detach"]
    ]
    assert detach_calls == [[
        "sudo", "-n", "/usr/local/libexec/gms-worker-usbip",
        "detach", "01",
    ]]


def test_usbip_action_rejects_shell_metacharacters():
    try:
        execute_usbip_action("attach", "192.0.2.10;touch", ["1-2"])
    except ValueError as exc:
        assert "source host" in str(exc)
    else:
        raise AssertionError("expected invalid host rejection")


def test_usbip_detach_matches_source_and_busid_before_detaching_port():
    port_output = """Port 00: <Port in Use> at High Speed(480Mbps)
       usbip://192.0.2.10:3240/1-2
       remote bus/dev 001/002
"""
    completed_port = subprocess.CompletedProcess([], 0, stdout=port_output, stderr="")
    completed_detach = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    with patch(
        "worker_agent.inventory.subprocess.run",
        side_effect=[completed_port, completed_detach],
    ) as run, patch("worker_agent.inventory.probe_devices", return_value=[]):
        result = execute_usbip_action("detach", "192.0.2.10", ["1-2"])
    assert result["detached_ports"] == ["00"]
    assert run.call_args_list[0].args[0][-1] == "port"
    assert run.call_args_list[1].args[0][-2:] == ["detach", "00"]


def test_usbip_detach_does_not_match_busid_prefix():
    port_output = """Port 00: <Port in Use> at High Speed(480Mbps)
       usbip://192.0.2.10:3240/1-10
       remote bus/dev 001/010
"""
    completed_port = subprocess.CompletedProcess(
        [], 0, stdout=port_output, stderr=""
    )
    with patch(
        "worker_agent.inventory.subprocess.run",
        return_value=completed_port,
    ), patch("worker_agent.inventory.probe_devices", return_value=[]):
        result = execute_usbip_action("detach", "192.0.2.10", ["1-1"])
    assert result["detached_ports"] == []
    assert result["already_detached"] is True


def test_usbip_detach_settles_stale_offline_entries():
    # Right after detach, ADB briefly keeps reporting the removed serial as
    # offline. The detach result must wait for those stale entries to clear
    # instead of returning a list that still looks "online".
    port_output = """Port 00: <Port in Use> at High Speed(480Mbps)
       usbip://192.0.2.10:3240/1-2
       remote bus/dev 001/002
"""
    completed_port = subprocess.CompletedProcess([], 0, stdout=port_output, stderr="")
    completed_detach = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    offline_once = [{"serial": "1-2-serial", "state": "offline", "transport": "local_usb"}]
    with patch(
        "worker_agent.inventory.subprocess.run",
        side_effect=[completed_port, completed_detach],
    ), patch(
        "worker_agent.inventory.probe_devices",
        side_effect=[offline_once, offline_once, []],
    ) as probe, patch("worker_agent.inventory.time.sleep"):
        result = execute_usbip_action("detach", "192.0.2.10", ["1-2"])
    assert result["detached_ports"] == ["00"]
    assert result["devices"] == []
    # Settle loop kept probing until the offline entry cleared.
    assert probe.call_count >= 2


def test_usbip_detach_is_idempotent_when_selected_device_is_not_attached():
    # Detaching an export that is no longer attached (already gone, or never
    # attached) must succeed idempotently so the controller can clear stale
    # assignment records instead of looping on 502.
    completed = subprocess.CompletedProcess(
        [], 0,
        stdout="Port 00: <Port in Use>\n       usbip://192.0.2.11:3240/2-1\n",
        stderr="",
    )
    with patch("worker_agent.inventory.subprocess.run", return_value=completed), patch(
        "worker_agent.inventory.probe_devices", return_value=[]
    ):
        result = execute_usbip_action("detach", "192.0.2.10", ["1-2"])
    assert result["detached_ports"] == []
    assert result["already_detached"] is True


def test_wifi_action_passes_credentials_as_argv_without_shell_interpolation():
    probe = [{"serial": "ABC", "state": "available"}]
    completed = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
    with patch("worker_agent.inventory.probe_devices", return_value=probe), patch(
        "worker_agent.inventory.subprocess.run", return_value=completed
    ) as run:
        result = execute_device_action("wifi", ["worker-246:ABC"], {
            "ssid": "lab wifi; touch /tmp/no", "password": "p a$s",
        })
    assert result["summary"]["success"] == 1
    assert run.call_args_list[1].args[0] == [
        "adb", "-s", "ABC", "shell", "cmd", "wifi", "connect-network",
        "lab wifi; touch /tmp/no", "wpa2", "p a$s",
    ]
    assert all(call.kwargs.get("shell") is not True for call in run.call_args_list)


def test_scrcpy_action_is_scoped_to_serial_and_starts_detached_process():
    probe = [{"serial": "ABC", "state": "available"}]
    process = type("Process", (), {"pid": 1234})()
    with patch("worker_agent.inventory.probe_devices", return_value=probe), patch(
        "worker_agent.inventory.shutil.which", return_value="/usr/bin/scrcpy"
    ), patch("worker_agent.inventory.Path.iterdir", return_value=[]), patch(
        "worker_agent.inventory.subprocess.Popen", return_value=process
    ) as popen:
        result = execute_device_action("scrcpy_start", ["worker-246:ABC"], {"display": ":1"})
    argv = popen.call_args.args[0]
    assert argv[:3] == ["/usr/bin/scrcpy", "-s", "ABC"]
    assert popen.call_args.kwargs["start_new_session"] is True
    assert popen.call_args.kwargs["env"]["DISPLAY"] == ":1"
    assert result["results"][0]["pid"] == 1234


def test_screenshot_action_returns_png_data_url():
    probe = [{"serial": "ABC", "state": "available"}]
    completed = subprocess.CompletedProcess([], 0, stdout=b"\x89PNG\r\n", stderr=b"")
    with patch("worker_agent.inventory.probe_devices", return_value=probe), patch(
        "worker_agent.inventory.subprocess.run", return_value=completed
    ) as run:
        result = execute_device_action("screenshot", ["worker-246:ABC"])
    assert result["serial"] == "ABC"
    assert result["image"].startswith("data:image/png;base64,")
    assert run.call_args.args[0] == ["adb", "-s", "ABC", "exec-out", "screencap", "-p"]


def test_suite_actions_are_confined_and_return_browser_shape(tmp_path):
    suite_root = tmp_path / "android-cts-17_r1"
    tools = suite_root / "tools"
    tools.mkdir(parents=True)
    (suite_root / "results").mkdir()
    (suite_root / "test_result.xml").write_text("ok", encoding="utf-8")
    config = WorkerConfig(worker_id="w", controller_url="https://controller", token="t",
                          suite_roots=[tmp_path], data_root=tmp_path / "data")
    listing = execute_suite_action(config, {"action": "list", "suite_path": str(tools), "path": ""})
    assert {item["name"] for item in listing["items"]} == {"results", "test_result.xml", "tools"}
    search = execute_suite_action(config, {"action": "search", "suite_path": str(tools),
                                           "query": "result", "limit": 10})
    assert {item["path"] for item in search["items"]} == {"results", "test_result.xml"}
    file_result = execute_suite_action(config, {"action": "read_file", "suite_path": str(tools),
                                                "path": "test_result.xml"})
    assert file_result["filename"] == "test_result.xml"
    assert file_result["content_base64"] == "b2s="


def test_suite_action_rejects_path_outside_roots(tmp_path):
    config = WorkerConfig(worker_id="w", controller_url="https://controller", token="t",
                          suite_roots=[tmp_path / "allowed"], data_root=tmp_path / "data")
    try:
        execute_suite_action(config, {"action": "list", "suite_path": "/tmp/not-allowed"})
    except ValueError as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("expected suite root rejection")


def test_suite_download_streams_into_worker_suite_root(tmp_path):
    root = tmp_path / "suites"
    root.mkdir()
    config = WorkerConfig(worker_id="w", controller_url="https://controller", token="t",
                          suite_roots=[root], data_root=tmp_path / "data")
    with patch("worker_agent.inventory.urllib.request.urlopen", return_value=io.BytesIO(b"archive")):
        result = execute_suite_action(config, {
            "action": "download_url", "url": "https://example.test/android-cts-17_r1.zip"
        })
    assert (root / "android-cts-17_r1.zip").read_bytes() == b"archive"
    assert result["file_size"] == 7


def test_suite_download_preserves_explicit_original_filename(tmp_path):
    root = tmp_path / "suites"
    root.mkdir()
    config = WorkerConfig(worker_id="w", controller_url="https://controller", token="t",
                          suite_roots=[root], data_root=tmp_path / "data")
    with patch("worker_agent.inventory.urllib.request.urlopen", return_value=io.BytesIO(b"sts")):
        result = execute_suite_action(config, {
            "action": "download_url", "url": "https://controller/suite-123.zip",
            "filename": "android-sts-17_sts-r52-linux-arm64.zip",
        })
    expected = root / "android-sts-17_sts-r52-linux-arm64.zip"
    assert expected.read_bytes() == b"sts"
    assert result["archive_path"] == str(expected)


def test_controller_suite_download_sends_worker_token(tmp_path):
    root = tmp_path / "suites"
    root.mkdir()
    config = WorkerConfig(
        worker_id="w",
        controller_url="https://controller",
        token="worker-secret",
        suite_roots=[root],
        data_root=tmp_path / "data",
    )
    with patch(
        "worker_agent.inventory.urllib.request.urlopen",
        return_value=io.BytesIO(b"suite"),
    ) as opened:
        execute_suite_action(
            config,
            {
                "action": "download_url",
                "url": "https://controller/api/cluster/suite-library-download/safe/file.zip",
                "filename": "file.zip",
            },
        )

    request = opened.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer worker-secret"


def test_controller_suite_download_attaches_token_when_browser_host_differs(tmp_path):
    """The browser builds the download URL from window.location, whose host may
    differ from the controller_url the Worker dials (reverse proxy / DNS alias).
    The suite-library-download path is unique to the Controller, so the Worker
    must still attach its token instead of failing the callback with 401."""
    root = tmp_path / "suites"
    root.mkdir()
    config = WorkerConfig(
        worker_id="w",
        controller_url="https://controller.internal",
        token="worker-secret",
        suite_roots=[root],
        data_root=tmp_path / "data",
    )
    with patch(
        "worker_agent.inventory.urllib.request.urlopen",
        return_value=io.BytesIO(b"suite"),
    ) as opened:
        execute_suite_action(
            config,
            {
                "action": "download_url",
                "url": "https://10.10.10.206/api/cluster/suite-library-download/safe/file.zip",
                "filename": "file.zip",
            },
        )

    request = opened.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer worker-secret"


def test_suite_archive_listing_and_safe_extraction(tmp_path):
    root = tmp_path / "suites"
    root.mkdir()
    archive = root / "android-cts-probe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("android-cts/tools/cts-tradefed", "probe")
    config = WorkerConfig(worker_id="w", controller_url="https://controller", token="t",
                          suite_roots=[root], data_root=tmp_path / "data")
    listed = execute_suite_action(config, {"action": "list_archives"})
    assert listed["archives"][0]["path"] == str(archive)
    extracted = execute_suite_action(config, {"action": "extract", "archive_path": str(archive),
                                               "target_dir_name": "cts-probe"})
    assert (root / "cts-probe/android-cts/tools/cts-tradefed").read_text() == "probe"
    assert extracted["extracted_path"] == str(root / "cts-probe")


def test_suite_zip_extraction_restores_executable_mode(tmp_path):
    root = tmp_path / "suites"
    root.mkdir()
    archive = root / "android-vts.zip"
    entry = zipfile.ZipInfo("android-vts/tools/vts-tradefed")
    entry.create_system = 3
    entry.external_attr = 0o100755 << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(entry, "#!/bin/sh\n")
    config = WorkerConfig(worker_id="w", controller_url="https://controller", token="t",
                          suite_roots=[root], data_root=tmp_path / "data")
    execute_suite_action(config, {"action": "extract", "archive_path": str(archive),
                                  "target_dir_name": "android-vts"})
    assert (root / "android-vts/android-vts/tools/vts-tradefed").stat().st_mode & 0o111


def test_suite_extraction_rejects_zip_slip(tmp_path):
    root = tmp_path / "suites"
    root.mkdir()
    archive = root / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape", "bad")
    config = WorkerConfig(worker_id="w", controller_url="https://controller", token="t",
                          suite_roots=[root], data_root=tmp_path / "data")
    try:
        execute_suite_action(config, {"action": "extract", "archive_path": str(archive),
                                      "target_dir_name": "unsafe"})
    except ValueError as exc:
        assert "unsafe path" in str(exc)
    else:
        raise AssertionError("expected zip-slip rejection")


def test_prepare_suite_directory_export_creates_zip(tmp_path):
    root = tmp_path / "suites"
    target = root / "android-cts/results/run-1"
    target.mkdir(parents=True)
    (target / "test_result.xml").write_text("result", encoding="utf-8")
    config = WorkerConfig(worker_id="w", controller_url="https://controller", token="t",
                          suite_roots=[root], data_root=tmp_path / "data")
    archive, temporary = prepare_suite_export(config, {"transfer_id": "t1",
        "suite_path": str(root / "android-cts/tools"), "path": "results/run-1", "directory": True})
    assert temporary is True
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.read("run-1/test_result.xml") == b"result"


def test_firmware_flash_requires_worker_staging_and_exactly_one_loader(tmp_path):
    data_root = tmp_path / "data"
    image = data_root / "firmware" / "fw-1" / "update.img"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    tool = tmp_path / "upgrade_tool"
    tool.write_text("tool")
    tool.chmod(0o755)
    config = WorkerConfig(worker_id="w", controller_url="https://controller", token="t",
                          data_root=data_root, suite_roots=[tmp_path / "suites"])
    responses = [
        subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="List of rockusb connected(1)\n", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="Download Firmware Success", stderr=""),
    ]
    with patch.dict("os.environ", {"GMS_WORKER_UPGRADE_TOOL": str(tool)}), patch(
        "worker_agent.inventory.probe_devices", return_value=[{"serial": "ABC"}]
    ), patch("worker_agent.inventory.time.sleep"), patch(
        "worker_agent.inventory.subprocess.run", side_effect=responses
    ) as run:
        result = flash_firmware(config, image, ["worker-246:ABC"])
    assert result["success"] is True
    assert run.call_args_list[-1].args[0] == [str(tool), "uf", str(image)]
