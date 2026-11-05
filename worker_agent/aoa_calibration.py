"""Bind measured touch scales to the display configuration they describe."""
import re

from foundation.aoa_display import input_displays
from foundation.aoa_profile import AOA_TOUCH_CALIBRATION_VERSION


CALIBRATION_COMPONENT = "com.gms.remote.aoacalibration/.CalibrationActivity"


def _input_rotation(dump):
    """Touch orientation across InputFlinger dump generations.

    Android ≤16 emits ``SurfaceOrientation: N`` per device; Android 17
    replaced it with ``InputDeviceOrientation: RotationN`` inside the
    reader state. Both carry the same 0..3 quarter-turn semantics.
    """
    match = (re.search(r"SurfaceOrientation:\s*([0-3])\b", dump)
             or re.search(r"InputDeviceOrientation:\s*Rotation\s*([0-3])\b", dump))
    return int(match.group(1)) if match else None


def display_context(serial, width, height, adb_run, touch_mode="pointer", display_id=0):
    model = (adb_run(serial, "shell", "getprop", "ro.product.model").stdout or "").strip()
    release = (adb_run(serial, "shell", "getprop", "ro.build.version.release").stdout or "").strip()
    fingerprint = (adb_run(serial, "shell", "getprop", "ro.build.fingerprint").stdout or "").strip()
    density_args = ("shell", "wm", "density") if display_id == 0 else ("shell", "wm", "density", "-d", str(display_id))
    density_output = adb_run(serial, *density_args).stdout or ""
    density = None
    for line in density_output.splitlines():
        if line.strip().startswith(("Override density:", "Physical density:")):
            try:
                density = int(line.split(":", 1)[1].strip())
            except (TypeError, ValueError):
                pass
            if line.strip().startswith("Override density:"):
                break
    dump = adb_run(serial, "shell", "dumpsys", "input").stdout or ""
    kernel = (adb_run(serial, "shell", "uname", "-r").stdout or "").strip()
    board = (adb_run(serial, "shell", "getprop", "ro.product.board").stdout or "").strip()
    hardware = (adb_run(serial, "shell", "getprop", "ro.hardware").stdout or "").strip()
    locale = (adb_run(serial, "shell", "getprop", "persist.sys.locale").stdout or "").strip()
    displays = input_displays(dump)
    selected = next((value for value in displays if value["display_id"] == display_id), None)
    routes = set(re.findall(r"uniqueId=['\"]([^'\"]+)['\"]", dump))
    if display_id != 0 and selected is None:
        raise ValueError("cannot verify requested display viewport")
    rotation = _input_rotation(dump)
    if not model or rotation is None:
        raise ValueError("cannot verify display orientation; touch calibration must be repeated after reconnecting")
    context = {"version": AOA_TOUCH_CALIBRATION_VERSION, "model": model, "release": release,
            "fingerprint": fingerprint[:256], "density": density,
            "kernel": kernel[:256], "display_route": next(iter(routes)) if len(routes) == 1 else "",
            "board": board[:256], "hardware": hardware[:256], "locale": locale[:64], "display_id": display_id,
            "display": [width, height], "rotation": rotation,
            "hid_touch_mode": touch_mode}
    if selected:
        context.update(selected)
    return context


def verify_calibration_surface(serial, adb_run, display_id=0):
    """A launch receipt is insufficient: reject covered or unfocused surfaces."""
    if type(display_id) is not int or display_id < 0:
        raise ValueError("invalid calibration display ID")
    result = adb_run(serial, "shell", "dumpsys", "window", timeout=15)
    output = (result.stdout or "").replace(
        "com.gms.remote.aoacalibration/com.gms.remote.aoacalibration.CalibrationActivity",
        CALIBRATION_COMPONENT)
    focused_display = re.search(r"m(?:TopFocused|Focused)DisplayId\s*[=:]\s*(\d+)", output)
    windows = re.findall(r"mCurrentFocus=Window\{[^}\n]*\s([^ }\n]+/[^ }\n]+)[^}\n]*\}", output)
    locked = re.search(r"(?:mKeyguardShowing|mShowingLockscreen|mDreamingLockscreen|isStatusBarKeyguard)\s*[=:]\s*true", output)
    if (result.returncode != 0 or not focused_display or int(focused_display.group(1)) != display_id
            or not windows or set(windows) != {CALIBRATION_COMPONENT} or locked):
        raise ValueError("安全标定 Activity 不在目标显示器前台或被锁屏/弹窗遮挡，请恢复标定页面后重试")


def open_calibration_surface(serial, adb_run, display_id=0):
    """Start the dedicated touch-consuming Activity before sending any probes."""
    result = adb_run(serial, "shell", "am", "start", "--display", str(display_id), "-W", "-n",
                     CALIBRATION_COMPONENT, timeout=30)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 or "Error" in output or "Status: ok" not in output:
        raise ValueError("安全标定应用未安装或无法启动；请安装 AOA Calibration 后重试")
    verify_calibration_surface(serial, adb_run, display_id)


def inventory_context(output, details):
    """Parse read-only, asynchronously enriched inventory; do not reuse source data."""
    sections = {}
    name = None
    for line in output.splitlines():
        if re.fullmatch(r"__[A-Z_]+__", line.strip()):
            name = line.strip()
            sections[name] = []
        elif name:
            sections[name].append(line)
    size = "\n".join(sections.get("__WM_SIZE__", []))
    density = "\n".join(sections.get("__WM_DENSITY__", []))
    dump = "\n".join(sections.get("__INPUT__", []))
    sizes = re.findall(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", size)
    densities = re.findall(r"(?:Physical|Override) density:\s*(\d+)", density)
    rotation = _input_rotation(dump)
    displays = input_displays(dump)
    routes = set(re.findall(r"uniqueId=['\"]([^'\"]+)['\"]", dump))
    if not sizes or rotation is None or not details.get("model") or not details.get("android_version"):
        return None
    context = {"version": AOA_TOUCH_CALIBRATION_VERSION, "model": details["model"],
            "release": details["android_version"], "display": list(map(int, sizes[-1])),
            "density": int(densities[-1]) if densities else None,
            "rotation": rotation,
            "fingerprint": "".join(sections.get("__FINGERPRINT__", [])).strip()[:256],
            "kernel": "".join(sections.get("__KERNEL__", [])).strip()[:256],
            "display_route": next(iter(routes)) if len(routes) == 1 else "", "display_id": 0,
            "board": "".join(sections.get("__BOARD__", [])).strip()[:256],
            "hardware": "".join(sections.get("__HARDWARE__", [])).strip()[:256],
            "locale": "".join(sections.get("__LOCALE__", [])).strip()[:64]}
    selected = next((value for value in displays if value["display_id"] == 0), None)
    if selected:
        context.update(selected)
    for display in displays:
        display["density"] = context["density"] if display["display_id"] == 0 else None
    context["displays"] = displays
    return context


def enrich_display_densities(context, query):
    """Additional display densities are independent; an unavailable one stays unknown."""
    for display in (context.get("displays") or [])[:4]:
        if display["display_id"] == 0:
            continue
        output = query(display["display_id"])
        values = re.findall(r"(?:Physical|Override) density:\s*(\d+)", output or "")
        display["density"] = int(values[-1]) if values else None
