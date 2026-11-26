"""AOA display geometry, frame coordinates and touch calibration."""
import re
import time

from foundation.aoa_certification import classify_direct_input, descriptor_sha256, validate_reference_evidence
from foundation.aoa_display import input_displays
from foundation.aoa_profile import AOA_TOUCH_CALIBRATION_VERSION

from .aoa_usb import bound_port, validate_binding


CALIBRATION_COMPONENT = "com.gms.remote.aoacalibration/.CalibrationActivity"


class CalibrationSurfaceUnavailableError(ValueError):
    """标定 Activity 无法启动（应用缺失/组件不可达）。

    结构化部署引导依赖它：record_aoa_input 据此产出
    calibration_app_missing 一键部署回执。界面被遮挡/失焦等运行态
    问题仍走普通 ValueError（返回 None 按轴回退，不触发部署引导）。
    """


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
        raise CalibrationSurfaceUnavailableError(
            "安全标定应用未安装或无法启动；请安装 AOA Calibration 后重试")
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


def _primary_viewport(result):
    if result.returncode != 0:
        return None
    return next((item for item in input_displays(result.stdout or "") if item["display_id"] == 0), None)


def resolve_frame_input(serial, action, display, adb_run):
    """Legacy inputs use native pixels; Web inputs explicitly carry frame size.

    scrcpy max_size changes the encoded resolution, independently of wm size
    and CSS scaling. Input viewports already include Android display rotation.
    """
    frame = action.get("frame_size")
    if frame is None:
        return action, display
    result = adb_run(serial, "shell", "dumpsys", "input", timeout=10)
    dump = result.stdout or ""
    selected = _primary_viewport(result)
    if selected is None:
        # Some Android builds omit a usable InputReader viewport. The display
        # service exposes the same current logical viewport independently.
        manager = adb_run(serial, "shell", "dumpsys", "display", timeout=10)
        selected = _primary_viewport(manager)
    if selected:
        display = selected["display"]
    else:
        if result.returncode != 0:
            raise ValueError("无法核对投屏坐标：读取设备显示信息失败，请检查 ADB 连接后重试")
        rotations = set(re.findall(r"SurfaceOrientation:\s*([0-3])\b", dump))
        if "Viewport" in dump or len(rotations) != 1:
            raise ValueError("无法核对投屏坐标：未取得唯一有效的主显示器视口，请检查设备显示配置")
        if int(next(iter(rotations))) % 2:
            display = display[::-1]
    width, height = display
    if not all(type(n) is int and 1 <= n <= 10000 for n in display):
        raise ValueError("无法核对投屏坐标：设备显示尺寸无效")
    # Encoders round dimensions to block boundaries; tolerate that small error,
    # but never stretch a stale portrait frame onto a landscape display.
    products = (frame[0] * height, frame[1] * width)
    if abs(products[0] - products[1]) > max(products) * 0.02:
        raise ValueError("投屏画面与设备方向或尺寸不一致，请等待新画面后重试")
    converted = dict(action)
    pairs = (("x1", "y1"), ("x2", "y2")) if action["action"] == "swipe" else (("x", "y"),)
    for x, y in pairs:
        converted[x] = min(width - 1, round(action[x] * width / frame[0]))
        converted[y] = min(height - 1, round(action[y] * height / frame[1]))
    return converted, (width, height)


def verify_direct_reference(serial, display_id=0):
    from . import aoa_actions as actions

    width, height = actions._display_size(serial)
    context = display_context(serial, width, height, actions._adb_run, "direct", display_id)
    width, height = context["display"]
    before = actions._adb_run(serial, "shell", "getevent", "-lp", timeout=15)
    if before.returncode:
        raise actions.AoaError("无法读取参考设备的实际输入轴和分类")
    captured = {}

    def collect():
        validate_binding()
        event = actions._adb_run(serial, "shell", "getevent", "-lp", timeout=15)
        reader = actions._adb_run(serial, "shell", "dumpsys", "input", timeout=15)
        if event.returncode or reader.returncode:
            raise actions.AoaError("无法验证新注册的 AOA 输入设备")
        inputs = classify_direct_input(before.stdout, event.stdout, reader.stdout, context)
        captured.update({"getevent_after": event.stdout, "input_reader": reader.stdout, "input": inputs})

    def probe(dump, x, y):
        device_id = captured["input"]["reader_device_id"]
        match = re.search(rf"(?ms)^  Device {re.escape(str(device_id))}:.*?(?=^  Device \d+:|\Z)", dump)
        if match is None:
            return None
        return actions._parse_probe_cooked_point(match[0], x, y)

    mapping = actions._calibrate_touch_affine(serial, width, height, time.sleep, "direct", context=context,
        evidence_hook=collect, probe_reader=probe)
    if mapping is None or not mapping.verified or actions.cleanup_diagnostics():
        raise actions.AoaError("标准 direct 参考验证失败；未生成认证证据，请核对分类、显示路由与落点")
    evidence = {"version": 1, "serial": serial, "usb_port_path": bound_port(), "context": context,
                "descriptor_sha256": descriptor_sha256(), "getevent_before": before.stdout,
                "getevent_after": captured["getevent_after"], "input_reader": captured["input_reader"],
                "samples": list(mapping.samples)}
    validate_reference_evidence(evidence)
    return {"success": True, "serial": serial, "direct_reference_evidence": evidence,
            "class_certified": False, "detail": "参考设备验证通过；类别认证还需另一台独立参考设备"}
