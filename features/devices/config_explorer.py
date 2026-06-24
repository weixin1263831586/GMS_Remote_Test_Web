"""Android resource config explorer.

Lists framework (or any package) resources — especially ``config_*`` — and
reports both the value baked into the APK (default) and the value that takes
effect on the device after vendor overlays are applied (looked up via
``adb shell cmd overlay lookup``).

This powers the "配置资源查看器" tool card on the 常用工具 page.
"""

import logging
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field

from foundation.config import settings

from .network import run_local_shell_command


logger = logging.getLogger(__name__)

# Resource types we know how to read default + effective values for.
RESOURCE_TYPES = ("bool", "integer", "string", "dimen", "integer-array", "string-array", "array")

# Resource id line: "    resource 0x010e0000 integer/config_shortAnimTime PUBLIC"
_RESOURCE_RE = re.compile(
    r"^\s*resource\s+0x[0-9a-fA-F]+\s+([a-z-]+)/([A-Za-z0-9_]+)(?:\s+PUBLIC)?\s*$"
)
# Default-qualifier value line: '      () 200' / '      () "8.8.8.8"' / '      () (array) size=0'
_DEFAULT_VALUE_RE = re.compile(r"^\s*\(\)\s(.*)$")
# Resource-id reference emitted by `cmd overlay lookup` for unresolvable refs,
# e.g. "@17040222 ->" (a bare decimal resource id, optionally followed by an
# empty "->" tail). We can't map the id back to a name here, so overlay status
# is detected by source package (see _lookup_effective / explore) instead of
# string-equaling these against the named ref from aapt2.
_RESID_REF_RE = re.compile(r"^@0x[0-9a-fA-F]+\s*(?:->\s*)?$|^@\d+\s*(?:->\s*)?$")
# The "Best matching is from ... of <pkg>" marker that precedes the value in
# `cmd overlay lookup --verbose` output. Everything after this line is the value.
_BEST_MATCH_RE = re.compile(r"Best matching is from .*? of ([\w.]+)\s*$")

# Cache directory for pulled APKs to avoid re-pulling on every request.
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
    p = _find_binary("aapt2")
    if not p:
        raise RuntimeError("aapt2 not found on PATH (需要 Android platform-tools)")
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

    The file is cached so repeated lookups against the same package don't
    re-transfer ~37MB each time. Staleness is acceptable here — the APK on a
    given device rarely changes between reboots.
    """
    import os

    os.makedirs(_APK_CACHE_DIR, exist_ok=True)
    local_path = os.path.join(_APK_CACHE_DIR, f"{package}.apk")
    if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
        return local_path
    adb = _adb_path()
    serial = f"-s {shlex.quote(device_id)} " if device_id else ""
    stdout, stderr, code = run_local_shell_command(
        f"{adb} {serial}pull {shlex.quote(on_device_path)} {shlex.quote(local_path)}",
        timeout=120,
    )
    if code != 0 or not os.path.exists(local_path):
        raise RuntimeError(f"拉取 {on_device_path} 失败: {(stderr or stdout).strip()}")
    return local_path


def parse_apk_resources(apk_path: str) -> list[ResourceEntry]:
    """Parse ``aapt2 dump resources`` into ResourceEntry list with default values.

    Returns ALL resources (not just config_*); callers filter as needed.
    """
    aapt2 = _aapt2_path()
    # Run directly via subprocess (not shell) to avoid shell-quoting issues with paths.
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
    return entries


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


def _enabled_overlays(device_id: str | None) -> list[str] | None:
    """Return enabled overlay package names via ``cmd overlay list``.

    Each line looks like ``[x] com.android.vendor.overlay.foo`` where ``[x]``
    marks an enabled overlay and ``[ ]`` a disabled one. Returns the list of
    enabled overlay packages. Returns ``None`` when the command itself fails
    (so callers fall back to per-resource lookups rather than silently skipping
    them).
    """
    adb = _adb_path()
    serial = f"-s {shlex.quote(device_id)} " if device_id else ""
    stdout, _stderr, code = run_local_shell_command(
        f"{adb} {serial}shell cmd overlay list", timeout=15
    )
    if code != 0:
        return None
    enabled: list[str] = []
    for ln in stdout.splitlines():
        ln = ln.strip()
        # First whitespace-delimited token is the state tag: "[x]" (enabled),
        # "[ ]" (disabled), "---" (section separators), etc.
        parts = ln.split(None, 1)
        if len(parts) < 2 or not parts[0].startswith("[") or "x" not in parts[0].lower():
            continue
        enabled.append(parts[1].strip())
    return enabled


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
    # cmd overlay lookup prints the value on stdout; errors go to stderr + rc!=0.
    stdout, stderr, code = run_local_shell_command(
        f"{adb} {serial}shell cmd overlay lookup --verbose {shlex.quote(package)} {shlex.quote(resource)}",
        timeout=15,
    )
    if code != 0:
        entry.lookup_error = (stderr or stdout).strip() or "lookup failed"
        return
    # `cmd overlay lookup --verbose` output structure:
    #   <resolution trace lines>
    #   Best matching is from ... of <source>     <- source marker
    #   <value lines>                             <- the actual value (0+ lines)
    # The value lives AFTER the "Best matching" marker. Taking "the last
    # non-empty line" instead mislabeled empty-valued resources: their trailing
    # value line is blank, so the marker line itself was captured as the value.
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
    # `cmd overlay lookup` prints unresolvable resource references as a bare
    # numeric id (e.g. "@17040222 ->"), which is meaningless to a user. When the
    # default value is itself a named reference (e.g. "@string/default_browser"),
    # show that readable form instead of the raw id.
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
    """Main entry: list resources of a package.

    Args:
        package: Android package name, e.g. ``android`` (framework-res).
        device_id: Optional adb serial. If omitted, uses the default device.
        name_filter: Substring filter on resource name (case-insensitive).
        type_filter: Restrict to a single type (bool/integer/string/dimen/array).
        config_only: If True (default), only ``config_*`` resources.
        with_effective: If True, also compute overlay-effective values.
            Expensive: one adb call per resource, run concurrently.
        effective_limit: If >0 and with_effective, cap how many effective
            lookups are performed (after filtering). 0 = no cap (compute all).
        concurrency: Max parallel adb lookups when with_effective.
    """
    on_device_path = _resolve_package_apk(device_id, package)
    local_apk = _pull_apk(device_id, on_device_path, package)
    entries = parse_apk_resources(local_apk)
    entries = _filter_entries(entries, name_filter, type_filter, config_only)

    overlayed = 0
    if with_effective:
        targets = entries if effective_limit <= 0 else entries[:effective_limit]
        # Short-circuit: if `cmd overlay list` shows no enabled overlay on the
        # device, every resource's effective value equals its APK default, so we
        # can skip the expensive per-resource `cmd overlay lookup` calls
        # entirely (1600+ adb round-trips → 1). If the list call itself fails we
        # fall back to the full lookup path so results stay correct.
        enabled_overlays = _enabled_overlays(device_id)
        if enabled_overlays is not None and not enabled_overlays:
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
    # Core system packages most likely to carry framework-style config_* resources.
    # Not exhaustive — the UI also lets users type any package name directly.
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
        # format: /path/to.apk=package.name  (path may contain '='? no; '=' is the separator)
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
