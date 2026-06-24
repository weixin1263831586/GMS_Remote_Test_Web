"""Config override via Runtime Resource Overlay (RRO).

Builds a single static overlay APK that re-defines selected ``config_*``
resources of the ``android`` (framework-res) package, pushes it to the device's
trusted overlay path (``/product/overlay``), and reboots so the framework
registers it — overriding the framework defaults at runtime **without
recompiling or reflashing firmware**.

Validated technique (manual run on a userdebug + unlocked device):
    1. aapt2 build a tiny overlay APK from a manifest + res/values/config.xml
    2. adb disable-verity (once) → reboot → adb root → adb remount /product
    3. adb push Overlay.apk /product/overlay/ + chcon system_file:s0
    4. adb reboot → framework applies the static overlay (high priority)

This module is the host-side logic + orchestration. ``config_override_api.py``
exposes it over HTTP; the device-config modal UI consumes that API.

State model: ONE overlay APK on the device = a snapshot of ALL current
overrides. The host store (``config_overrides.json``) is the source of truth,
keyed by resource name (de-duplicated). ``apply()`` rebuilds the whole APK from
the full entry set → push → reboot; idempotent. ``revert()`` deletes the APK →
reboot.
"""

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import xml.sax.saxutils as _saxutils
from dataclasses import asdict, dataclass
from pathlib import Path

from foundation.config import settings

from .adb_ops import mount_point_is_rw, reboot_with_runner, root_and_remount
from .config_explorer import _APK_CACHE_DIR, _aapt2_path, _adb_path, _pull_apk, _resolve_package_apk
from .network import run_local_shell_command


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Resource types we can override. Mirrors config_explorer.RESOURCE_TYPES plus
# fraction (a numeric-with-suffix type that framework-res config_* also uses).
ALLOWED_TYPES = (
    "string", "bool", "integer", "dimen", "fraction",
    "integer-array", "string-array", "array",
)
_SCALAR_TYPES = ("string", "bool", "integer", "dimen", "fraction")
_ARRAY_TYPES = ("integer-array", "string-array", "array")

# Android dimension units (resource type "dimen").
_DIMEN_UNITS = ("dp", "dip", "sp", "sip", "px", "in", "mm", "pt")
# Fraction suffixes.
_FRACTION_SUFFIXES = ("%", "%p")

# v1 target scope: only the framework package. The state schema carries
# target_package per entry and the manifest builder accepts it, so v2
# multi-package grouping is a small change. See apply_overrides.
DEFAULT_TARGET_PACKAGE = "android"

# The single overlay package that carries every override.
OVERLAY_PACKAGE = "com.gms.configoverrides"
OVERLAY_PRIORITY = 9999
# File name on /product/overlay (and our host build dir).
OVERLAY_APK_NAME = "GmsConfigOverrides.apk"

# Trusted overlay path on the device. Some userdebug builds use
# /system/product/overlay; we probe and create as needed in apply().
OVERLAY_DIR = "/product/overlay"

# Host-side persistent store of pending/applied overrides.
_STORE_PATH = Path(settings.data_root) / "config_overrides.json"

# Resource name must match a PUBLIC framework resource: letters, digits, _, . -
_RESOURCE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class OverrideEntry:
    """One override: redefine resource ``resource_name`` (of ``resource_type``)
    to ``value``. ``value`` is the raw user string; validation normalizes it."""
    resource_name: str
    resource_type: str
    value: str
    target_package: str = DEFAULT_TARGET_PACKAGE
    updated_at: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "OverrideEntry":
        return cls(
            resource_name=d["resource_name"],
            resource_type=d["resource_type"],
            value=d.get("value", ""),
            target_package=d.get("target_package", DEFAULT_TARGET_PACKAGE),
            updated_at=d.get("updated_at", ""),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OverrideStatus:
    """Read-only snapshot of the device's override-readiness."""
    reachable: bool = False
    build_type: str | None = None
    is_userdebug: bool = False
    verity_disabled: bool | None = None
    rooted: bool | None = None
    product_remountable: bool | None = None
    overlay_installed: bool = False
    overlay_apk_path: str | None = None
    applied_entry_count: int | None = None


@dataclass
class ApplyResult:
    """Outcome of apply()/revert(). ``rebooting`` means the device is rebooting
    when the call returned (the HTTP request returns before reconnect)."""
    success: bool
    stage: str
    message: str
    apk_path: str | None = None
    rebooting: bool = False


# ---------------------------------------------------------------------------
# Validation & parsing (pure functions — unit-tested, no I/O)
# ---------------------------------------------------------------------------

def _escape(value: str) -> str:
    """XML-escape a user value for safe insertion into res/values/config.xml."""
    return _saxutils.escape(value, {'"': "&quot;"})


def _split_array_items(raw: str) -> list[str]:
    """Split raw array input into items.

    Items are newline-separated (NOT comma — string-array items commonly
    contain commas, and comma-separation would force fragile escaping). Splits
    on \\n / \\r\\n, strips per-item trailing whitespace, keeps deliberate empty
    items, and drops the single trailing empty line produced by a final newline.
    """
    if raw is None:
        return []
    # Normalize CRLF → LF, then split.
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    items = text.split("\n")
    # Drop a single trailing empty line from a final newline.
    if items and items[-1] == "":
        items = items[:-1]
    return [it.rstrip() for it in items]


def _parse_number_unit(value: str, allowed_units: tuple[str, ...], type_label: str) -> str:
    """Validate a "<number><unit>" value (dimen/fraction). Returns the value
    unchanged on success, raises ValueError otherwise."""
    v = value.strip()
    for unit in sorted(allowed_units, key=len, reverse=True):
        if v.endswith(unit):
            num = v[: -len(unit)].strip()
            if not num:
                raise ValueError(f"{type_label} 值缺少数字: {value!r}")
            try:
                float(num)
            except ValueError:
                raise ValueError(f"{type_label} 值的数字部分无效: {num!r}") from None
            return v
    raise ValueError(
        f"{type_label} 值必须以单位结尾({'/'.join(allowed_units)}): {value!r}"
    )


def validate_override(rtype: str, value: str) -> str:
    """Validate a user-supplied raw string value for resource type ``rtype``.

    Returns the normalized value on success; raises ``ValueError`` with a
    Chinese message on failure.

      string:        any text (escaping done at render time).
      bool:          'true'/'false' (case-insensitive) → lowercase.
      integer:       base-10 integer (rejects hex/float).
      dimen:         '<float><unit>', unit in _DIMEN_UNITS.
      fraction:      '<float><suffix>', suffix '%' or '%p'.
      integer-array: newline-separated items, each a base-10 integer.
      string-array:  newline-separated items (any text).
      array:         newline-separated items (any text; generic TypedArray).
    """
    if rtype not in ALLOWED_TYPES:
        raise ValueError(f"不支持的资源类型: {rtype}")
    if value is None:
        raise ValueError("值不能为空")

    if rtype == "string":
        return value
    if rtype == "bool":
        v = value.strip().lower()
        if v not in ("true", "false"):
            raise ValueError(f"bool 值必须为 true/false: {value!r}")
        return v
    if rtype == "integer":
        v = value.strip()
        if not re.fullmatch(r"-?\d+", v):
            raise ValueError(f"integer 值必须是十进制整数: {value!r}")
        return v
    if rtype == "dimen":
        return _parse_number_unit(value, _DIMEN_UNITS, "dimen")
    if rtype == "fraction":
        return _parse_number_unit(value, _FRACTION_SUFFIXES, "fraction")
    # array types
    items = _split_array_items(value)
    if rtype == "integer-array":
        for it in items:
            if not re.fullmatch(r"-?\d+", it):
                raise ValueError(f"integer-array 的每项必须是整数: {it!r}")
        return "\n".join(items)
    # string-array / array: keep raw (normalized line endings)
    return "\n".join(items)


# ---------------------------------------------------------------------------
# RRO XML / APK generation (pure-ish; build_overlay_apk runs aapt2)
# ---------------------------------------------------------------------------

def render_resource_xml(rtype: str, name: str, value: str) -> str:
    """Return one ``<...>`` element for res/values/config.xml.

    ``name`` is the bare resource name; it MUST match a PUBLIC resource id in
    the -I symbol APK or ``aapt2 link`` fails. User values are XML-escaped.
    """
    if rtype == "string":
        return f'<string name="{name}">{_escape(value)}</string>'
    if rtype == "bool":
        return f'<bool name="{name}">{value}</bool>'
    if rtype == "integer":
        return f'<integer name="{name}">{value}</integer>'
    if rtype == "dimen":
        return f'<dimen name="{name}">{_escape(value)}</dimen>'
    if rtype == "fraction":
        return f'<fraction name="{name}">{value}</fraction>'
    if rtype in _ARRAY_TYPES:
        items = _split_array_items(value)
        inner = "".join(f"<item>{_escape(it)}</item>" for it in items)
        return f'<{rtype} name="{name}">{inner}</{rtype}>'
    raise ValueError(f"不支持的资源类型: {rtype}")


def build_config_xml(entries: list[OverrideEntry]) -> str:
    """Assemble res/values/config.xml from validated entries.

    Duplicate resource names are not de-duped here (the store keys by name, so
    duplicates cannot reach this function) — but we assert defensively.
    """
    seen: set[str] = set()
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<resources>"]
    for e in entries:
        if e.resource_name in seen:
            raise ValueError(f"重复的资源名: {e.resource_name}")
        seen.add(e.resource_name)
        lines.append(
            "  " + render_resource_xml(e.resource_type, e.resource_name, e.value)
        )
    lines.append("</resources>")
    return "\n".join(lines) + "\n"


def build_manifest(
    target_package: str = DEFAULT_TARGET_PACKAGE,
    overlay_package: str = OVERLAY_PACKAGE,
    priority: int = OVERLAY_PRIORITY,
) -> str:
    """Static overlay manifest. ``targetName`` is intentionally OMITTED — it is
    optional and was unnecessary in the validated run."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"\n'
        f'    package="{overlay_package}">\n'
        '    <overlay android:targetPackage="{target}"\n'
        '             android:isStatic="true"\n'
        '             android:priority="{priority}" />\n'
        '</manifest>\n'
    ).format(target=target_package, priority=priority)


def resolve_symbol_apk(device_id: str | None, target_package: str) -> str:
    """Return a LOCAL path to the symbol APK to pass as ``aapt2 -I``.

    The overlay references framework resource ids by name, so aapt2 link needs
    the target package's APK as the symbol table. For ``android`` this reuses
    the config_explorer cache (``config_explorer_cache/android.apk``); for other
    packages it pulls (and caches) the on-device APK.
    """
    if target_package == DEFAULT_TARGET_PACKAGE:
        cached = os.path.join(_APK_CACHE_DIR, f"{target_package}.apk")
        if os.path.exists(cached) and os.path.getsize(cached) > 0:
            return cached
    # Pull (caches under config_explorer_cache/<pkg>.apk via _pull_apk).
    on_device = _resolve_package_apk(device_id, target_package)
    return _pull_apk(device_id, on_device, target_package)


def build_overlay_apk(
    entries: list[OverrideEntry],
    work_dir: str,
    symbol_apk: str,
    target_package: str = DEFAULT_TARGET_PACKAGE,
    overlay_package: str = OVERLAY_PACKAGE,
) -> str:
    """Build the overlay APK into ``work_dir`` via aapt2 compile + link.

    Uses ``subprocess.run`` with arg lists (NOT shell) — mirrors
    config_explorer.parse_apk_resources, to avoid shell-quoting issues with the
    APK paths. Raises RuntimeError (with stderr) on any non-zero step, BEFORE
    any device push — so a build failure never leaves the device half-applied.
    Returns the path to ``work_dir/Overlay.apk``.
    """
    aapt2 = _aapt2_path()
    os.makedirs(work_dir, exist_ok=True)
    res_dir = os.path.join(work_dir, "res", "values")
    os.makedirs(res_dir, exist_ok=True)
    with open(os.path.join(work_dir, "AndroidManifest.xml"), "w", encoding="utf-8") as f:
        f.write(build_manifest(target_package, overlay_package))
    with open(os.path.join(res_dir, "config.xml"), "w", encoding="utf-8") as f:
        f.write(build_config_xml(entries))

    out_apk = os.path.join(work_dir, "Overlay.apk")
    compiled = os.path.join(work_dir, "compiled.zip")

    # compile (cwd=work_dir: aapt2 records res/values/config.xml into the zip)
    proc = subprocess.run(
        [aapt2, "compile", "-o", compiled, "res/values/config.xml"],
        cwd=work_dir, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"aapt2 compile 失败: {proc.stderr.strip()}")

    # link: -I provides the symbol table so overlay refs resolve to PUBLIC ids.
    proc = subprocess.run(
        [
            aapt2, "link",
            "-o", out_apk,
            "-I", symbol_apk,
            "--manifest", os.path.join(work_dir, "AndroidManifest.xml"),
            "--rename-manifest-package", overlay_package,
            compiled,
        ],
        cwd=work_dir, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 or not os.path.exists(out_apk):
        raise RuntimeError(f"aapt2 link 失败: {proc.stderr.strip() or proc.stdout.strip()}")
    return out_apk


# ---------------------------------------------------------------------------
# Persistent store
# ---------------------------------------------------------------------------

class OverrideStore:
    """JSON-backed store of overrides, per device.

    Empty ``device_id`` serializes under the ``_default`` key (mirrors the
    ``device_id or None`` convention used elsewhere). Writes are atomic:
    tmp file + ``os.replace`` so a crash mid-write cannot corrupt the store.
    """

    def __init__(self, path=_STORE_PATH) -> None:
        self.path = Path(path)

    # -- internals --
    def _load(self) -> dict:
        if not self.path.exists():
            return {"schema_version": 1, "device_overrides": {}}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            logger.warning("config_overrides.json 损坏，重置为空")
            return {"schema_version": 1, "device_overrides": {}}
        data.setdefault("schema_version", 1)
        data.setdefault("device_overrides", {})
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(self.path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    @staticmethod
    def _device_key(device_id: str | None) -> str:
        return device_id if device_id else "_default"

    def _device_block(self, data: dict, device_id: str | None) -> dict:
        key = self._device_key(device_id)
        return data["device_overrides"].setdefault(
            key,
            {
                "target_package": DEFAULT_TARGET_PACKAGE,
                "overlay_package": OVERLAY_PACKAGE,
                "entries": {},
            },
        )

    # -- public API --
    def list_entries(self, device_id: str | None) -> list[OverrideEntry]:
        data = self._load()
        block = data["device_overrides"].get(self._device_key(device_id))
        if not block:
            return []
        return [OverrideEntry.from_dict(d) for d in block.get("entries", {}).values()]

    def upsert(self, device_id: str | None, entry: OverrideEntry) -> None:
        """Validate then insert/update by resource name. Raises ValueError on
        bad name or value."""
        if not entry.resource_name or not _RESOURCE_NAME_RE.match(entry.resource_name):
            raise ValueError(f"非法的资源名: {entry.resource_name!r}")
        entry.value = validate_override(entry.resource_type, entry.value)
        data = self._load()
        block = self._device_block(data, device_id)
        entry.updated_at = _now_iso()
        block["entries"][entry.resource_name] = entry.to_dict()
        self._save(data)

    def remove(self, device_id: str | None, resource_name: str) -> bool:
        data = self._load()
        block = data["device_overrides"].get(self._device_key(device_id))
        if not block or resource_name not in block.get("entries", {}):
            return False
        del block["entries"][resource_name]
        self._save(data)
        return True

    def clear(self, device_id: str | None) -> int:
        """Clear the HOST store for a device (does NOT touch the device)."""
        data = self._load()
        block = data["device_overrides"].get(self._device_key(device_id))
        if not block:
            return 0
        count = len(block.get("entries", {}))
        block["entries"] = {}
        self._save(data)
        return count


def _now_iso() -> str:
    """ISO8601 timestamp. Uses datetime (not available in workflow scripts, but
    this is a normal module)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Device probing (read-only — never disable-verity / reboot / push)
# ---------------------------------------------------------------------------

def _run_adb(device_id: str | None, args: str, timeout: int = 15) -> tuple[str, int]:
    """Run an adb command on the host; return (combined output, return code)."""
    adb = _adb_path()
    serial = f"-s {shlex.quote(device_id)} " if device_id else ""
    stdout, stderr, code = run_local_shell_command(
        f"{adb} {serial}{args}", timeout=timeout
    )
    return ((stdout or "") + (stderr or "")), code


def _run_adb_for_ops(device_id: str | None, args: str, timeout: int) -> tuple[str, int]:
    return _run_adb(device_id, args, timeout)


def _getprop(device_id: str | None, name: str) -> str:
    out, _ = _run_adb(device_id, f"shell getprop {shlex.quote(name)}", timeout=10)
    return out.strip()


def probe_status(device_id: str | None) -> OverrideStatus:
    """Read-only snapshot of override-readiness. Never mutates the device."""
    status = OverrideStatus()
    # Reachability: a cheap getprop.
    build_type = _getprop(device_id, "ro.build.type")
    if not build_type:
        return status  # device unreachable
    status.reachable = True
    status.build_type = build_type
    status.is_userdebug = build_type in ("userdebug", "eng")

    verity = _getprop(device_id, "ro.boot.veritymode")
    status.verity_disabled = verity == "disabled"

    # Read-only probes only. Do not run `adb root` or `adb remount` here; opening
    # the status tab must not restart adbd or mutate mount state.
    id_out, id_code = _run_adb(device_id, "shell id", timeout=10)
    status.rooted = id_code == 0 and ("uid=0(" in id_out or id_out.startswith("uid=0"))

    mount_out, mount_code = _run_adb(device_id, "shell mount", timeout=10)
    status.product_remountable = mount_point_is_rw(mount_out, OVERLAY_DIR.rsplit("/", 1)[0]) if mount_code == 0 else None

    # Is our overlay already installed?
    apk_remote = f"{OVERLAY_DIR}/{OVERLAY_APK_NAME}"
    ls_out, ls_code = _run_adb(device_id, f"shell ls {shlex.quote(apk_remote)}", timeout=10)
    status.overlay_installed = ls_code == 0 and "No such file" not in ls_out
    if status.overlay_installed:
        status.overlay_apk_path = apk_remote

    # Best-effort: how many overrides does the store say should be applied?
    store = OverrideStore()
    entries = store.list_entries(device_id)
    status.applied_entry_count = len(entries) if entries else 0
    return status


# ---------------------------------------------------------------------------
# dm-verity control (one-time bootstrap for apply)
# ---------------------------------------------------------------------------

@dataclass
class VerityResult:
    """Outcome of a disable/enable-verity request. ``needs_reboot`` means the
    device must reboot for the new verity state to take effect (the caller does
    NOT reboot here — the UI reuses POST /api/config-override/reboot)."""
    success: bool
    action: str          # 'disable' | 'enable'
    message: str
    needs_reboot: bool = False


def _verity_needs_reboot(output: str) -> bool:
    """``adb disable/enable-verity`` prints 'Reboot the device for new settings
    to take effect' when the state actually changed (needs reboot). When the
    state is already as requested it prints 'already enabled/disabled' and no
    reboot is needed."""
    return "Reboot the device" in output or "reboot your device" in output.lower()


def disable_verity(device_id: str | None) -> VerityResult:
    """Run ``adb disable-verity``. Does NOT reboot — returns needs_reboot so the
    caller can chain POST /api/config-override/reboot. This is the one-time
    bootstrap that makes apply() work on a userdebug device."""
    out, code = _run_adb(device_id, "disable-verity", timeout=15)
    if code != 0:
        return VerityResult(False, "disable", f"disable-verity 失败: {out.strip()}")
    return VerityResult(
        True, "disable",
        "已禁用 dm-verity" + ("（重启后生效）" if _verity_needs_reboot(out) else "（已是禁用状态）"),
        needs_reboot=_verity_needs_reboot(out),
    )


def enable_verity(device_id: str | None) -> VerityResult:
    """Run ``adb enable-verity``. Restores verified boot. Returns needs_reboot."""
    out, code = _run_adb(device_id, "enable-verity", timeout=15)
    if code != 0:
        return VerityResult(False, "enable", f"enable-verity 失败: {out.strip()}")
    return VerityResult(
        True, "enable",
        "已恢复 dm-verity" + ("（重启后生效）" if _verity_needs_reboot(out) else "（已是启用状态）"),
        needs_reboot=_verity_needs_reboot(out),
    )


def reboot_device(device_id: str | None) -> ApplyResult:
    """Reboot the device via the local adb connection (same path as disable/
    enable-verity, so the device_id is unambiguous). Fire-and-forget."""
    result = reboot_with_runner(_run_adb_for_ops, device_id, wait_for_online=False)
    if not result.success:
        return ApplyResult(False, "error", f"重启失败: {result.output.strip()}")
    return ApplyResult(True, "rebooting", "设备正在重启，约 40 秒后刷新状态。", rebooting=True)


# ---------------------------------------------------------------------------
# Orchestration: apply / revert
# ---------------------------------------------------------------------------

def apply_overrides(
    device_id: str | None,
    store: OverrideStore | None = None,
) -> ApplyResult:
    """Rebuild the single overlay APK from ALL stored entries, push, reboot.

    Ordering guarantees atomicity (a failure at any stage leaves the device's
    previous overlay untouched):
      1. load + validate all entries  (no device I/O)
      2. build APK locally            (no device I/O)
      3. adb root + remount /product  (fails ⇒ raise, device untouched)
      4. adb push + chcon
      5. adb reboot (fire-and-forget) ⇒ return rebooting=True
    """
    store = store or OverrideStore()
    entries = store.list_entries(device_id)
    if not entries:
        return ApplyResult(True, "validated", "无覆盖项，无需应用")

    # 1. validate (defensive — upsert already validated, but re-check).
    for e in entries:
        try:
            e.value = validate_override(e.resource_type, e.value)
        except ValueError as exc:
            return ApplyResult(False, "error", f"覆盖项 {e.resource_name} 无效: {exc}")

    # 2. build locally (device untouched on failure).
    symbol_apk = resolve_symbol_apk(device_id, DEFAULT_TARGET_PACKAGE)
    work_dir = tempfile.mkdtemp(prefix="rro_build_")
    try:
        apk_path = build_overlay_apk(entries, work_dir, symbol_apk)
    except Exception as exc:
        logger.exception("RRO build failed")
        return ApplyResult(False, "error", f"编译 overlay APK 失败: {exc}")

    # 3. ensure writable.
    remount = root_and_remount(_run_adb_for_ops, device_id, "/product")
    if not remount.success:
        shutil.rmtree(work_dir, ignore_errors=True)
        return ApplyResult(
            False, "error",
            "无法 remount /product。若 dm-verity 未关闭，请先手动执行一次："
            "`adb disable-verity && adb reboot`（仅一次性，需重启）。"
            f"输出: {remount.remount_output.strip() or remount.root_output.strip()}",
        )

    # Ensure the overlay dir exists (some builds lack it).
    _run_adb(device_id, f"shell mkdir -p {OVERLAY_DIR}", timeout=10)

    # 4. push + chcon.
    remote = f"{OVERLAY_DIR}/{OVERLAY_APK_NAME}"
    push_out, push_code = _run_adb(
        device_id, f"push {shlex.quote(apk_path)} {shlex.quote(remote)}", timeout=60
    )
    if push_code != 0:
        shutil.rmtree(work_dir, ignore_errors=True)
        return ApplyResult(False, "error", f"推送 overlay APK 失败: {push_out.strip()}")

    _run_adb(
        device_id,
        f"shell chcon u:object_r:system_file:s0 {shlex.quote(remote)}",
        timeout=10,
    )

    # 5. reboot (fire-and-forget). HTTP returns before reconnect.
    reboot_with_runner(_run_adb_for_ops, device_id, wait_for_online=False)
    return ApplyResult(
        True, "rebooting", "已推送 overlay 并重启设备，约 40 秒后刷新状态。",
        apk_path=remote, rebooting=True,
    )


def revert_all(
    device_id: str | None,
    store: OverrideStore | None = None,
) -> ApplyResult:
    """Delete the overlay APK from the device and reboot. Does NOT clear the
    host store (so the user can re-apply after editing)."""
    # 1. ensure writable.
    remount = root_and_remount(_run_adb_for_ops, device_id, "/product")
    if not remount.success:
        return ApplyResult(
            False, "error",
            "无法 remount /product。若 dm-verity 未关闭，请先手动执行一次："
            "`adb disable-verity && adb reboot`。"
            f"输出: {remount.remount_output.strip() or remount.root_output.strip()}",
        )

    remote = f"{OVERLAY_DIR}/{OVERLAY_APK_NAME}"
    ls_out, ls_code = _run_adb(device_id, f"shell ls {shlex.quote(remote)}", timeout=10)
    installed = ls_code == 0 and "No such file" not in ls_out
    if installed:
        _run_adb(device_id, f"shell rm -f {shlex.quote(remote)}", timeout=10)

    reboot_with_runner(_run_adb_for_ops, device_id, wait_for_online=False)
    msg = (
        "已删除 overlay 并重启设备。" if installed
        else "设备上无 overlay 包，已重启以恢复状态。"
    )
    return ApplyResult(True, "rebooting", msg, rebooting=True)
