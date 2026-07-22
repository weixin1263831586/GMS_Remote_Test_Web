"""Allow-listed Android inspection operations executed by a cluster Worker.

The Controller must never interpolate browser supplied values into a remote
shell.  Every operation in this module builds an argv list and is scoped to a
single device serial that was already verified against the Worker's inventory.
"""

from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any


_PACKAGE_RE = re.compile(r"^[A-Za-z0-9_.]+$")
_RESOURCE_RE = re.compile(
    r"^\s*resource\s+0x[0-9a-fA-F]+\s+([a-z-]+)/([A-Za-z0-9_]+)(?:\s+PUBLIC)?\s*$"
)
_DEFAULT_VALUE_RE = re.compile(r"^\s*\(\)\s(.*)$")
_BEST_MATCH_RE = re.compile(r"Best matching is from .*? of ([\w.]+)\s*$")
_PROP_RE = re.compile(r"^\[([^\]]*)\]:\s*\[([^\]]*)\]\s*$")
_RESOURCE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_RESOURCE_TYPES = {
    "bool", "integer", "string", "dimen", "fraction",
    "integer-array", "string-array", "array",
}
_EXPORT_PREFIXES = (
    "/system/", "/system_ext/", "/product/", "/vendor/", "/odm/",
    "/data/app/", "/apex/",
)
_OVERLAY_DIR = "/product/overlay/GmsConfigOverride"
_OVERLAY_APK = "GmsConfigOverride.apk"


def _aapt2_path() -> str:
    configured = os.getenv("GMS_WORKER_AAPT2_PATH", "").strip()
    bundled = Path(__file__).resolve().parent.parent / "tools/aapt2"
    candidates = [configured, shutil.which("aapt2") or "", str(bundled)]
    candidates.extend(str(path) for path in sorted(Path("/usr/lib/android-sdk/build-tools").glob("*/aapt2"), reverse=True))
    executable = next((value for value in candidates if value and os.path.isfile(value) and os.access(value, os.X_OK)), "")
    if not executable:
        raise RuntimeError(
            "aapt2 is not installed on this Worker; redeploy the Worker or install android-sdk-build-tools"
        )
    return executable


def _run(
    argv: list[str],
    timeout: int = 60,
    *,
    binary: bool = False,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=not binary,
        timeout=timeout,
        check=False,
        cwd=cwd,
    )
    if completed.returncode != 0:
        error = completed.stderr or completed.stdout or "command failed"
        if isinstance(error, bytes):
            error = error.decode(errors="replace")
        raise RuntimeError(str(error).strip())
    return completed


def _adb(serial: str, *args: str, timeout: int = 60) -> str:
    return str(_run(["adb", "-s", serial, *args], timeout=timeout).stdout or "")


def _pm(serial: str, *args: str) -> str:
    return _adb(serial, "shell", "pm", "list", *args, timeout=60)


def list_packages_with_path(serial: str) -> list[dict[str, str]]:
    rows = []
    for line in _pm(serial, "packages", "-f").splitlines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        path, separator, package = line[8:].rpartition("=")
        rows.append({"path": path if separator else line[8:], "package": package})
    return rows


def list_packages(serial: str) -> list[str]:
    return sorted(
        line[8:].strip()
        for line in _pm(serial, "packages").splitlines()
        if line.strip().startswith("package:") and line[8:].strip()
    )


def list_features(serial: str) -> list[dict[str, str]]:
    rows = []
    for line in _pm(serial, "features").splitlines():
        line = line.strip()
        if not line.startswith("feature:"):
            continue
        name, separator, version = line[8:].partition("=")
        rows.append({"name": name.strip(), "version": version.strip() if separator else ""})
    return sorted(rows, key=lambda item: item["name"])


def list_properties(serial: str) -> list[dict[str, str]]:
    rows = []
    for line in _adb(serial, "shell", "getprop", timeout=30).splitlines():
        match = _PROP_RE.match(line.strip())
        if match and match.group(1).strip():
            rows.append({"name": match.group(1).strip(), "value": match.group(2).strip()})
    return sorted(rows, key=lambda item: item["name"])


def _package_path(serial: str, package: str) -> str:
    if not _PACKAGE_RE.fullmatch(package):
        raise ValueError("invalid Android package name")
    output = _adb(serial, "shell", "pm", "path", package, timeout=30)
    first = next((line.strip() for line in output.splitlines() if line.strip()), "")
    if not first.startswith("package:/"):
        raise RuntimeError(f"package APK path not found: {package}")
    return first[8:]


def _pull_package(serial: str, package: str, directory: Path) -> tuple[str, Path]:
    remote = _package_path(serial, package)
    local = directory / "symbols.apk"
    _run(["adb", "-s", serial, "pull", remote, str(local)], timeout=180)
    if not local.is_file() or local.stat().st_size <= 0:
        raise RuntimeError("pulled package APK is empty")
    return remote, local


def _parse_resources(apk: Path) -> list[dict[str, Any]]:
    aapt2 = _aapt2_path()
    output = _run([aapt2, "dump", "resources", str(apk)], timeout=180).stdout or ""
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    array_lines: list[str] = []
    collecting_array = False
    for line in str(output).splitlines():
        match = _RESOURCE_RE.match(line)
        if match:
            if current is not None:
                entries.append(current)
            resource_type, name = match.groups()
            current = {
                "name": f"{resource_type}/{name}", "type": resource_type,
                "resname": name, "default_value": None,
                "effective_value": None, "overlay_changed": None,
                "overlay_source": None, "lookup_error": None,
            }
            array_lines = []
            collecting_array = False
            continue
        if current is not None and collecting_array:
            array_lines.append(line.strip())
            if line.strip().endswith("]"):
                current["default_value"] = " ".join(array_lines)
                collecting_array = False
            continue
        if current is not None and current["default_value"] is None:
            value_match = _DEFAULT_VALUE_RE.match(line)
            if value_match:
                value = value_match.group(1).strip()
                if value.startswith("(array)"):
                    collecting_array = True
                else:
                    current["default_value"] = value
    if current is not None:
        entries.append(current)
    return entries


def _lookup_effective(serial: str, package: str, entry: dict[str, Any]) -> None:
    resource = f"{package}:{entry['type']}/{entry['resname']}"
    try:
        output = _adb(
            serial, "shell", "cmd", "overlay", "lookup", "--verbose",
            package, resource, timeout=20,
        )
    except Exception as exc:
        entry["lookup_error"] = str(exc)
        return
    source = None
    values: list[str] = []
    after_marker = False
    for line in output.splitlines():
        if not after_marker:
            match = _BEST_MATCH_RE.search(line)
            if match:
                source = match.group(1).strip()
                after_marker = True
            continue
        if line.strip():
            values.append(line.strip())
    entry["effective_value"] = values[-1] if values else ""
    entry["overlay_source"] = None if source in {None, package} else source
    entry["overlay_changed"] = entry["overlay_source"] is not None


def explore_resources(serial: str, options: dict[str, Any]) -> dict[str, Any]:
    package = str(options.get("package") or "android")
    name_filter = str(options.get("name_filter") or "").lower().strip()
    type_filter = str(options.get("type_filter") or "").lower().strip()
    with_effective = bool(options.get("with_effective"))
    effective_limit = max(0, min(int(options.get("effective_limit") or 0), 5000))
    with tempfile.TemporaryDirectory(prefix="gms-worker-config-") as directory:
        remote, local = _pull_package(serial, package, Path(directory))
        entries = _parse_resources(local)
    entries = [item for item in entries if item["resname"].startswith("config_")]
    if name_filter:
        entries = [item for item in entries if name_filter in item["resname"].lower()]
    if type_filter:
        entries = [item for item in entries if item["type"] == type_filter]
    targets = entries if effective_limit <= 0 else entries[:effective_limit]
    if with_effective and targets:
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            list(pool.map(lambda item: _lookup_effective(serial, package, item), targets))
    return {
        "package": package,
        "apk_path": remote,
        "total": len(entries),
        "overlayed_count": sum(item["overlay_changed"] is True for item in entries),
        "resources": [{key: value for key, value in item.items() if key != "resname"} for item in entries],
    }


def _getprop(serial: str, name: str) -> str:
    return _adb(serial, "shell", "getprop", name, timeout=15).strip()


def override_status(serial: str, entry_count: int = 0) -> dict[str, Any]:
    try:
        build_type = _getprop(serial, "ro.build.type")
    except Exception:
        return {"reachable": False, "applied_entry_count": entry_count}
    identity = _adb(serial, "shell", "id", timeout=15)
    mounts = _adb(serial, "shell", "mount", timeout=20)
    overlay_path = f"{_OVERLAY_DIR}/{_OVERLAY_APK}"
    probe = subprocess.run(
        ["adb", "-s", serial, "shell", "ls", overlay_path],
        capture_output=True, text=True, timeout=15, check=False,
    )
    product_lines = [line for line in mounts.splitlines() if " /product " in line]
    return {
        "reachable": True,
        "build_type": build_type,
        "is_userdebug": build_type in {"userdebug", "eng"},
        "verity_disabled": _getprop(serial, "ro.boot.veritymode") == "disabled",
        "rooted": "uid=0(" in identity or identity.startswith("uid=0"),
        "product_remountable": any("rw" in line.split()[-1].split(",") for line in product_lines),
        "overlay_installed": probe.returncode == 0,
        "overlay_apk_path": overlay_path if probe.returncode == 0 else "",
        "applied_entry_count": entry_count,
    }


def _render_resource(entry: dict[str, Any]) -> str:
    name = str(entry.get("resource_name") or "")
    resource_type = str(entry.get("resource_type") or "")
    value = str(entry.get("value") if entry.get("value") is not None else "")
    if not _RESOURCE_NAME_RE.fullmatch(name) or resource_type not in _RESOURCE_TYPES:
        raise ValueError("invalid config override entry")
    if resource_type in {"integer-array", "string-array", "array"}:
        items = "".join(f"<item>{html.escape(item)}</item>" for item in value.splitlines())
        return f'<{resource_type} name="{name}">{items}</{resource_type}>'
    return f'<{resource_type} name="{name}">{html.escape(value)}</{resource_type}>'


def _build_overlay(serial: str, entries: list[dict[str, Any]], target_package: str, directory: Path) -> Path:
    if not entries or len(entries) > 500:
        raise ValueError("config override requires 1-500 entries")
    aapt2 = _aapt2_path()
    _, symbols = _pull_package(serial, target_package, directory)
    values = directory / "res" / "values"
    values.mkdir(parents=True)
    (directory / "AndroidManifest.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android" '
        'package="com.rockchip.configoverrides">\n'
        f'<overlay android:targetPackage="{target_package}" android:isStatic="true" '
        'android:priority="999" />\n</manifest>\n', encoding="utf-8",
    )
    config_xml = '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n' + \
        "\n".join("  " + _render_resource(entry) for entry in entries) + "\n</resources>\n"
    (values / "config.xml").write_text(config_xml, encoding="utf-8")
    compiled = directory / "compiled.zip"
    output = directory / _OVERLAY_APK
    _run(
        [aapt2, "compile", "-o", str(compiled), "res/values/config.xml"],
        timeout=120,
        cwd=directory,
    )
    _run([
        aapt2, "link", "-o", str(output), "-I", str(symbols), "--manifest",
        str(directory / "AndroidManifest.xml"), "--rename-manifest-package",
        "com.rockchip.configoverrides", str(compiled),
    ], timeout=120, cwd=directory)
    return output


def _root_remount(serial: str) -> None:
    _adb(serial, "root", timeout=30)
    _adb(serial, "wait-for-device", timeout=45)
    _adb(serial, "remount", timeout=60)


def execute_override_action(action: str, serial: str, options: dict[str, Any]) -> dict[str, Any]:
    if action == "override_status":
        return override_status(serial, len(options.get("entries") or []))
    if action in {"override_disable_verity", "override_enable_verity"}:
        verb = "disable-verity" if action.endswith("disable_verity") else "enable-verity"
        output = _adb(serial, verb, timeout=30)
        lower = output.lower()
        return {
            "action": "disable" if action.endswith("disable_verity") else "enable",
            "message": output.strip() or f"{verb} completed",
            "needs_reboot": "reboot" in lower and "already" not in lower,
        }
    if action == "override_reboot":
        _adb(serial, "reboot", timeout=30)
        return {"status": "rebooting", "message": "设备正在重启", "rebooting": True}
    if action == "override_revert":
        _root_remount(serial)
        remote = f"{_OVERLAY_DIR}/{_OVERLAY_APK}"
        _adb(serial, "shell", "rm", "-f", remote, timeout=30)
        _adb(serial, "reboot", timeout=30)
        return {"status": "rebooting", "message": "已删除 overlay 并重启设备", "rebooting": True}
    if action != "override_apply":
        raise ValueError(f"unsupported override action: {action}")
    target = str(options.get("target_package") or "android")
    if not _PACKAGE_RE.fullmatch(target):
        raise ValueError("invalid override target package")
    with tempfile.TemporaryDirectory(prefix="gms-worker-overlay-") as directory:
        apk = _build_overlay(serial, list(options.get("entries") or []), target, Path(directory))
        _root_remount(serial)
        remote = f"{_OVERLAY_DIR}/{_OVERLAY_APK}"
        _adb(serial, "shell", "mkdir", "-p", _OVERLAY_DIR, timeout=30)
        _run(["adb", "-s", serial, "push", str(apk), remote], timeout=120)
        _adb(serial, "shell", "chcon", "u:object_r:system_file:s0", remote, timeout=30)
    _adb(serial, "reboot", timeout=30)
    return {
        "status": "rebooting", "message": "已编译并推送 overlay，设备正在重启",
        "apk_path": f"{_OVERLAY_DIR}/{_OVERLAY_APK}", "rebooting": True,
    }


def execute_inspection_action(action: str, serial: str, options: dict[str, Any]) -> dict[str, Any]:
    if action == "packages_with_path":
        rows = list_packages_with_path(serial)
        return {"rows": rows, "count": len(rows)}
    if action == "packages_all":
        packages = list_packages(serial)
        return {"packages": packages, "count": len(packages)}
    if action == "features":
        rows = list_features(serial)
        return {"rows": rows, "count": len(rows)}
    if action == "props":
        rows = list_properties(serial)
        return {"rows": rows, "count": len(rows)}
    if action == "config_explore":
        return explore_resources(serial, options)
    if action.startswith("override_"):
        return execute_override_action(action, serial, options)
    raise ValueError(f"unsupported inspection action: {action}")


def validate_export_path(path: str) -> str:
    value = str(path or "").strip()
    pure = PurePosixPath(value)
    if (
        not value.startswith(_EXPORT_PREFIXES)
        or pure.suffix.lower() not in {".apk", ".jar"}
        or ".." in pure.parts
        or len(value) > 1024
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("device export path is not an allow-listed APK/JAR path")
    return value


def prepare_device_export(payload: dict[str, Any]) -> Path:
    devices = list(payload.get("devices") or [])
    if len(devices) != 1:
        raise ValueError("device export requires exactly one device")
    serial = str(devices[0]).split(":", 1)[-1]
    # 读取设备文件前再次确认设备状态。
    from .inventory import probe_devices

    if serial not in {item["serial"] for item in probe_devices()}:
        raise ValueError("device is not attached to this Worker")
    remote = validate_export_path(str(payload.get("path") or ""))
    suffix = PurePosixPath(remote).suffix.lower()
    fd, filename = tempfile.mkstemp(prefix="gms-device-export-", suffix=suffix)
    os.close(fd)
    target = Path(filename)
    try:
        with target.open("wb") as output:
            completed = subprocess.run(
                ["adb", "-s", serial, "exec-out", "cat", remote],
                stdout=output, stderr=subprocess.PIPE, timeout=300, check=False,
            )
        if completed.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
            error = (completed.stderr or b"device export failed").decode(errors="replace")
            raise RuntimeError(error.strip())
        max_bytes = int(os.getenv("GMS_WORKER_DEVICE_EXPORT_MAX_BYTES", str(2 * 1024**3)))
        if target.stat().st_size > max_bytes:
            raise ValueError("device export exceeds Worker size limit")
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise
