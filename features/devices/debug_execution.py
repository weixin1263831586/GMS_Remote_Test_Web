"""Compile bounded app/UI operations into quoted ADB argv, never raw shell."""

from __future__ import annotations

import re
import shlex

from foundation.error_model import ApiError

from .deployment_execution import run


FIELDS = {"launch": {"activity"}, "stop": set(), "tap": {"x", "y"},
          "swipe": {"x", "y", "end_x", "end_y", "duration_ms"},
          "text": {"text"}, "keyevent": {"key"}}
REQUIRED = {"tap": {"x", "y"}, "swipe": {"x", "y", "end_x", "end_y"},
            "text": {"text"}, "keyevent": {"key"}}
KEYS = frozenset({"BACK", "ENTER", "DEL", "TAB", "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "DPAD_CENTER"})
# Complete dumps may contain both focus aliases. Every record must agree;
# an unparseable system/null window must never be skipped in favor of an app.
FOCUS_FIELD_RE = re.compile(r"^\s*m(?:CurrentFocus|FocusedWindow)\s*=\s*([^\r\n]*)", re.MULTILINE)
FOCUS_WINDOW_RE = re.compile(r"Window\{[^\s{}]+\s+u(\d+)\s+([A-Za-z0-9_.]+)/[^{}\r\n]*\}")
DISPLAY_ID_RE = re.compile(r"\bmDisplayId\s*=\s*(-?\d+)\b|^\s*Display\s+#(-?\d+)\b", re.MULTILINE)
INPUT_DISPLAY_ID = 0


def focused_window(output):
    """Return one unambiguous main-display focus; otherwise fail closed."""
    display_ids = {int(match[1] or match[2]) for match in DISPLAY_ID_RE.finditer(output)}
    if display_ids - {INPUT_DISPLAY_ID}:
        return None
    records = FOCUS_FIELD_RE.findall(output)
    if not records:
        return None
    identities = set()
    for record in records:
        match = FOCUS_WINDOW_RE.fullmatch(record.strip())
        if match is None:
            return None
        identities.add((int(match[1]), match[2]))
    return identities.pop() if len(identities) == 1 else None


def validate(body):
    fields = body.model_fields_set - {"action"}
    if fields - FIELDS[body.action]:
        raise ApiError.invalid_semantics("该调试操作包含不适用的参数")
    for field in REQUIRED.get(body.action, set()):
        if getattr(body, field) is None:
            raise ApiError.invalid_semantics("调试操作缺少参数：" + field)
    if body.action == "text" and not re.fullmatch(r"[A-Za-z0-9@._%+,:/ -]{1,200}", body.text):
        raise ApiError.invalid_semantics("输入文本仅支持 1–200 个英文、数字、空格及 @._%+,:/-")
    if body.action == "keyevent" and body.key not in KEYS:
        raise ApiError.invalid_semantics("按键不在允许的应用导航键列表内")


def perform(ssh, session, body, check):
    serial, package = session["serial"], session["package"]

    def shell(argv, timeout=30):
        check()
        output = run(ssh, ["adb", "-s", serial, "shell", shlex.join(argv)], timeout)
        if re.search(r"(?im)^\s*(Error:|Exception|SecurityException|Permission Denial|Failure|java\.)", output):
            raise ApiError.upstream_failure(output[:1500], service="adb")
        return output

    if body.action == "launch":
        if body.activity:
            activity = body.activity
            if activity.startswith("."):
                activity = package + activity
            if not re.fullmatch(re.escape(package) + r"\.[A-Za-z_$][A-Za-z0-9_.$]*", activity):
                raise ApiError.forbidden("Activity 不属于获批应用")
            component = package + "/" + activity
        else:
            output = shell(["cmd", "package", "resolve-activity", "--brief", "--user", "0",
                            "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER", "-p", package])
            components = re.findall(r"(?m)^([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)\s*$", output)
            if len(components) != 1 or components[0].split("/", 1)[0] != package:
                raise ApiError.invalid_semantics("应用未提供可启动的 Launcher Activity，请指定该应用的 Activity")
            component = components[0]
        output = shell(["am", "start", "-W", "--user", "0", "-n", component], 60)
    elif body.action == "stop":
        output = shell(["am", "force-stop", "--user", "0", package])
    else:
        focus = shell(["dumpsys", "window"])
        if focused_window(focus) != (0, package):
            raise ApiError.conflict("无法确认主显示屏的前台窗口属于获批应用（user 0）；请启动应用、人工关闭系统弹窗，或切回单显示屏后重试")
        if body.action == "tap":
            argv = ["input", "tap", str(body.x), str(body.y)]
        elif body.action == "swipe":
            argv = ["input", "swipe", *[str(getattr(body, f)) for f in ("x", "y", "end_x", "end_y")],
                    str(body.duration_ms if body.duration_ms is not None else 300)]
        elif body.action == "text":
            argv = ["input", "text", body.text.replace(" ", "%s")]
        else:
            argv = ["input", "keyevent", "KEYCODE_" + body.key]
        output = shell(["input", "-d", str(INPUT_DISPLAY_ID), *argv[1:]])
    return {"action": body.action, "package": package, "output": output[:1500]}
