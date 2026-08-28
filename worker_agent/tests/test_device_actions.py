from __future__ import annotations

import io
import subprocess
import tarfile
import zipfile
from unittest.mock import patch

from worker_agent.android_inspection import _aapt2_path
from worker_agent.config import WorkerConfig
from worker_agent.inventory import (
    execute_device_action,
    execute_suite_action,
    execute_usbip_action,
    flash_firmware,
    import_suite_report,
    prepare_suite_export,
)


def test_aapt2_path_accepts_configured_worker_binary(tmp_path):
    binary = tmp_path / "aapt2"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    with patch.dict("os.environ", {"GMS_WORKER_AAPT2_PATH": str(binary)}):
        assert _aapt2_path() == str(binary)


def test_device_action_strips_worker_namespace_and_uses_argv():
    probe = [{"serial": "ABC", "state": "available"}]
    completed = subprocess.CompletedProcess([], 0, stdout="rebooting\n", stderr="")
    with patch("worker_agent.device_actions.probe_devices", return_value=probe), patch(
        "worker_agent.device_actions.subprocess.run", return_value=completed
    ) as run:
        result = execute_device_action("reboot_bootloader", ["worker-246:ABC"])
    assert result["summary"] == {"total": 1, "success": 1, "failed": 0}
    assert run.call_args.args[0] == ["adb", "-s", "ABC", "reboot", "bootloader"]
    assert run.call_args.kwargs["check"] is False


def test_device_action_rejects_device_not_attached_to_worker():
    with patch("worker_agent.device_actions.probe_devices", return_value=[]):
        try:
            execute_device_action("reboot", ["worker-246:OTHER"])
        except ValueError as exc:
            assert "not attached" in str(exc)
        else:
            raise AssertionError("expected unattached device rejection")


def test_usbip_action_uses_required_native_executor():
    expected = {"attached_busids": ["1-2"]}
    with patch(
        "worker_agent.device_actions.resolve_native_tool",
        return_value="/opt/gms/gms-usbip-control",
    ), patch(
        "worker_agent.device_actions.execute_external_transport",
        return_value=expected,
    ) as execute:
        result = execute_usbip_action(
            "attach", "192.0.2.10", ["1-2"], "tcp:127.0.0.1:5039", 7
        )

    assert result == expected
    execute.assert_called_once_with(
        "/opt/gms/gms-usbip-control",
        transport="usbip",
        action="attach",
        payload={
            "source_host": "192.0.2.10",
            "busids": ["1-2"],
            "adb_server_socket": "tcp:127.0.0.1:5039",
            "generation": 7,
        },
        timeout=180,
    )


def test_usbip_action_rejects_shell_metacharacters_before_native_execution():
    with patch("worker_agent.device_actions.execute_external_transport") as execute:
        try:
            execute_usbip_action("attach", "192.0.2.10;touch", ["1-2"])
        except ValueError as exc:
            assert "source host" in str(exc)
        else:
            raise AssertionError("expected invalid host rejection")
    execute.assert_not_called()

def test_wifi_action_passes_credentials_as_argv_without_shell_interpolation():
    probe = [{"serial": "ABC", "state": "available"}]
    completed = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
    with patch("worker_agent.device_actions.probe_devices", return_value=probe), patch(
        "worker_agent.device_actions.subprocess.run", return_value=completed
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
    with patch("worker_agent.device_actions.probe_devices", return_value=probe), patch(
        "worker_agent.device_actions.shutil.which", return_value="/usr/bin/scrcpy"
    ), patch("worker_agent.device_actions.Path.iterdir", return_value=[]), patch(
        "worker_agent.device_actions.subprocess.Popen", return_value=process
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
    with patch("worker_agent.device_actions.probe_devices", return_value=probe), patch(
        "worker_agent.device_actions.subprocess.run", return_value=completed
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
    with patch("worker_agent.suite_actions.urllib.request.urlopen", return_value=io.BytesIO(b"archive")):
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
    with patch("worker_agent.suite_actions.urllib.request.urlopen", return_value=io.BytesIO(b"sts")):
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
        "worker_agent.suite_actions.urllib.request.urlopen",
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


def test_controller_suite_download_routes_browser_alias_through_controller(tmp_path):
    """A browser alias is replaced by the Worker's trusted Controller origin."""
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
        "worker_agent.suite_actions.urllib.request.urlopen",
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
    assert request.full_url == (
        "https://controller.internal/api/cluster/"
        "suite-library-download/safe/file.zip"
    )
    assert request.get_header("Authorization") == "Bearer worker-secret"


def test_external_suite_download_does_not_send_worker_token(tmp_path):
    root = tmp_path / "suites"
    root.mkdir()
    config = WorkerConfig(
        worker_id="w",
        controller_url="https://controller:8443",
        token="worker-secret",
        suite_roots=[root],
        data_root=tmp_path / "data",
    )
    with patch(
        "worker_agent.suite_actions.urllib.request.urlopen",
        return_value=io.BytesIO(b"suite"),
    ) as opened:
        execute_suite_action(
            config,
            {
                "action": "download_url",
                "url": "https://downloads.example.test/releases/file.zip",
                "filename": "file.zip",
            },
        )

    request = opened.call_args.args[0]
    assert request.get_header("Authorization") is None


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


def test_suite_extraction_rejects_special_tar_members(tmp_path):
    root = tmp_path / "suites"
    root.mkdir()
    archive = root / "unsafe.tar"
    with tarfile.open(archive, "w") as bundle:
        member = tarfile.TarInfo("android-cts/unsafe.pipe")
        member.type = tarfile.FIFOTYPE
        bundle.addfile(member)
    config = WorkerConfig(
        worker_id="w",
        controller_url="https://controller",
        token="t",
        suite_roots=[root],
        data_root=tmp_path / "data",
    )

    try:
        execute_suite_action(
            config,
            {
                "action": "extract",
                "archive_path": str(archive),
                "target_dir_name": "unsafe-tar",
            },
        )
    except ValueError as exc:
        assert "unsafe path or link" in str(exc)
    else:
        raise AssertionError("expected unsafe tar member rejection")


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


class _TruncatedResponse(io.BytesIO):
    """urlopen response that promises more bytes than it delivers."""

    def __init__(self, body: bytes, content_length: int):
        super().__init__(body)
        self.headers = {"Content-Length": str(content_length)}


def test_suite_download_rejects_truncated_archive(tmp_path):
    root = tmp_path / "suites"
    root.mkdir()
    config = WorkerConfig(worker_id="w", controller_url="https://controller", token="t",
                          suite_roots=[root], data_root=tmp_path / "data")
    with patch("worker_agent.suite_actions.urllib.request.urlopen",
               return_value=_TruncatedResponse(b"PK-header-only", 1000)):
        try:
            execute_suite_action(config, {
                "action": "download_url",
                "url": "https://example.test/android-cts-17_r1.zip",
            })
        except ValueError as exc:
            assert "incomplete" in str(exc)
        else:
            raise AssertionError("expected truncated download rejection")
    assert not (root / "android-cts-17_r1.zip").exists()
    assert not list(root.glob(".*.part"))


def test_suite_extract_reports_truncated_zip(tmp_path):
    root = tmp_path / "suites"
    root.mkdir()
    archive = root / "android-cts-17_r1.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("android-cts/tools/cts-tradefed", "probe")
    # 截断：保留 ZIP 头、丢弃结尾 EOCD，复现传输中断的现场。
    data = archive.read_bytes()
    archive.write_bytes(data[: max(1, data.index(b"PK\x05\x06"))])
    config = WorkerConfig(worker_id="w", controller_url="https://controller", token="t",
                          suite_roots=[root], data_root=tmp_path / "data")
    try:
        execute_suite_action(config, {"action": "extract", "archive_path": str(archive),
                                       "target_dir_name": "cts-truncated"})
    except ValueError as exc:
        assert "incomplete or corrupted" in str(exc)
    else:
        raise AssertionError("expected truncated zip rejection")



def test_import_suite_report_extracts_one_timestamp_directory(tmp_path):
    report_name = "2026.08.07_15.56.09.558_3101"
    root = tmp_path / "suites"
    tools = root / "android-cts" / "tools"
    tools.mkdir(parents=True)
    data_root = tmp_path / "data"
    archive = data_root / "report-copies" / "transfer-1" / "report.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(f"{report_name}/test_result.xml", "result")
        bundle.writestr(f"{report_name}/module/test.txt", "passed")
    config = WorkerConfig(
        worker_id="w",
        controller_url="https://controller",
        token="t",
        suite_roots=[root],
        data_root=data_root,
    )

    result = import_suite_report(config, archive, str(tools), report_name)

    destination = root / "android-cts" / "results" / report_name
    assert result["destination"] == str(destination)
    assert result["file_count"] == 2
    assert (destination / "test_result.xml").read_text(encoding="utf-8") == "result"
    assert (destination / "module" / "test.txt").read_text(encoding="utf-8") == "passed"


def test_import_suite_report_rejects_unsafe_or_existing_destination(tmp_path):
    report_name = "2026.08.07_15.56.09.558_3101"
    root = tmp_path / "suites"
    tools = root / "android-cts" / "tools"
    tools.mkdir(parents=True)
    data_root = tmp_path / "data"
    archive = data_root / "report-copies" / "transfer-1" / "report.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(f"{report_name}/../outside.txt", "unsafe")
    config = WorkerConfig(
        worker_id="w",
        controller_url="https://controller",
        token="t",
        suite_roots=[root],
        data_root=data_root,
    )

    try:
        import_suite_report(config, archive, str(tools), report_name)
    except ValueError as exc:
        assert "unsafe path" in str(exc)
    else:
        raise AssertionError("expected unsafe report archive rejection")
    assert not (root / "android-cts" / "results" / "outside.txt").exists()

    destination = root / "android-cts" / "results" / report_name
    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(f"{report_name}/test_result.xml", "result")
    try:
        import_suite_report(config, archive, str(tools), report_name)
    except ValueError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("expected existing report rejection")


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
        "worker_agent.device_actions.probe_devices", return_value=[{"serial": "ABC"}]
    ), patch("worker_agent.device_actions.time.sleep"), patch(
        "worker_agent.device_actions.subprocess.run", side_effect=responses
    ) as run:
        result = flash_firmware(config, image, ["worker-246:ABC"])
    assert result["success"] is True
    assert run.call_args_list[-1].args[0] == [str(tool), "uf", str(image)]


def test_firmware_flash_accepts_device_already_in_fastboot(tmp_path):
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
        subprocess.CompletedProcess([], 0, stdout="List of devices attached\nABC\tdevice\n", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="List of rockusb connected(1)\n", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="Download Firmware Success", stderr=""),
    ]
    with patch.dict("os.environ", {"GMS_WORKER_UPGRADE_TOOL": str(tool)}), patch(
        "worker_agent.device_actions.probe_devices",
        return_value=[{"serial": "ABC", "state": "fastboot"}],
    ), patch("worker_agent.device_actions.time.sleep"), patch(
        "worker_agent.device_actions.subprocess.run", side_effect=responses
    ) as run:
        result = flash_firmware(config, image, ["worker-246:ABC"])

    commands = [call.args[0] for call in run.call_args_list]
    assert commands[:3] == [
        ["fastboot", "-s", "ABC", "reboot"],
        ["adb", "devices"],
        ["adb", "-s", "ABC", "reboot", "loader"],
    ]
    assert result["success"] is True
