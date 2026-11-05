"""Synthetic independent calibration measurements; never touches a device."""
from foundation.aoa_calibration import five_point_calibration_points, mapping_from_samples


def verified_mapping(width=800, height=1280, *, mode="direct", offset_y=0):
    context = {"version": 3, "model": "RK3576", "release": "16", "display": [width, height],
               "rotation": 0, "hid_touch_mode": mode, "fingerprint": "build-a", "density": 240,
               "kernel": "test-kernel", "display_route": "local:0"}
    def measure(point):
        u, v = point
        return ((u, v), (u * width / 361, v * height / 641 + offset_y))
    return mapping_from_samples(mode, [measure(p) for p in five_point_calibration_points(360, 640)],
        context=context, display_pixels=(width, height),
        validation_samples=[measure((120, 213)), measure((240, 427))])
