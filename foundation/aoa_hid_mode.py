"""Explicit HID mode selection without changing stored command coordinates."""
from __future__ import annotations

import math


# Original pointer descriptor. Keep its bytes unchanged for calibrated legacy
# profiles; the direct touchscreen descriptor lives in aoa_profile.
POINTER_TOUCH_DESCRIPTOR = bytes([
    0x05, 0x0D, 0x09, 0x04, 0xA1, 0x00, 0x09, 0x32, 0x09, 0x33,
    0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x02, 0x81, 0x02,
    0x75, 0x01, 0x95, 0x06, 0x81, 0x01, 0x05, 0x01, 0x09, 0x30,
    0x15, 0x00, 0x26, 0x68, 0x01, 0x75, 0x10, 0x95, 0x01, 0x81, 0x02,
    0x09, 0x31, 0x15, 0x00, 0x26, 0x80, 0x02, 0x75, 0x10, 0x95, 0x01,
    0x81, 0x02, 0xC0,
])


def validate_touch_mode(mode: str) -> str:
    if mode not in ("pointer", "direct"):
        raise ValueError("AOA HID touch mode must be pointer or direct")
    return mode


def profile_hid_options(metadata: dict | None) -> dict:
    metadata = {} if metadata is None else metadata
    if not isinstance(metadata, dict):
        raise ValueError("AOA HID metadata must be an object")
    calibration = metadata.get("calibration")
    calibration = {} if calibration is None else calibration
    if not isinstance(calibration, dict):
        raise ValueError("AOA calibration metadata must be an object")
    context = calibration.get("context")
    context = {} if context is None else context
    if not isinstance(context, dict):
        raise ValueError("AOA calibration context must be an object")
    # Version 2 was introduced with direct touch before mode selection existed.
    inferred = context.get("hid_touch_mode", "direct") if context.get("version") == 2 else "pointer"
    inferred = calibration.get("hid_touch_mode", inferred)
    mode = validate_touch_mode(metadata.get("hid_touch_mode", inferred))
    settle = metadata.get("hid_settle_seconds", 2)
    if isinstance(settle, bool) or not isinstance(settle, (int, float)) or not math.isfinite(settle) or not 0 <= settle <= 10:
        raise ValueError("AOA HID settle seconds must be 0..10")
    return {"aoa_hid_touch_mode": mode, "aoa_hid_settle_seconds": settle}


def hid_registrations(mode: str):
    from .aoa_profile import AOA_HID_REGISTRATIONS, AOA_HID_TOUCH_SCREEN

    validate_touch_mode(mode)
    return tuple((channel, POINTER_TOUCH_DESCRIPTOR if channel == AOA_HID_TOUCH_SCREEN and mode == "pointer" else descriptor)
                 for channel, descriptor in AOA_HID_REGISTRATIONS)
