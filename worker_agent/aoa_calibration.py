"""Bind measured touch scales to the display configuration they describe."""
import re

from foundation.aoa_profile import AOA_TOUCH_CALIBRATION_VERSION


def display_context(serial, width, height, adb_run, touch_mode="pointer"):
    model = (adb_run(serial, "shell", "getprop", "ro.product.model").stdout or "").strip()
    release = (adb_run(serial, "shell", "getprop", "ro.build.version.release").stdout or "").strip()
    dump = adb_run(serial, "shell", "dumpsys", "input").stdout or ""
    rotation = re.search(r"SurfaceOrientation:\s*([0-3])\b", dump)
    if not model or not rotation:
        raise ValueError("cannot verify display orientation; touch calibration must be repeated after reconnecting")
    return {"version": AOA_TOUCH_CALIBRATION_VERSION, "model": model, "release": release,
            "display": [width, height], "rotation": int(rotation.group(1)), "hid_touch_mode": touch_mode}
