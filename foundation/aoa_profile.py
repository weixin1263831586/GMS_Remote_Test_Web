"""AOAv2 命令序列的单一真值：语法、上限与 HID 报文。

对齐 AOSP ``AoaTargetPreparer``（tools/tradefederation aoa_helper）的官方
动作语法与 ``AoaDevice`` 的 HID 报文格式：

- 动作：``click X Y`` / ``longClick X Y`` / ``swipe X1 Y1 MS X2 Y2`` /
  ``write TEXT`` / ``key [N*]KEY ...`` / ``wake`` / ``home`` / ``back`` /
  ``sleep MS``（WebAOA 同款，用户可把 ATS 录制的 action 列表直接粘贴）。
- 触摸报文 5 字节：``[type, x_lo, x_hi, y_lo, y_hi]``，逻辑分辨率
  X≤360 / Y≤640；键盘报文 2 字节 ``[modifier, usage]``；系统键 1 字节
  位标志（wake=1, home=2, back=4）。

Controller（校验请求/profile）与 Worker（生成 USB 控制传输报文）都从本
模块读取，避免两处漂移。ADR 0022 的安全边界：限制步骤数、总运行
时长与允许的按键集合，keycode 不开放任意整数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ---- 协议常量（AoaDevice.java）----
AOA_GOOGLE_VID = 0x18D1
AOA_ACCESSORY_PID_RANGE = (0x2D00, 0x2D05)
AOA_ACCESSORY_GET_PROTOCOL = 51
AOA_ACCESSORY_SEND_STRING = 52
AOA_ACCESSORY_START = 53
AOA_ACCESSORY_REGISTER_HID = 54
AOA_ACCESSORY_UNREGISTER_HID = 55
AOA_ACCESSORY_SET_HID_REPORT_DESC = 56
AOA_ACCESSORY_SEND_HID_EVENT = 57
# bmRequestType：方向 OUT | 类型 vendor(0x02<<5)
AOA_REQUEST_OUTPUT = 0x40
AOA_REQUEST_INPUT = 0xC0

# AOAv2 HID ID 由配件分配，并非设备类型枚举。避开部分 gadget 拒绝的 ID 0。
AOA_HID_TOUCH_SCREEN = 3
AOA_HID_KEYBOARD = 1
AOA_HID_SYSTEM = 2

# 触摸首字节：低 2 位为 in-range/tip switch，其余 6 位为 Contact ID（单指固定 0）。
AOA_TOUCH_UP = 0b00
AOA_TOUCH_DOWN = 0b11

# 系统键位标志
AOA_SYSTEM_WAKE = 0b001
AOA_SYSTEM_HOME = 0b010
AOA_SYSTEM_BACK = 0b100

# 触摸屏 HID 逻辑分辨率（描述符 Logical Maximum，AoaDevice.touch 里 clamp）
AOA_TOUCH_MAX_X = 360
AOA_TOUCH_MAX_Y = 640
# 版本 3：多点仿射标定 + AOSP 按轴分母 max-min+1（361/641）。version 2 是
# 直接触摸+单点比例；两者同属 direct 时代（aoa_hid_mode 按版本 >=2 推断）。
AOA_TOUCH_CALIBRATION_VERSION = 3

TOUCH_SCREEN_HID_DESCRIPTOR = bytes([
    0x05, 0x0D,        # Usage Page (Digitizer)
    0x09, 0x04,        # Usage (Touch Screen)
    0xA1, 0x01,        # Collection (Application): direct touchscreen, not a pointer
    0x09, 0x22,        #   Usage (Finger)
    0xA1, 0x02,        #   Collection (Logical)
    0x09, 0x32,        #   Usage (In Range)
    0x09, 0x42,        #   Usage (Tip Switch)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x02,        #   Report Count (2)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)
    0x09, 0x51,        #   Usage (Contact Identifier): selects Linux hid-multitouch
    0x25, 0x3F,        #   Logical Maximum (63)
    0x75, 0x06,        #   Report Size (6)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x02,        #   Input (Data, Variable, Absolute), single contact ID 0
    0x05, 0x01,        #   Usage Page (Generic)
    0x09, 0x30,        #   Usage (X)
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0x68, 0x01,  #   Logical Maximum (360)
    0x75, 0x10,        #   Report Size (16)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)
    0x09, 0x31,        #   Usage (Y)
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0x80, 0x02,  #   Logical Maximum (640)
    0x75, 0x10,        #   Report Size (16)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)
    0xC0,              #   End Finger Collection
    0xC0,              # End Touch Screen Application
])

# 101-key 键盘：8-bit modifier + 8-bit keycode（单键报告）
KEYBOARD_HID_DESCRIPTOR = bytes([
    0x05, 0x01,        # Usage Page (Generic)
    0x09, 0x06,        # Usage (Keyboard)
    0xA1, 0x01,        # Collection (Application)
    0x05, 0x07,        #   Usage Page (Key Codes)
    0x19, 0xE0,        #   Usage Minimum (Left Control)
    0x29, 0xE7,        #   Usage Maximum (Right GUI)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x08,        #   Report Count (8)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)
    0x19, 0x00,        #   Usage Minimum (0)
    0x29, 0x65,        #   Usage Maximum (101)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x65,        #   Logical Maximum (101)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x00,        #   Input (Data, Array, Absolute)
    0xC0,              # End Collection
])

# 系统键：wake/home/back 三位标志 + 5 位 padding
SYSTEM_HID_DESCRIPTOR = bytes([
    0x05, 0x01,        # Usage Page (Generic)
    0x09, 0x80,        # Usage (System Control)
    0xA1, 0x01,        # Collection (Application)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x01,        #   Report Count (1)
    0x09, 0x83,        #   Usage (Wake)
    0x81, 0x06,        #   Input (Data, Variable, Relative)
    0xC0,              # End Collection
    0x05, 0x0C,        # Usage Page (Consumer)
    0x09, 0x01,        # Usage (Consumer Control)
    0xA1, 0x01,        # Collection (Application)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x01,        #   Report Count (1)
    0x0A, 0x23, 0x02,  #   Usage (Home)
    0x81, 0x06,        #   Input (Data, Variable, Relative)
    0x0A, 0x24, 0x02,  #   Usage (Back)
    0x81, 0x06,        #   Input (Data, Variable, Relative)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x05,        #   Report Count (5) - padding
    0x81, 0x01,        #   Input (Constant)
    0xC0,              # End Collection
])

# HID 标识符 → 描述符的注册顺序。
AOA_HID_REGISTRATIONS: tuple[tuple[int, bytes], ...] = (
    (AOA_HID_TOUCH_SCREEN, TOUCH_SCREEN_HID_DESCRIPTOR),
    (AOA_HID_KEYBOARD, KEYBOARD_HID_DESCRIPTOR),
    (AOA_HID_SYSTEM, SYSTEM_HID_DESCRIPTOR),
)

# ---- 键位表（官方 AoaTargetPreparer.KEYS；白名单，不开放任意 usage）----
MODIFIER_SHIFT = 0b0010
_KEY_USAGES: dict[str, int] = {
    "1": 0x1E, "2": 0x1F, "3": 0x20, "4": 0x21, "5": 0x22,
    "6": 0x23, "7": 0x24, "8": 0x25, "9": 0x26, "0": 0x27,
    "enter": 0x28, "tab": 0x2B, "space": 0x2C, "-": 0x2D, ".": 0x37,
    "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52,
    "@": 0x1F, "_": 0x2D, "+": 0x1E,
}
for _usage, _letter in zip(range(0x04, 0x1E), "abcdefghijklmnopqrstuvwxyz"):
    _KEY_USAGES[_letter] = _usage
    _KEY_USAGES[_letter.upper()] = _usage  # 大写 = SHIFT 修饰（大小写敏感键）
_KEY_USAGES["@"] = 0x1F  # 官方：SHIFT+0x1F
_KEY_USAGES["_"] = 0x2D  # SHIFT+0x2D
_KEY_USAGES["+"] = 0x1E  # SHIFT+0x1E
_SHIFTED = {"@", "_", "+"} | set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

# TV/STB 遥控语义别名（ADR 0022：优先虚拟遥控器按键）
_DPAD_ALIASES = {"dpad_up": "up", "dpad_down": "down", "dpad_left": "left",
                 "dpad_right": "right", "dpad_center": "enter", "ok": "enter"}
# 官方 WebAOA 语法里的全大写方向/导航键（key RIGHT 2*TAB ENTER）：方向
# 与导航键大小写不敏感，其余键保持官方的大小写敏感语义。
for _alias in ("UP", "DOWN", "LEFT", "RIGHT", "ENTER", "TAB", "BACK", "HOME"):
    _DPAD_ALIASES[_alias] = _alias.lower()

_POINT_RE = re.compile(r"(\d{1,3}) (\d{1,3})")
_KEY_TOKEN_RE = re.compile(r"(?:(\d+)\*)?([a-zA-Z0-9@\-_.]+)")
# write 只支持空格（text_to_key_reports 将空格映射为 0x2C，其余空白
# 字符无 HID 映射），正则收紧为仅空格，避免解析通过而回放失败。
_WRITE_RE = re.compile(r"[a-zA-Z0-9@\-_. ]+")

# ---- 安全上限（ADR 0022：限制步骤数/时长/按键）----
MAX_PROFILE_COMMANDS = 256
MAX_SLEEP_MS = 10_000
MAX_TOTAL_RUNTIME_MS = 300_000
MAX_AOA_OPERATION_SECONDS = 960
MAX_AOA_PAUSE_SECONDS = 600
MAX_WRITE_LENGTH = 128


@dataclass(frozen=True)
class AoaCommand:
    """一条解析后的 AOA 命令（kind 与官方动作一一对应）。"""

    kind: str          # click/longClick/swipe/write/key/wake/home/back/sleep
    x1: int = 0
    y1: int = 0
    x2: int = 0
    y2: int = 0
    duration_ms: int = 0
    text: str = ""
    keys: tuple[tuple[int, int], ...] = ()   # (modifier, usage) 序列
    sleep_ms: int = 0


def required_hid_channels(commands: list[AoaCommand]) -> frozenset[int]:
    """Freeze only the input devices this sequence needs before sending any input."""
    channels = set()
    for command in commands:
        if command.kind in {'click', 'longClick', 'swipe'}:
            channels.add(AOA_HID_TOUCH_SCREEN)
        elif command.kind in {'key', 'write'}:
            channels.add(AOA_HID_KEYBOARD)
        elif command.kind in {'wake', 'home', 'back'}:
            channels.add(AOA_HID_SYSTEM)
    return frozenset(channels)


def parse_profile_commands(commands: list[str] | tuple[str, ...]) -> list[AoaCommand]:
    """解析并校验 AOA 命令序列；语法/上限不满足时抛 ``ValueError``。

    与官方 ``AoaTargetPreparer`` 正则一一对应；额外施加 ADR 0022 的步骤数、
    总时长、sleep 单步与 write 长度上限，保证单次 device_action 在
    Controller 等待窗口内可完成。
    """
    if not commands:
        raise ValueError("AOA profile is empty")
    if len(commands) > MAX_PROFILE_COMMANDS:
        raise ValueError(f"AOA profile exceeds {MAX_PROFILE_COMMANDS} commands")
    parsed: list[AoaCommand] = []
    total_runtime_ms = 0
    for line in commands:
        text = str(line or "").strip()
        if not text:
            continue
        head, _, rest = text.partition(" ")
        args = rest.split()
        if head in ("click", "longClick"):
            match = _POINT_RE.fullmatch(rest)
            if not match:
                raise ValueError(f"invalid AOA action: {text}")
            parsed.append(AoaCommand(kind=head, x1=int(match.group(1)), y1=int(match.group(2))))
        elif head == "swipe":
            match = re.fullmatch(rf"{_POINT_RE.pattern} (\d+) {_POINT_RE.pattern}", rest)
            if not match:
                raise ValueError(f"invalid AOA action: {text}")
            parsed.append(AoaCommand(kind=head, x1=int(match.group(1)), y1=int(match.group(2)),
                                     duration_ms=int(match.group(3)),
                                     x2=int(match.group(4)), y2=int(match.group(5))))
        elif head == "write":
            payload = rest
            if not payload or not _WRITE_RE.fullmatch(payload):
                raise ValueError(f"invalid AOA action: {text}")
            if payload.startswith("-"):
                # 与 record input 同一限制：Android `input text -…` 会把
                # 首词当选项解析；Profile/录制语法保持一致。
                raise ValueError(f"AOA write must not start with '-': {text}")
            if len(payload) > MAX_WRITE_LENGTH:
                raise ValueError(f"AOA write exceeds {MAX_WRITE_LENGTH} characters")
            parsed.append(AoaCommand(kind=head, text=payload))
        elif head == "key":
            keys = _parse_key_combo(args, text)
            parsed.append(AoaCommand(kind=head, keys=keys))
        elif head in ("wake", "home", "back"):
            if args:
                raise ValueError(f"invalid AOA action: {text}")
            parsed.append(AoaCommand(kind=head))
        elif head == "sleep":
            if len(args) != 1 or not args[0].isdigit():
                raise ValueError(f"invalid AOA action: {text}")
            milliseconds = int(args[0])
            if not 0 < milliseconds <= MAX_SLEEP_MS:
                raise ValueError(f"AOA sleep must be 1..{MAX_SLEEP_MS} ms")
            parsed.append(AoaCommand(kind=head, sleep_ms=milliseconds))
        else:
            raise ValueError(f"invalid AOA action: {text}")
        command = parsed[-1]
        if command.kind in {"click", "longClick", "swipe"}:
            points = [(command.x1, command.y1)]
            if command.kind == "swipe":
                points.append((command.x2, command.y2))
            if any(not (0 <= x <= AOA_TOUCH_MAX_X and 0 <= y <= AOA_TOUCH_MAX_Y) for x, y in points):
                raise ValueError(f"AOA coordinates outside 0..{AOA_TOUCH_MAX_X}, 0..{AOA_TOUCH_MAX_Y}: {text}")
        total_runtime_ms += _estimate_runtime_ms(parsed[-1])
    if not parsed:
        raise ValueError("AOA profile is empty")
    if total_runtime_ms > MAX_TOTAL_RUNTIME_MS:
        raise ValueError(
            f"AOA profile runtime {total_runtime_ms}ms exceeds {MAX_TOTAL_RUNTIME_MS}ms budget")
    return parsed


def _parse_key_combo(args: list[str], text: str) -> tuple[tuple[int, int], ...]:
    if not args:
        raise ValueError(f"invalid AOA action: {text}")
    keys: list[tuple[int, int]] = []
    for token in args:
        match = _KEY_TOKEN_RE.fullmatch(token)
        if not match:
            raise ValueError(f"invalid AOA key: {token}")
        count = int(match.group(1)) if match.group(1) else 1
        name = match.group(2)
        # 大小写敏感（官方 parseKey）：原名优先，再查 TV 遥控/导航别名。
        if name not in _KEY_USAGES:
            alias = _DPAD_ALIASES.get(name) or _DPAD_ALIASES.get(name.lower())
            if alias:
                name = alias
        usage = _KEY_USAGES.get(name)
        if usage is None:
            raise ValueError(f"AOA key not allowed: {match.group(2)}")
        modifier = MODIFIER_SHIFT if name in _SHIFTED else 0
        if not 0 < count <= 32:
            raise ValueError(f"AOA key repeat count out of range: {count}")
        keys.extend([(modifier, usage)] * count)
    return tuple(keys)


def _estimate_runtime_ms(command: AoaCommand) -> int:
    """保守估算单命令耗时（swipe 按官方 10ms 步进插值 + 动作间隔）。

    key/write 每键 2 个 HID 事件（按下+释放）× ~20ms；防止用超长按键
    序列放大实际执行时间绕过 300s 预算。
    """
    if command.kind == "sleep":
        return command.sleep_ms
    if command.kind == "swipe":
        return max(command.duration_ms, 100) + 1_000
    if command.kind == "write":
        return len(command.text) * 120 + 1_000
    if command.kind == "key":
        return max(len(command.keys), 1) * 120 + 1_000
    if command.kind == "longClick":
        return 2_000
    return 1_200


# ---- HID 报文构建（AoaDevice.java 的 touch/pressKeys/system 等价实现）----

def touch_report(touch_down: bool, x: int, y: int) -> bytes:
    x = min(max(int(x), 0), AOA_TOUCH_MAX_X)
    y = min(max(int(y), 0), AOA_TOUCH_MAX_Y)
    return bytes([
        (AOA_TOUCH_DOWN if touch_down else AOA_TOUCH_UP),
        x & 0xFF, (x >> 8) & 0xFF,
        y & 0xFF, (y >> 8) & 0xFF,
    ])


def key_report(modifier: int, usage: int) -> bytes:
    return bytes([modifier & 0xFF, usage & 0xFF])


def system_report(flag: int) -> bytes:
    return bytes([flag & 0xFF])


def swipe_points(x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> list[tuple[bool, int, int]]:
    """官方 swipe 插值：down 后按 10ms 步进线性移动到终点再 up。"""
    duration_ms = max(int(duration_ms or 100), 100)
    points: list[tuple[bool, int, int]] = [(True, x1, y1)]
    elapsed = 0
    while elapsed < duration_ms:
        elapsed = min(elapsed + 10, duration_ms)
        progress = elapsed / duration_ms
        points.append((True,
                       round(progress * x2 + (1 - progress) * x1),
                       round(progress * y2 + (1 - progress) * y1)))
        if elapsed >= duration_ms:
            break
    points.append((False, x2, y2))
    return points


def text_to_key_reports(text: str) -> list[bytes]:
    """官方 write：逐字符转 (modifier, usage)，空格转 0x2C。"""
    reports: list[bytes] = []
    for char in text:
        if char == " ":
            reports.append(key_report(0, 0x2C))
            continue
        if char not in _KEY_USAGES:
            raise ValueError(f"AOA write contains unsupported character: {char!r}")
        modifier = MODIFIER_SHIFT if char in _SHIFTED else 0
        reports.append(key_report(modifier, _KEY_USAGES[char]))
    return reports


def command_runtime_seconds(commands: list[AoaCommand]) -> int:
    return max(30, sum(_estimate_runtime_ms(item) for item in commands) // 1000 + 30)


# ---- 录制（userdebug 固件上经 ADB 录制，回放到 user 固件）----

RECORD_ACTIONS = frozenset({
    "click", "longClick", "scroll_down", "scroll_up", "back", "home", "wake",
})
MAX_RECORD_STEPS = 256
MAX_SCROLL_ATTEMPTS = 8
MAX_CLICKS = 32

RECORD_INPUT_ACTIONS = frozenset({
    "tap", "long_click", "swipe", "back", "home", "wake", "write", "sleep",
    "calibrate",
})


def parse_record_input_action(raw: dict) -> dict:
    """Validate one interactive Web AOA recording action.

    Coordinates are physical display pixels.  The Worker executes the action
    through ADB on the userdebug recorder and returns the equivalent AOAv2
    command for the command track.
    """
    if not isinstance(raw, dict):
        raise ValueError("record input action must be an object")
    action = str(raw.get("action") or "").strip()
    if action not in RECORD_INPUT_ACTIONS:
        raise ValueError(f"unknown record input action: {action!r}")
    normalized: dict = {"action": action}

    def integer(name: str, minimum: int, maximum: int, default=None) -> int:
        value = raw.get(name, default)
        if isinstance(value, bool):
            raise ValueError(f"record input {name} must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"record input {name} must be an integer") from exc
        if not minimum <= parsed <= maximum:
            raise ValueError(
                f"record input {name} must be {minimum}..{maximum}")
        return parsed

    if action in {"tap", "long_click"}:
        normalized["x"] = integer("x", 0, 10000)
        normalized["y"] = integer("y", 0, 10000)
        if action == "long_click":
            normalized["duration_ms"] = integer("duration_ms", 500, 5000, 1000)
    elif action == "swipe":
        for name in ("x1", "y1", "x2", "y2"):
            normalized[name] = integer(name, 0, 10000)
        normalized["duration_ms"] = integer("duration_ms", 100, 5000, 400)
    elif action == "write":
        text = str(raw.get("text") or "")
        if not text or len(text) > MAX_WRITE_LENGTH or not _WRITE_RE.fullmatch(text):
            raise ValueError(
                f"record input text must be 1..{MAX_WRITE_LENGTH} supported characters")
        if text.startswith("-"):
            # Android `input text -…` 会把首词当选项解析导致输入丢失。
            raise ValueError("record input text must not start with '-'")
        normalized["text"] = text
    elif action == "sleep":
        normalized["duration_ms"] = integer("duration_ms", 1, MAX_SLEEP_MS, 800)

    for name in ("scale_x", "scale_y"):
        if raw.get(name) is None:
            continue
        if isinstance(raw[name], bool):
            raise ValueError(f"record input {name} must be numeric")
        try:
            scale = float(raw[name])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"record input {name} must be numeric") from exc
        if not 0.05 <= scale <= 100:
            raise ValueError(f"record input {name} is out of range")
        normalized[name] = scale
    context = raw.get("calibration_context")
    if "scale_x" in normalized or "scale_y" in normalized:
        if ("scale_x" not in normalized or "scale_y" not in normalized
                or not isinstance(context, dict) or type(context.get("version")) is not int
                or context.get("version") != AOA_TOUCH_CALIBRATION_VERSION
                or not isinstance(context.get("model"), str)
                or not context["model"].strip() or len(context["model"]) > 256
                or type(context.get("rotation")) is not int
                or context["rotation"] not in range(4)
                or not isinstance(context.get("display"), list)
                or len(context["display"]) != 2
                or any(type(n) is not int or not 1 <= n <= 10000 for n in context["display"])):
            raise ValueError("touch scales require a versioned calibration_context; recalibrate the display")
        normalized["calibration_context"] = {
            "version": AOA_TOUCH_CALIBRATION_VERSION, "model": context["model"], "release": str(context.get("release") or "")[:64],
            "display": context["display"], "rotation": context["rotation"],
            "hid_touch_mode": context.get("hid_touch_mode", "direct"),
        }
    if raw.get("hid_touch_mode") is not None or normalized.get("calibration_context"):
        from .aoa_hid_mode import validate_touch_mode

        expected_mode = (normalized.get("calibration_context") or {}).get("hid_touch_mode", "pointer")
        mode = validate_touch_mode(raw.get("hid_touch_mode", expected_mode))
        if normalized.get("calibration_context") and expected_mode != mode:
            raise ValueError("touch calibration belongs to another HID mode; recalibrate the display")
        normalized["hid_touch_mode"] = mode
    return normalized


def real_to_aoa_point(x: int, y: int, display_width: int, display_height: int) -> tuple[int, int]:
    """真实显示坐标 → AOA 触摸 HID 逻辑坐标（AOSP 按轴映射的逆变换）。

    官方 InputReader 触摸映射分母为 ``max - min + 1``
    （source.android.com touch-devices），HID Logical Maximum 360/640 时
    反向换算用 ``W/361``、``H/641``。这只是估算初始值：设备端分类、
    显示绑定或统一缩放都会使实际映射偏离按轴假设，必须经标定验证
    （``foundation.aoa_calibration``）。
    """
    if display_width <= 0 or display_height <= 0:
        raise ValueError("display size must be positive")
    logical_x = round(int(x) * (AOA_TOUCH_MAX_X + 1) / int(display_width))
    logical_y = round(int(y) * (AOA_TOUCH_MAX_Y + 1) / int(display_height))
    return (
        min(max(logical_x, 0), AOA_TOUCH_MAX_X),
        min(max(logical_y, 0), AOA_TOUCH_MAX_Y),
    )


def parse_record_steps(steps: list) -> list[dict]:
    """校验并规范化录制脚本（record 步骤的单一真值）。

    步骤动作：
    - ``click/longClick``：``find`` 匹配 UI 元素后点击（可 ``locate_only``
      只定位不点击——用于录制 USB 调试开关本身，避免录制期间真的关掉
      ADB）；``any_text`` 支持多语言候选；``scroll_attempts`` 找不到时
      先滚动再找；``optional`` 找不到则跳过；``clicks`` 连点 N 次
      （版本号连点 7 次解锁开发者选项）。
    - ``scroll_down/scroll_up``：固定区域滑动并记录。
    - ``back/home/wake``：按键。
    回放安全：所有步骤都会产出等价的 AOA 命令（launch/am start 之类
    依赖 ADB 的动作被禁止），保证录制的 Profile 在 user 固件上可回放。
    """
    if not steps:
        raise ValueError("AOA record steps are empty")
    if len(steps) > MAX_RECORD_STEPS:
        raise ValueError(f"AOA record exceeds {MAX_RECORD_STEPS} steps")
    normalized: list[dict] = []
    for index, raw in enumerate(steps):
        if not isinstance(raw, dict):
            raise ValueError(f"record step {index} must be an object")
        action = str(raw.get("action") or "")
        if action not in RECORD_ACTIONS:
            raise ValueError(f"record step {index}: unknown action {action!r}")
        step: dict = {"action": action, "wait_ms": _record_wait_ms(raw, index)}
        if action in ("click", "longClick"):
            find = raw.get("find")
            if not isinstance(find, dict):
                raise ValueError(f"record step {index}: click requires a find matcher")
            matcher = {
                key: value for key, value in find.items()
                if key in ("text", "any_text", "resource_id", "content_desc",
                           "class") and value
            }
            if not matcher:
                raise ValueError(f"record step {index}: find matcher is empty")
            step["find"] = matcher
            step["locate_only"] = bool(raw.get("locate_only"))
            step["optional"] = bool(raw.get("optional"))
            step["scroll_attempts"] = _record_int(
                raw, "scroll_attempts", 0, 0, MAX_SCROLL_ATTEMPTS, index)
            step["scroll_x"] = _record_float(
                raw, "scroll_x", 0.5, 0.05, 0.95, index)
            step["clicks"] = _record_int(raw, "clicks", 1, 1, MAX_CLICKS, index)
        elif action in ("scroll_down", "scroll_up"):
            step["swipe_ms"] = _record_int(raw, "swipe_ms", 300, 50, 3000, index)
        normalized.append(step)
    return normalized


def _record_int(raw: dict, key: str, default: int, low: int, high: int, index: int) -> int:
    value = raw.get(key)
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"record step {index}: {key} must be an integer") from exc
    if not low <= number <= high:
        raise ValueError(f"record step {index}: {key} out of range {low}..{high}")
    return number


def _record_float(raw: dict, key: str, default: float, low: float, high: float,
                  index: int) -> float:
    value = raw.get(key)
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"record step {index}: {key} must be a number") from exc
    if not low <= number <= high:
        raise ValueError(f"record step {index}: {key} out of range {low}..{high}")
    return round(number, 2)


def _record_wait_ms(raw: dict, index: int) -> int:
    return _record_int(raw, "wait_ms", 1500, 0, MAX_SLEEP_MS, index)


# 预设旅程（多语言匹配器；机型 UI 差异用 optional/scroll_attempts 吸收）。
PRESET_JOURNEYS: dict[str, list[dict]] = {
    "usb_debugging": [
        {"action": "wake", "wait_ms": 800},
        # 回桌面再找设置：录制机可能停在任意界面，部分启动器（实机 8e4d
        # 桌面只有 Home/Chrome/YouTube 等）没有设置图标，直接找必败。
        {"action": "home", "wait_ms": 1500},
        # 桌面上的设置入口：找不到时逐屏上滑打开应用抽屉重找（长慢滑
        # 0.75H→0.15H/500ms，实机验证可开抽屉）；重试滑动写入回放命令。
        {"action": "click", "wait_ms": 2500, "scroll_attempts": 8,
         "find": {"any_text": ["设置", "Settings"],
                  "content_desc": ["设置", "Settings"]}},
        # 双栏设置（实机 800dp 平板）：左栏是入口列表，中间滑动只滚右栏
        # 详情——找 System 必须在左栏横轴（x≈0.25）滚动；精确匹配优先，
        # 避免 System 误命中 System update。
        {"action": "click", "wait_ms": 2000, "scroll_attempts": 6,
         "scroll_x": 0.25,
         "find": {"any_text": ["系统", "System"]}},
        # System 选中后（双栏）开发者选项在左栏 System 子菜单里：右栏保持
        # 任意详情页，x=0.5 的滚动滚不到左栏——必须在左栏横轴（x≈0.25）滚。
        {"action": "click", "wait_ms": 2000, "scroll_attempts": 6,
         "scroll_x": 0.25,
         "find": {"any_text": ["开发者选项", "Developer options"]}},
        # 开发者选项页打开在右栏：USB 调试开关在右栏（x≈0.5）滚动查找。
        {"action": "click", "locate_only": True, "wait_ms": 500,
         "scroll_attempts": 4, "scroll_x": 0.5,
         "find": {"any_text": ["USB 调试", "USB debugging"]}},
    ],
}
