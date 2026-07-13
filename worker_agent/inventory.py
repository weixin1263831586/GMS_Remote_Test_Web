from __future__ import annotations

import os
import base64
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import urllib.parse
import urllib.request
import tarfile
import zipfile
import mimetypes
import time
from pathlib import Path
from typing import Any, Callable

from .config import WorkerConfig


def _run(argv: list[str], timeout: int = 10) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def probe_devices() -> list[dict[str, Any]]:
    devices = []
    for line in _run(["adb", "devices", "-l"]).splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[1] not in {"device", "offline", "unauthorized"}:
            continue
        serial, adb_state = parts[0], parts[1]
        properties = {}
        for item in parts[2:]:
            key, separator, value = item.partition(":")
            if separator:
                properties[key] = value
        devices.append({
            "serial": serial, "transport": "local_usb",
            "state": "available" if adb_state == "device" else adb_state,
            "properties": properties,
        })
    # Fastboot-only devices must remain visible to the controller.
    known = {item["serial"] for item in devices}
    for line in _run(["fastboot", "devices"]).splitlines():
        serial = line.split()[0] if line.split() else ""
        if serial and serial not in known:
            devices.append({"serial": serial, "transport": "local_usb",
                            "state": "fastboot", "properties": {}})
    return devices


def execute_device_action(action: str, device_ids: list[str], options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute one strictly allow-listed Android device operation."""
    allowed = {item["serial"] for item in probe_devices()}
    serials = []
    for device_id in device_ids:
        serial = str(device_id).split(":", 1)[-1]
        if serial not in allowed:
            raise ValueError(f"device is not attached to this worker: {serial}")
        serials.append(serial)
    options = options or {}
    if action == "scrcpy_start":
        bundled_scrcpy = Path(__file__).resolve().parent.parent / "tools" / "scrcpy-linux-x86_64-v3.3.4" / "scrcpy"
        executable = os.getenv("GMS_WORKER_SCRCPY_PATH") or shutil.which("scrcpy")
        if not executable and bundled_scrcpy.is_file() and os.access(bundled_scrcpy, os.X_OK):
            executable = str(bundled_scrcpy)
        if not executable:
            raise RuntimeError("scrcpy is not installed on this Worker")
        display = str(options.get("display") or os.getenv("DISPLAY") or ":0")
        results = []
        for index, serial in enumerate(serials):
            # Exact argv inspection avoids duplicate mirrors without a broad
            # process-name kill that could affect another device or user.
            already_running = False
            for proc in Path("/proc").iterdir():
                if not proc.name.isdigit():
                    continue
                try:
                    argv = (proc / "cmdline").read_bytes().split(b"\0")
                except (OSError, PermissionError):
                    continue
                decoded = [item.decode(errors="replace") for item in argv if item]
                if decoded and Path(decoded[0]).name == "scrcpy" and "-s" in decoded:
                    position = decoded.index("-s")
                    if position + 1 < len(decoded) and decoded[position + 1] == serial:
                        already_running = True
                        break
            if already_running:
                results.append({"device": serial, "success": True, "already_running": True})
                continue
            env = dict(os.environ)
            env["DISPLAY"] = display
            process = subprocess.Popen(
                [executable, "-s", serial, "--window-title", f"GMS {serial}",
                 "--window-x", str(20 + index * 380), "--window-y", "60",
                 "--max-size", "1280", "--no-audio"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, env=env,
            )
            results.append({"device": serial, "success": True, "pid": process.pid,
                            "already_running": False, "display": display})
        return {"results": results, "summary": {"total": len(results),
                "success": sum(bool(item["success"]) for item in results), "failed": 0}}
    if action == "screenshot":
        if len(serials) != 1:
            raise ValueError("screenshot requires exactly one device")
        completed = subprocess.run(["adb", "-s", serials[0], "exec-out", "screencap", "-p"],
                                   capture_output=True, timeout=30, check=False)
        if completed.returncode != 0 or not completed.stdout:
            raise RuntimeError((completed.stderr or b"screenshot failed").decode(errors="replace"))
        return {"serial": serials[0], "image": "data:image/png;base64," +
                base64.b64encode(completed.stdout).decode("ascii")}
    if action == "layout":
        if len(serials) != 1:
            raise ValueError("layout requires exactly one device")
        serial = serials[0]
        remote = f"/data/local/tmp/gms-layout-{os.getpid()}.xml"
        dump = subprocess.run(["adb", "-s", serial, "shell", "uiautomator", "dump", remote],
                              capture_output=True, text=True, timeout=30, check=False)
        read = subprocess.run(["adb", "-s", serial, "exec-out", "cat", remote],
                              capture_output=True, text=True, timeout=30, check=False)
        subprocess.run(["adb", "-s", serial, "shell", "rm", "-f", remote],
                       capture_output=True, timeout=10, check=False)
        if dump.returncode != 0 or read.returncode != 0 or not read.stdout.strip():
            raise RuntimeError((dump.stderr or read.stderr or "layout failed").strip())
        elements = []
        for node in ET.fromstring(read.stdout).iter("node"):
            attrs = node.attrib
            nums = [int(value) for value in re.findall(r"-?\d+", attrs.get("bounds", ""))[:4]]
            center = [(nums[0] + nums[2]) // 2, (nums[1] + nums[3]) // 2] if len(nums) == 4 else None
            interactions = [name for key, name in (("clickable", "clickable"),
                ("long-clickable", "long_clickable"), ("scrollable", "scrollable"),
                ("checkable", "checkable"), ("focusable", "focusable")) if attrs.get(key) == "true"]
            if attrs.get("text") or attrs.get("content-desc") or attrs.get("resource-id") or interactions:
                elements.append({"text": attrs.get("text", ""),
                    "content_desc": attrs.get("content-desc", ""),
                    "resource_id": attrs.get("resource-id", ""), "class_name": attrs.get("class", ""),
                    "package": attrs.get("package", ""), "center": center,
                    "bounds": attrs.get("bounds", ""), "interactions": interactions,
                    "enabled": attrs.get("enabled", "true") == "true"})
        return {"serial": serial, "source": "worker-uiautomator", "elements": elements}
    if action == "tap":
        if len(serials) != 1 or options.get("x") is None or options.get("y") is None:
            raise ValueError("tap requires one device and coordinates")
        x, y = int(options["x"]), int(options["y"])
        if not 0 <= x <= 10000 or not 0 <= y <= 10000:
            raise ValueError("tap coordinates out of range")
        completed = subprocess.run(["adb", "-s", serials[0], "shell", "input", "tap", str(x), str(y)],
                                   capture_output=True, text=True, timeout=15, check=False)
        return {"serial": serials[0], "x": x, "y": y, "success": completed.returncode == 0,
                "output": (completed.stdout or completed.stderr).strip()}
    if action == "wifi":
        ssid = str(options.get("ssid") or "")
        password = str(options.get("password") or "")
        if not ssid:
            raise ValueError("wifi action requires an SSID")
        results = []
        for serial in serials:
            enabled = subprocess.run(
                ["adb", "-s", serial, "shell", "cmd", "wifi", "set-wifi-enabled", "enabled"],
                capture_output=True, text=True, timeout=20, check=False,
            )
            argv = ["adb", "-s", serial, "shell", "cmd", "wifi", "connect-network", ssid]
            if password:
                argv.extend(["wpa2", password])
            else:
                argv.append("open")
            connected = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
            results.append({"device": serial,
                "success": enabled.returncode == 0 and connected.returncode == 0,
                "output": "\n".join(filter(None, [enabled.stdout, enabled.stderr,
                                                    connected.stdout, connected.stderr])).strip()})
        return {"results": results, "summary": {"total": len(results),
                "success": sum(bool(item["success"]) for item in results),
                "failed": sum(not item["success"] for item in results)}}
    results = []
    for serial in serials:
        commands: list[list[str]]
        if action == "reboot":
            commands = [["adb", "-s", serial, "reboot"]]
        elif action == "reboot_bootloader":
            commands = [["adb", "-s", serial, "reboot", "bootloader"]]
        elif action == "remount":
            commands = [["adb", "-s", serial, "root"], ["adb", "-s", serial, "remount"]]
        elif action == "get_properties":
            commands = [["adb", "-s", serial, "shell", "getprop"]]
        elif action == "bootloader_status":
            commands = [["adb", "-s", serial, "shell", "getprop", "ro.boot.verifiedbootstate"]]
        elif action in {"bootloader_lock", "bootloader_unlock"}:
            # Keep the operation scoped to the selected serial throughout the
            # adb -> fastboot transition; never use a host-wide fastboot call.
            subprocess.run(["adb", "-s", serial, "reboot", "bootloader"],
                           capture_output=True, text=True, timeout=30, check=False)
            verb = "lock" if action == "bootloader_lock" else "unlock"
            commands = [["fastboot", "-s", serial, "flashing", verb]]
        else:
            raise ValueError(f"unsupported device action: {action}")
        output = []
        success = True
        for argv in commands:
            try:
                completed = subprocess.run(argv, capture_output=True, text=True,
                                           timeout=30, check=False)
                output.append((completed.stdout or completed.stderr).strip())
                if completed.returncode != 0:
                    success = False
                    break
            except (OSError, subprocess.TimeoutExpired) as exc:
                output.append(str(exc))
                success = False
                break
        results.append({"device": serial, "success": success, "output": "\n".join(output)})
    return {"results": results, "summary": {"total": len(results),
            "success": sum(bool(item["success"]) for item in results),
            "failed": sum(not item["success"] for item in results)}}


def execute_suite_action(config: WorkerConfig, payload: dict[str, Any],
                         progress_callback: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    action = payload.get("action")
    roots = [root.expanduser().resolve() for root in config.suite_roots if root.expanduser().exists()]
    if action == "list_archives":
        archives = []
        extensions = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2")
        for root in roots:
            for path in root.iterdir():
                if path.is_file() and path.name.lower().endswith(extensions):
                    stat = path.stat()
                    name = path.name
                    default = next((name[:-len(ext)] for ext in extensions if name.lower().endswith(ext)), path.stem)
                    archives.append({"name": name, "path": str(path), "size": stat.st_size,
                                     "modified": int(stat.st_mtime), "default_dir_name": default})
        return {"archives": sorted(archives, key=lambda item: item["modified"], reverse=True)}
    if action == "download_url":
        url = str(payload.get("url") or "").strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("suite URL must use http or https")
        filename = str(payload.get("filename") or Path(urllib.parse.unquote(parsed.path)).name)
        if (not filename or Path(filename).name != filename
                or any(ord(character) < 32 for character in filename)):
            raise ValueError("suite URL has an invalid filename")
        target_root = roots[0] if roots else None
        if target_root is None:
            raise ValueError("no configured suite root exists")
        destination = target_root / filename
        temporary = target_root / f".{filename}.part"
        max_bytes = int(os.getenv("GMS_WORKER_SUITE_DOWNLOAD_MAX_BYTES", str(80 * 1024 ** 3)))
        downloaded = 0
        last_reported = 0
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "GMS-Worker/0.1"})
            ssl_context = None
            if parsed.scheme == "https":
                import ssl
                controller_host = urllib.parse.urlparse(config.controller_url).hostname
                if parsed.hostname == controller_host:
                    ssl_context = (ssl.create_default_context(cafile=config.controller_ca)
                                   if config.controller_ca else ssl._create_unverified_context())
            with urllib.request.urlopen(request, timeout=60, context=ssl_context) as response, temporary.open("wb") as output:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    downloaded += len(block)
                    if downloaded > max_bytes:
                        raise ValueError("suite download exceeds configured size limit")
                    output.write(block)
                    if progress_callback and downloaded - last_reported >= 16 * 1024 * 1024:
                        headers = getattr(response, "headers", {})
                        total = int(payload.get("size_bytes") or headers.get("Content-Length") or 0)
                        progress_callback({"downloaded_bytes": downloaded, "total_bytes": total})
                        last_reported = downloaded
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return {"archive_path": str(destination), "file_size": downloaded,
                "message": f"downloaded {filename}"}
    if action == "extract":
        archive = Path(str(payload.get("archive_path") or "")).expanduser().resolve()
        if not archive.is_file() or not any(archive.is_relative_to(root) for root in roots):
            raise ValueError("archive is outside configured suite roots")
        folder = str(payload.get("target_dir_name") or "")
        if not re.fullmatch(r"[A-Za-z0-9._+-]+", folder):
            raise ValueError("invalid extraction folder")
        root = next(root for root in roots if archive.is_relative_to(root))
        destination = (root / folder).resolve()
        if not destination.is_relative_to(root) or destination.exists():
            raise ValueError("extraction destination already exists or is invalid")
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as bundle:
                members = bundle.infolist()
                if any(not (destination / item.filename).resolve().is_relative_to(destination) for item in members):
                    raise ValueError("archive contains an unsafe path")
                bundle.extractall(destination)
                # ZipFile.extractall does not restore Unix mode bits. Android
                # suite launchers (cts-tradefed/vts-tradefed, etc.) rely on
                # the executable bit stored in ZipInfo.external_attr.
                for item in members:
                    mode = (item.external_attr >> 16) & 0o777
                    target = (destination / item.filename).resolve()
                    if mode and target.exists():
                        target.chmod(mode)
        elif tarfile.is_tarfile(archive):
            with tarfile.open(archive) as bundle:
                members = bundle.getmembers()
                if any(not (destination / item.name).resolve().is_relative_to(destination)
                           or item.issym() or item.islnk() for item in members):
                    raise ValueError("archive contains an unsafe path or link")
                bundle.extractall(destination)
        else:
            raise ValueError("unsupported suite archive format")
        return {"extracted_path": str(destination), "message": f"extracted {archive.name}"}
    suite_path = Path(str(payload.get("suite_path") or "")).expanduser().resolve()
    suite_root = suite_path.parent if suite_path.name == "tools" else suite_path
    if not any(root.exists() and suite_root.is_relative_to(root.resolve())
               for root in config.suite_roots):
        raise ValueError("suite path is outside configured roots")
    if action == "read_file":
        relative = Path(str(payload.get("path") or ""))
        target = (suite_root / relative).resolve()
        if not target.is_relative_to(suite_root) or not target.is_file():
            raise ValueError("invalid suite file")
        max_bytes = int(os.getenv("GMS_WORKER_SUITE_READ_MAX_BYTES", str(32 * 1024 ** 2)))
        if target.stat().st_size > max_bytes:
            raise ValueError("suite file exceeds inline transfer limit")
        return {"filename": target.name, "content_type": mimetypes.guess_type(target.name)[0]
                or "application/octet-stream",
                "content_base64": base64.b64encode(target.read_bytes()).decode("ascii")}
    if action == "list":
        relative = Path(str(payload.get("path") or ""))
        target = (suite_root / relative).resolve()
        if not target.is_relative_to(suite_root) or not target.is_dir():
            raise ValueError("invalid suite directory")
        items = []
        for entry in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            try:
                stat = entry.stat()
            except OSError:
                continue
            rel = str(entry.relative_to(suite_root))
            items.append({"name": entry.name, "path": rel,
                          "type": "directory" if entry.is_dir() else "file",
                          "size": 0 if entry.is_dir() else stat.st_size,
                          "modified": int(stat.st_mtime),
                          "is_apk": entry.suffix.lower() == ".apk",
                          "is_jar": entry.suffix.lower() == ".jar"})
        return {"suite_path": str(suite_path), "suite_root": str(suite_root),
                "path": "" if target == suite_root else str(target.relative_to(suite_root)),
                "items": items}
    if action == "search":
        query = str(payload.get("query") or "").lower()
        limit = max(1, min(200, int(payload.get("limit") or 30)))
        items = []
        for current, dirs, files in os.walk(suite_root):
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            for name in sorted(dirs) + sorted(files):
                if query not in name.lower():
                    continue
                entry = Path(current) / name
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                items.append({"name": name, "path": str(entry.relative_to(suite_root)),
                              "type": "directory" if entry.is_dir() else "file",
                              "size": 0 if entry.is_dir() else stat.st_size,
                              "modified": int(stat.st_mtime),
                              "is_apk": entry.suffix.lower() == ".apk",
                              "is_jar": entry.suffix.lower() == ".jar"})
                if len(items) >= limit:
                    return {"suite_path": str(suite_path), "suite_root": str(suite_root),
                            "query": payload.get("query", ""), "items": items, "count": len(items)}
        return {"suite_path": str(suite_path), "suite_root": str(suite_root),
                "query": payload.get("query", ""), "items": items, "count": len(items)}
    raise ValueError(f"unsupported suite action: {action}")


def prepare_suite_export(config: WorkerConfig, payload: dict[str, Any]) -> tuple[Path, bool]:
    suite_path = Path(str(payload.get("suite_path") or "")).expanduser().resolve()
    suite_root = suite_path.parent if suite_path.name == "tools" else suite_path
    roots = [root.expanduser().resolve() for root in config.suite_roots if root.expanduser().exists()]
    if not any(suite_root.is_relative_to(root) for root in roots):
        raise ValueError("suite path is outside configured roots")
    target = (suite_root / Path(str(payload.get("path") or ""))).resolve()
    if not target.is_relative_to(suite_root) or not target.exists():
        raise ValueError("invalid suite export path")
    directory = bool(payload.get("directory"))
    if directory != target.is_dir():
        raise ValueError("suite export type does not match target")
    if not directory:
        return target, False
    export_root = config.data_root / "exports"
    export_root.mkdir(parents=True, exist_ok=True)
    archive_base = export_root / f"{payload.get('transfer_id')}-{target.name}"
    archive = Path(shutil.make_archive(str(archive_base), "zip", target.parent, target.name))
    return archive, True


def flash_firmware(config: WorkerConfig, firmware: Path, device_ids: list[str]) -> dict[str, Any]:
    """Flash exactly one locally attached device using a Worker-local image."""
    if len(device_ids) != 1:
        raise ValueError("firmware flashing requires exactly one device")
    serial = str(device_ids[0]).split(":", 1)[-1]
    if serial not in {item["serial"] for item in probe_devices()}:
        raise ValueError("device is not attached to this Worker")
    firmware = firmware.resolve()
    allowed_root = (config.data_root / "firmware").resolve()
    if not firmware.is_file() or not firmware.is_relative_to(allowed_root):
        raise ValueError("firmware is outside the Worker staging root")
    bundled = Path(__file__).resolve().parent.parent / "tools" / "upgrade_tool"
    tool = Path(os.getenv("GMS_WORKER_UPGRADE_TOOL", str(bundled))).resolve()
    if not tool.is_file() or not os.access(tool, os.X_OK):
        raise RuntimeError("upgrade_tool is not installed on this Worker")
    reboot = subprocess.run(["adb", "-s", serial, "reboot", "loader"], capture_output=True,
                            text=True, timeout=30, check=False)
    if reboot.returncode != 0:
        raise RuntimeError((reboot.stderr or reboot.stdout or "failed to enter loader").strip())
    time.sleep(8)
    listed = subprocess.run([str(tool), "ld"], capture_output=True, text=True, timeout=20, check=False)
    match = re.search(r"List of rockusb connected\((\d+)\)", listed.stdout or "")
    if listed.returncode != 0 or not match or int(match.group(1)) != 1:
        raise RuntimeError("expected exactly one RockUSB loader device on Worker")
    completed = subprocess.run([str(tool), "uf", str(firmware)], capture_output=True,
                               text=True, timeout=1800, check=False)
    output = "\n".join(filter(None, [completed.stdout, completed.stderr])).strip()
    return {"device": serial, "success": completed.returncode == 0,
            "exit_code": completed.returncode, "output": output[-20000:]}


def flash_gsi(config: WorkerConfig, system_img: Path, vendor_img: Path | None,
              device_ids: list[str]) -> dict[str, Any]:
    if len(device_ids) != 1:
        raise ValueError("GSI flashing requires exactly one device")
    serial = str(device_ids[0]).split(":", 1)[-1]
    if serial not in {item["serial"] for item in probe_devices()}:
        raise ValueError("device is not attached to this Worker")
    allowed = (config.data_root / "firmware").resolve()
    for image in (system_img, vendor_img):
        if image and (not image.resolve().is_relative_to(allowed) or not image.is_file()):
            raise ValueError("GSI image is outside Worker staging root")
    script = Path(__file__).resolve().parent.parent / "scripts" / "run_GSI_Burn.sh"
    if not script.is_file():
        raise RuntimeError("GSI burn script is not installed on Worker")
    argv = [str(script), serial, "--system", str(system_img)]
    if vendor_img:
        argv.extend(["--vendor", str(vendor_img)])
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=1800, check=False)
    output = "\n".join(filter(None, [completed.stdout, completed.stderr])).strip()
    return {"device": serial, "success": completed.returncode == 0,
            "exit_code": completed.returncode, "output": output[-20000:]}


def _suite_details(path: Path) -> tuple[str, str]:
    lowered = str(path).lower()
    suite_type = next((name for name in ("CTS", "GTS", "VTS", "STS")
                       if f"{name.lower()}-tradefed" in path.name.lower()
                       or f"android-{name.lower()}" in lowered), "XTS")
    match = re.search(r"(?:android-)?(?:cts|gts|vts|sts)[-_]([0-9]+(?:_r[0-9]+)?)", lowered)
    return suite_type, match.group(1) if match else ""


def scan_suites(config: WorkerConfig) -> list[dict[str, Any]]:
    suites = []
    seen = set()
    names = {"cts-tradefed", "gts-tradefed", "vts-tradefed", "sts-tradefed"}
    for root in config.suite_roots:
        if not root.exists():
            continue
        for current, dirs, files in os.walk(root):
            depth = len(Path(current).relative_to(root).parts)
            if depth > 5:
                dirs[:] = []
                continue
            for filename in names.intersection(files):
                executable = Path(current) / filename
                tools_path = str(executable.parent)
                if tools_path in seen:
                    continue
                seen.add(tools_path)
                suite_type, version = _suite_details(executable)
                suites.append({
                    "suite_type": suite_type, "suite_version": version,
                    "suite_key": f"{suite_type}:{version or executable.parent.parent.name}",
                    "tools_path": tools_path, "checksum": "", "size_bytes": 0,
                    "available": os.access(executable, os.X_OK),
                })
    return suites


def host_metrics(config: WorkerConfig) -> dict[str, float]:
    usage = shutil.disk_usage(config.data_root.parent if config.data_root.parent.exists() else Path.home())
    memory_percent = 0.0
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0])
        memory_percent = 100 * (1 - values["MemAvailable"] / values["MemTotal"])
    except Exception:
        pass
    try:
        load = os.getloadavg()[0]
        cpu_percent = min(100.0, load * 100 / max(1, os.cpu_count() or 1))
    except OSError:
        cpu_percent = 0.0
    return {"cpu_percent": round(cpu_percent, 2),
            "memory_percent": round(memory_percent, 2),
            "disk_free_gb": round(usage.free / 1024 ** 3, 2)}
