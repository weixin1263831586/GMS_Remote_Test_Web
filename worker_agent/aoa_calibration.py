"""Bind measured touch scales to the display configuration they describe."""
import re

from foundation.aoa_profile import AOA_TOUCH_CALIBRATION_VERSION


def display_context(serial, width, height, adb_run, touch_mode="pointer"):
    model = (adb_run(serial, "shell", "getprop", "ro.product.model").stdout or "").strip()
    release = (adb_run(serial, "shell", "getprop", "ro.build.version.release").stdout or "").strip()
    fingerprint = (adb_run(serial, "shell", "getprop", "ro.build.fingerprint").stdout or "").strip()
    density_output = adb_run(serial, "shell", "wm", "density").stdout or ""
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
    route = re.search(r"uniqueId=['\"]([^'\"]+)['\"]", dump)
    rotation = re.search(r"SurfaceOrientation:\s*([0-3])\b", dump)
    if not model or not rotation:
        raise ValueError("cannot verify display orientation; touch calibration must be repeated after reconnecting")
    return {"version": AOA_TOUCH_CALIBRATION_VERSION, "model": model, "release": release,
            "fingerprint": fingerprint[:256], "density": density,
            "kernel": kernel[:256], "display_route": route.group(1) if route else "",
            "display": [width, height], "rotation": int(rotation.group(1)),
            "hid_touch_mode": touch_mode}


def open_calibration_surface(serial, adb_run):
    """Start the dedicated touch-consuming Activity before sending any probes."""
    result = adb_run(serial, "shell", "am", "start", "-W", "-n",
                     "com.gms.remote.aoacalibration/.CalibrationActivity", timeout=30)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 or "Error" in output or "Status: ok" not in output:
        raise ValueError("安全标定应用未安装或无法启动；请安装 AOA Calibration 后重试")


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
    rotation = re.search(r"SurfaceOrientation:\s*([0-3])\b", dump)
    route = re.search(r"uniqueId=['\"]([^'\"]+)['\"]", dump)
    if not sizes or not rotation or not details.get("model") or not details.get("android_version"):
        return None
    return {"version": AOA_TOUCH_CALIBRATION_VERSION, "model": details["model"],
            "release": details["android_version"], "display": list(map(int, sizes[-1])),
            "density": int(densities[-1]) if densities else None,
            "rotation": int(rotation.group(1)),
            "fingerprint": "".join(sections.get("__FINGERPRINT__", [])).strip()[:256],
            "kernel": "".join(sections.get("__KERNEL__", [])).strip()[:256],
            "display_route": route.group(1) if route else ""}
