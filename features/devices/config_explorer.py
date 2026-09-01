"""Android resource config explorer.

Lists framework (or any package) resources — especially ``config_*`` — and
reports both the value baked into the APK (default) and the value that takes
effect on the device after vendor overlays are applied (looked up via
``adb shell cmd overlay lookup``).

This powers the "配置资源查看器" tool card on the 常用工具 page.
"""

import hashlib
import logging
import os
import re
import shlex
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from foundation.config import settings

from .network import run_local_shell_command


logger = logging.getLogger(__name__)

# Resource types we know how to read default + effective values for.
RESOURCE_TYPES = ("bool", "integer", "string", "dimen", "integer-array", "string-array", "array")

# 匹配 aapt2 资源 ID 行。
_RESOURCE_RE = re.compile(
    r"^\s*resource\s+0x[0-9a-fA-F]+\s+([a-z-]+)/([A-Za-z0-9_]+)(?:\s+PUBLIC)?\s*$"
)
# 匹配默认限定符的值行。
_DEFAULT_VALUE_RE = re.compile(r"^\s*\(\)\s(.*)$")
# cmd overlay lookup 无法解析资源引用时返回的数字资源 ID。
_RESID_REF_RE = re.compile(r"^@0x[0-9a-fA-F]+\s*(?:->\s*)?$|^@\d+\s*(?:->\s*)?$")
# 有效值位于 Best matching 标记之后。
_BEST_MATCH_RE = re.compile(r"Best matching is from .*? of ([\w.]+)\s*$")

# 已拉取 APK 的缓存目录。
_APK_CACHE_DIR = str(settings.data_root / "config_explorer_cache")


@dataclass
class ResourceEntry:
    name: str           # full "type/name", e.g. "bool/config_supportsCamToggle"
    type: str           # "bool" / "integer" / "string" / "dimen" / "array" / ...
    resname: str        # bare name, e.g. "config_supportsCamToggle"
    default_value: str | None = None   # value from the APK (default config)
    effective_value: str | None = None # value after overlays (cmd overlay lookup)
    overlay_changed: bool | None = None  # True if effective != default
    overlay_source: str | None = None  # package that overlaid this resource (None if not overlaid)
    lookup_error: str | None = None


@dataclass
class ExploreResult:
    package: str
    apk_path: str
    total: int = 0
    overlayed_count: int = 0
    resources: list[dict] = field(default_factory=list)


def _find_binary(name: str) -> str | None:
    """Return the full path to a binary, or None if not on PATH."""
    return shutil.which(name)


def _adb_path() -> str:
    p = _find_binary("adb")
    if not p:
        raise RuntimeError("adb not found on PATH (需要 Android platform-tools)")
    return p


def _aapt2_path() -> str:
    configured = os.getenv("GMS_AAPT2_PATH", "").strip()
    candidates = [configured, _find_binary("aapt2") or ""]
    candidates.extend(
        str(path)
        for path in sorted(
            Path("/usr/lib/android-sdk/build-tools").glob("*/aapt2"),
            reverse=True,
        )
    )
    p = next(
        (
            value
            for value in candidates
            if value and os.path.isfile(value) and os.access(value, os.X_OK)
        ),
        "",
    )
    if not p:
        raise RuntimeError(
            "aapt2 not found (需要单独安装 Android Build-Tools 或配置 GMS_AAPT2_PATH)"
        )
    return p


def _resolve_package_apk(device_id: str | None, package: str) -> str:
    """Return the on-device APK path for ``package`` (first split if multiple)."""
    adb = _adb_path()
    serial = f"-s {shlex.quote(device_id)} " if device_id else ""
    stdout, stderr, code = run_local_shell_command(
        f"{adb} {serial}shell pm path {shlex.quote(package)}", timeout=20
    )
    if code != 0 or not stdout.strip():
        raise RuntimeError(
            f"无法获取包 {package} 的 apk 路径: {(stderr or stdout).strip() or 'device offline?'}"
        )
    first = stdout.strip().splitlines()[0].strip()
    # Output looks like "package:/system/framework/framework-res.apk"
    if not first.startswith("package:"):
        raise RuntimeError(f"无法解析 pm path 输出: {first}")
    return first[len("package:"):]


def _pull_apk(device_id: str | None, on_device_path: str, package: str) -> str:
    """Pull the APK into a cache dir keyed by package; returns local path.

    The file is cached so repeated lookups against the same device build and
    package don't re-transfer ~37MB each time. The cache identity includes the
    device, build fingerprint and remote APK metadata; package-only caching can
    return another device's framework resources in mixed-build labs.
    """
    import os

    os.makedirs(_APK_CACHE_DIR, exist_ok=True)
    adb = _adb_path()
    serial = f"-s {shlex.quote(device_id)} " if device_id else ""
    fingerprint, _, fingerprint_code = run_local_shell_command(
        f"{adb} {serial}shell getprop ro.build.fingerprint", timeout=10
    )
    metadata, _, metadata_code = run_local_shell_command(
        f"{adb} {serial}shell stat -c %s:%Y {shlex.quote(on_device_path)}",
        timeout=10,
    )
    identity = "\0".join(
        (
            device_id or "default",
            package,
            on_device_path,
            fingerprint.strip() if fingerprint_code == 0 else "",
            metadata.strip() if metadata_code == 0 else "",
        )
    )
    cache_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    local_path = os.path.join(_APK_CACHE_DIR, f"{cache_key}.apk")
    if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
        return local_path
    # Pull to a unique temporary file and publish atomically. An interrupted
    # adb transfer may leave a non-empty partial file; treating that as a cache
    # hit on the next request would make Device Info parse the wrong contents.
    temp_path = os.path.join(
        _APK_CACHE_DIR, f".{cache_key}.{uuid.uuid4().hex}.part"
    )
    try:
        stdout, stderr, code = run_local_shell_command(
            f"{adb} {serial}pull {shlex.quote(on_device_path)} {shlex.quote(temp_path)}",
            timeout=120,
        )
        if (
            code != 0
            or not os.path.exists(temp_path)
            or os.path.getsize(temp_path) <= 0
        ):
            raise RuntimeError(
                f"拉取 {on_device_path} 失败: {(stderr or stdout).strip()}"
            )
        os.replace(temp_path, local_path)
    finally:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass
    return local_path


@lru_cache(maxsize=32)
def _parse_apk_resource_records(
    apk_path: str,
) -> tuple[tuple[str, str, str, str | None], ...]:
    """Parse one content-addressed APK into immutable resource records.

    The APK cache path already includes device/build/file identity. Keeping the
    expensive aapt2 result in a small process cache makes subsequent filters
    instant while immutable records prevent overlay lookups mutating another
    request's result.
    """
    aapt2 = _aapt2_path()
    # 直接执行子进程，避免 Shell 路径转义问题。
    try:
        proc = subprocess.run(
            [aapt2, "dump", "resources", apk_path],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("aapt2 dump 超时") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"aapt2 dump 失败: {proc.stderr.strip()}")

    entries: list[ResourceEntry] = []
    current: ResourceEntry | None = None
    array_lines: list[str] = []
    collecting_array = False
    for line in proc.stdout.splitlines():
        m = _RESOURCE_RE.match(line)
        if m:
            if current is not None:
                entries.append(current)
            rtype, rname = m.group(1), m.group(2)
            current = ResourceEntry(name=f"{rtype}/{rname}", type=rtype, resname=rname)
            array_lines = []
            collecting_array = False
            continue
        if current is not None and collecting_array:
            array_lines.append(line.strip())
            if line.strip().endswith("]"):
                current.default_value = " ".join(array_lines)
                array_lines = []
                collecting_array = False
            continue
        if current is not None and current.default_value is None:
            vm = _DEFAULT_VALUE_RE.match(line)
            if vm:
                value = vm.group(1).strip()
                if value.startswith("(array)"):
                    collecting_array = True
                else:
                    current.default_value = value
    if current is not None:
        entries.append(current)
    return tuple(
        (entry.name, entry.type, entry.resname, entry.default_value)
        for entry in entries
    )


def parse_apk_resources(apk_path: str) -> list[ResourceEntry]:
    """Return fresh mutable entries for all resources in ``apk_path``."""
    return [
        ResourceEntry(
            name=name,
            type=resource_type,
            resname=resname,
            default_value=default_value,
        )
        for name, resource_type, resname, default_value
        in _parse_apk_resource_records(apk_path)
    ]


def _filter_entries(
    entries: list[ResourceEntry],
    name_filter: str | None,
    type_filter: str | None,
    config_only: bool,
) -> list[ResourceEntry]:
    result = entries
    if config_only:
        result = [e for e in result if e.resname.startswith("config_")]
    if type_filter:
        tf = type_filter.lower().strip()
        result = [e for e in result if e.type == tf]
    if name_filter:
        nf = name_filter.lower().strip()
        result = [e for e in result if nf in e.resname.lower()]
    return result


def _enabled_overlays_by_target(device_id: str | None) -> dict[str, list[str]] | None:
    """Return enabled overlay package names grouped by target package.

    ``cmd overlay list`` prints target-package section headers followed by
    ``[x]`` / ``[ ]`` overlay rows. Grouping lets us skip expensive
    per-resource lookups when the current target package has no enabled RROs,
    even if other packages on the device do.
    """
    adb = _adb_path()
    serial = f"-s {shlex.quote(device_id)} " if device_id else ""
    stdout, _stderr, code = run_local_shell_command(
        f"{adb} {serial}shell cmd overlay list", timeout=15
    )
    if code != 0:
        return None
    by_target: dict[str, list[str]] = {}
    current_target = ""
    for ln in stdout.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        # First whitespace-delimited token is the state tag: "[x]" (enabled),
        # "[ ]" (disabled), "---" (section separators), etc.
        parts = ln.split(None, 1)
        if len(parts) < 2 or not parts[0].startswith("["):
            if not ln.startswith("---"):
                current_target = ln
                by_target.setdefault(current_target, [])
            continue
        if "x" in parts[0].lower() and current_target:
            by_target.setdefault(current_target, []).append(parts[1].strip())
    return by_target


def _enabled_overlays(device_id: str | None) -> list[str] | None:
    by_target = _enabled_overlays_by_target(device_id)
    if by_target is None:
        return None
    return [overlay for overlays in by_target.values() for overlay in overlays]


def _lookup_effective(
    device_id: str | None, package: str, entry: ResourceEntry
) -> None:
    """Fill in entry.effective_value + overlay_source via `cmd overlay lookup`.

    Uses --verbose so the trailing "Best matching is from default configuration
    of <pkg>" line tells us which overlay package supplied the value. When the
    source equals the target package (e.g. ``android``), the resource was NOT
    overlaid.
    """
    adb = _adb_path()
    serial = f"-s {shlex.quote(device_id)} " if device_id else ""
    resource = f"{package}:{entry.type}/{entry.resname}"
    # 有效值写入 stdout，错误写入 stderr 并返回非零状态。
    stdout, stderr, code = run_local_shell_command(
        f"{adb} {serial}shell cmd overlay lookup --verbose {shlex.quote(package)} {shlex.quote(resource)}",
        timeout=15,
    )
    if code != 0:
        entry.lookup_error = (stderr or stdout).strip() or "lookup failed"
        return
    # 仅采集 Best matching 标记后的有效值行。
    source_pkg = None
    value_lines: list[str] = []
    seen_marker = False
    for ln in stdout.splitlines():
        if not seen_marker:
            m = _BEST_MATCH_RE.search(ln)
            if m:
                src = m.group(1).strip()
                # Source == target package => not overlaid.
                source_pkg = None if src == package else src
                seen_marker = True
            continue
        if ln.strip():
            value_lines.append(ln.strip())
    value = value_lines[-1] if value_lines else ""
    # 数字资源 ID 无法展示时回退到可读的默认引用。
    if value and _RESID_REF_RE.match(value) and entry.default_value:
        value = entry.default_value
    if entry.type in {"array", "integer-array", "string-array"} and value:
        items = [line.strip() for line in value.splitlines() if line.strip()]
        if not (len(items) == 1 and items[0].startswith("[")):
            value = f"[{', '.join(items)}]"
    entry.effective_value = value
    entry.overlay_source = source_pkg


def explore(
    package: str = "android",
    device_id: str | None = None,
    name_filter: str | None = None,
    type_filter: str | None = None,
    config_only: bool = True,
    with_effective: bool = False,
    effective_limit: int = 0,
    concurrency: int = 8,
) -> ExploreResult:
    """按条件列出包资源，并可并发查询 Overlay 生效值。"""
    on_device_path = _resolve_package_apk(device_id, package)
    local_apk = _pull_apk(device_id, on_device_path, package)
    entries = parse_apk_resources(local_apk)
    entries = _filter_entries(entries, name_filter, type_filter, config_only)

    overlayed = 0
    if with_effective:
        targets = entries if effective_limit <= 0 else entries[:effective_limit]
        # 无启用 Overlay 时直接使用默认值，避免逐资源查询。
        enabled_by_target = _enabled_overlays_by_target(device_id)
        target_overlays = None if enabled_by_target is None else enabled_by_target.get(package, [])
        if target_overlays is not None and not target_overlays:
            for e in targets:
                e.effective_value = e.default_value
                e.overlay_source = None
                e.overlay_changed = False
        else:
            # Run adb lookups concurrently to keep full-table queries (1600+ rows)
            # tractable. Each lookup is an independent `cmd overlay lookup` call.
            from concurrent.futures import ThreadPoolExecutor

            workers = max(1, min(concurrency, 16))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(
                    lambda e: _lookup_effective(device_id, package, e),
                    targets,
                ))
            for e in targets:
                # Authoritative overlay signal: `cmd overlay lookup --verbose`
                # reports which package supplied the value. A non-None source
                # (different from the target package) means an overlay overrode
                # it. Relying on string comparison is unreliable because aapt2's
                # default (quoted literal / named ref) and lookup's effective
                # value (bare literal / numeric id) are different surface forms
                # of the same value, so equal values compared unequal and every
                # row was mislabeled "已修改".
                if e.overlay_source is not None:
                    e.overlay_changed = True
                    overlayed += 1
                elif e.lookup_error is None:
                    e.overlay_changed = False
                else:
                    e.overlay_changed = None

    result = ExploreResult(package=package, apk_path=on_device_path)
    result.total = len(entries)
    result.overlayed_count = overlayed
    result.resources = [
        {
            "name": e.name,
            "type": e.type,
            "default_value": e.default_value,
            "effective_value": e.effective_value,
            "overlay_changed": e.overlay_changed,
            "overlay_source": e.overlay_source,
            "lookup_error": e.lookup_error,
        }
        for e in entries
    ]
    return result


def list_packages(device_id: str | None = None) -> list[str]:
    """Return candidate packages known to carry framework-style config resources.

    We don't enumerate every installed package (thousands); instead we offer a
    curated set that typically contain ``config_*`` resources, plus verify each
    resolves to an APK via ``pm path``.
    """
    # 常见的 framework config 资源包，界面仍支持输入任意包名。
    candidates = [
        "android",
        "com.android.systemui",
        "com.android.providers.settings",
        "com.android.providers.telephony",
        "com.android.settings",
        "com.android.phone",
        "com.android.wifi",
        "com.android.networkstack",
        "com.android.connectivity.resources",
        "com.android.bluetooth",
        "com.android.hotspot",
        "com.android.cellbroadcastreceiver",
        "com.android.cellbroadcastservice",
        "com.android.server.telecom",
        "com.android.dreams.basic",
        "com.android.wallpaperbackup",
        "com.android.inputmethod.latin",
        "com.android.contacts",
        "com.android.deskclock",
        "com.android.camera",
    ]
    adb = _adb_path()
    serial = f"-s {shlex.quote(device_id)} " if device_id else ""
    valid: list[str] = []
    for pkg in candidates:
        stdout, _, code = run_local_shell_command(
            f"{adb} {serial}shell pm path {shlex.quote(pkg)}", timeout=10
        )
        if code == 0 and stdout.strip().startswith("package:"):
            valid.append(pkg)
    return valid


def list_all_packages(device_id: str | None = None) -> list[str]:
    """Return every package installed on the device via ``pm list packages``.

    Used to populate a complete, type-ahead package picker in the UI (the
    curated ``list_packages`` only covers known config-bearing packages).
    """
    adb = _adb_path()
    serial = f"-s {shlex.quote(device_id)} " if device_id else ""
    stdout, _, code = run_local_shell_command(
        f"{adb} {serial}shell pm list packages", timeout=30
    )
    if code != 0:
        return []
    pkgs: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            pkgs.append(line[len("package:"):].strip())
    pkgs.sort()
    return pkgs


def pull_device_file(
    device_id: str | None, on_device_path: str, local_path: str, timeout: int = 120
) -> None:
    """Pull a file off the device into ``local_path`` (creating parent dirs).

    Used to fetch an APK for host-side decompilation. Raises RuntimeError on
    failure.
    """
    import os

    os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
    adb = _adb_path()
    serial = f"-s {shlex.quote(device_id)} " if device_id else ""
    stdout, stderr, code = run_local_shell_command(
        f"{adb} {serial}pull {shlex.quote(on_device_path)} {shlex.quote(local_path)}",
        timeout=timeout,
    )
    if code != 0 or not os.path.exists(local_path):
        raise RuntimeError(f"拉取 {on_device_path} 失败: {(stderr or stdout).strip()}")


def list_devices() -> list[dict[str, str]]:
    """Return currently attached adb devices as [{serial, state}]."""
    adb = _adb_path()
    stdout, _, code = run_local_shell_command(f"{adb} devices", timeout=10)
    devices: list[dict[str, str]] = []
    if code != 0:
        return devices
    for line in stdout.splitlines()[1:]:
        line = line.strip()
        if not line or "\t" not in line:
            continue
        serial, state = line.split("\t", 1)
        if serial.strip().startswith("localhost:"):
            continue
        devices.append({"serial": serial.strip(), "state": state.strip()})
    return devices


def _pm_list(device_id: str | None, args: str, timeout: int = 60) -> str:
    """Run ``adb shell pm list <args>`` and return raw stdout."""
    adb = _adb_path()
    serial = f"-s {shlex.quote(device_id)} " if device_id else ""
    stdout, stderr, code = run_local_shell_command(
        f"{adb} {serial}shell pm list {args}", timeout=timeout
    )
    if code != 0:
        raise RuntimeError(f"pm list {args} 失败: {(stderr or stdout).strip() or 'device offline?'}")
    return stdout


def list_packages_with_path(device_id: str | None = None) -> list[dict[str, str]]:
    """``pm list packages -f`` → [{path, package}]."""
    out = _pm_list(device_id, "packages -f")
    rows: list[dict[str, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        body = line[len("package:"):]
        # 输出格式为 APK 路径和包名，以等号分隔。
        if "=" in body:
            path, pkg = body.rsplit("=", 1)
        else:
            path, pkg = body, ""
        rows.append({"path": path.strip(), "package": pkg.strip()})
    return rows


def list_features(device_id: str | None = None) -> list[dict[str, str]]:
    """``pm list features`` → [{name, version?}]."""
    out = _pm_list(device_id, "features")
    rows: list[dict[str, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("feature:"):
            continue
        body = line[len("feature:"):]
        if "=" in body:
            name, version = body.split("=", 1)
            rows.append({"name": name.strip(), "version": version.strip()})
        else:
            rows.append({"name": body.strip(), "version": ""})
    rows.sort(key=lambda r: r["name"])
    return rows


def list_props(device_id: str | None = None) -> list[dict[str, str]]:
    """``getprop`` → [{name, value}], sorted by name.

    Each getprop line looks like ``[ro.build.version.release]: [14]``. We strip
    the brackets and skip empty values.
    """
    adb = _adb_path()
    serial = f"-s {shlex.quote(device_id)} " if device_id else ""
    stdout, stderr, code = run_local_shell_command(
        f"{adb} {serial}shell getprop", timeout=30
    )
    if code != 0:
        raise RuntimeError(f"getprop 失败: {(stderr or stdout).strip() or 'device offline?'}")
    rows: list[dict[str, str]] = []
    # Match "[name]: [value]" — value may itself be empty "[name]: []".
    prop_re = re.compile(r"^\[([^\]]*)\]:\s*\[([^\]]*)\]\s*$")
    for line in stdout.splitlines():
        m = prop_re.match(line.strip())
        if m:
            name = m.group(1).strip()
            value = m.group(2).strip()
            if name:
                rows.append({"name": name, "value": value})
    rows.sort(key=lambda r: r["name"])
    return rows
