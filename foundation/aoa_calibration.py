"""AOA 触摸标定引擎：仿射映射、mapping_policy 与跨设备归一化换算。

ADR 0026（多点仿射标定与映射策略）的机制层单一真值，Controller 与 Worker 共用：

- **多点仿射标定**：用 ≥3 个不共线的 HID 探针点（推荐五点）实测
  ``dumpsys input`` cooked 坐标，最小二乘拟合 ``[X,Y] = M·[u,v] + t``，
  代替单点比例（单点只能覆盖零偏移、无旋转、纯按轴缩放）。
- **AOSP 公式对齐**：官方触摸设备文档的映射分母是 ``max - min + 1``
  （source.android.com touch-devices），HID Logical Maximum 360/640 时
  按轴估算用 ``W/361``、``H/641``，不是 ``W/360``。
- **mapping_policy**：标定来源分级，替代裸 ``measured`` 布尔值——
  ``direct_standard``（已验证的 AOSP 直接触摸映射，免逐台标定）、
  ``pointer_calibrated``（旧指针语义 + 实测映射）、
  ``direct_unverified``（描述符 direct 但未经设备端验证）、
  ``unsupported``（无法确认分类/显示绑定；禁止自动触摸）。
- **误差验证**：每个采样点保留欧氏残差，输出 RMS 与最大误差；
  最大误差超过 ``MAX_CALIBRATION_ERROR_PX`` 时标记 unverified。
- **跨设备归一化**：任何映射（含按轴估算）都是仿射的，因此
  ``convert_commands`` 可以把 Profile 的逻辑坐标经"源映射 → 参考像素 →
  目标映射"重投影，实现一份 Profile 跨分辨率复用。

纯计算模块：无 USB/ADB 依赖，设备侧采集见 ``worker_agent.aoa_actions``，
持久化见 ``features.devices.aoa_calibrations``。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from foundation.aoa_profile import AOA_TOUCH_MAX_X, AOA_TOUCH_MAX_Y


# ---- mapping_policy：标定来源分级（ADR 0026）----

MAPPING_POLICY_ESTIMATED = "estimated"
MAPPING_POLICY_POINTER_CALIBRATED = "pointer_calibrated"
MAPPING_POLICY_DIRECT_UNVERIFIED = "direct_unverified"
MAPPING_POLICY_DIRECT_CALIBRATED = "direct_calibrated"
MAPPING_POLICY_DIRECT_STANDARD = "direct_standard"
MAPPING_POLICY_UNSUPPORTED = "unsupported"

MAPPING_POLICIES: tuple[str, ...] = (
    MAPPING_POLICY_ESTIMATED,
    MAPPING_POLICY_POINTER_CALIBRATED,
    MAPPING_POLICY_DIRECT_UNVERIFIED,
    MAPPING_POLICY_DIRECT_CALIBRATED,
    MAPPING_POLICY_DIRECT_STANDARD,
    MAPPING_POLICY_UNSUPPORTED,
)

# 工程验收门槛（ADR 0026：初始建议 ≤8 px，非 Android 官方保证值）。
MAX_CALIBRATION_ERROR_PX = 8.0

_TOUCH_COMMAND_RE = re.compile(
    r"^(?P<kind>click|longClick|swipe)(?: (?P<args>.*))?$")

# 仿射矩阵行列式下限：低于此值说明采样共线或映射退化，无法安全求逆。
_INVERTIBLE_DETERMINANT_MIN = 1e-9


def validate_mapping_policy(policy) -> str:
    """校验 mapping_policy；未声明（None/""）返回空串表示沿用旧语义。"""
    if policy is None:
        return ""
    policy = str(policy).strip()
    if not policy:
        return ""
    if policy not in MAPPING_POLICIES:
        raise ValueError(
            "AOA mapping policy must be one of: " + ", ".join(MAPPING_POLICIES))
    return policy


def policy_for(hid_touch_mode: str, verified: bool) -> str:
    """按 HID 模式与验证结果推导 mapping_policy。"""
    if not verified:
        return MAPPING_POLICY_UNSUPPORTED
    if hid_touch_mode == "direct":
        return MAPPING_POLICY_DIRECT_CALIBRATED
    return MAPPING_POLICY_POINTER_CALIBRATED


def auto_touch_allowed(policy: str) -> bool:
    """该策略下是否允许自动注入触摸；unsupported 一律禁止。"""
    return validate_mapping_policy(policy) != MAPPING_POLICY_UNSUPPORTED


# ---- 多点最小二乘仿射拟合 ----

def five_point_calibration_points(max_x: int, max_y: int) -> list[tuple[int, int]]:
    """五点 HID 探针布局：四角 25%/75% 内缩 + 中心（ADR 0026 五点布局）。

    探针必须落在安全的可点击界面执行；点位不贴屏幕边缘，避免遮挡区
    （手势条/系统栏）吞掉触摸。四个外围点保证仿射拟合的几何条件
    （不共线），中心点用于残差验证。
    """
    quarter_x, quarter_y = max_x // 4, max_y // 4
    half_x, half_y = max_x // 2, max_y // 2
    return [
        (quarter_x, quarter_y),
        (max_x - quarter_x, quarter_y),
        (half_x, half_y),
        (quarter_x, max_y - quarter_y),
        (max_x - quarter_x, max_y - quarter_y),
    ]


def _solve3(matrix: list[list[float]], rhs: list[float]) -> tuple[float, float, float]:
    """解 3×3 线性方程组（高斯消元 + 部分主元）。"""
    augmented = [[*row, value] for row, value in zip(matrix, rhs)]
    size = 3
    for column in range(size):
        pivot = max(range(column, size), key=lambda r: abs(augmented[r][column]))
        if abs(augmented[pivot][column]) < _INVERTIBLE_DETERMINANT_MIN:
            raise ValueError("calibration points are collinear or degenerate")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column] / augmented[column][column]
            for col in range(column, size + 1):
                augmented[row][col] -= factor * augmented[column][col]
    return tuple(augmented[i][size] / augmented[i][i] for i in range(size))  # type: ignore[return-value]


def fit_affine(samples: list[tuple[tuple[float, float], tuple[float, float]]]
               ) -> tuple[tuple[float, float, float, float], tuple[float, float]]:
    """最小二乘拟合 HID(u,v) → 屏幕(X,Y) 的仿射变换。

    返回 ``(matrix, offset)``：``X = a*u + b*v + tx``、``Y = c*u + d*v + ty``。
    少于 3 个不共线采样点时抛 ``ValueError``。
    """
    if len(samples) < 3:
        raise ValueError("affine calibration needs at least 3 probe points")
    sum_uu = sum_uv = sum_vv = sum_u = sum_v = 0.0
    sum_ux = sum_vx = sum_x = 0.0
    sum_uy = sum_vy = sum_y = 0.0
    for (u, v), (x, y) in samples:
        sum_uu += u * u
        sum_uv += u * v
        sum_vv += v * v
        sum_u += u
        sum_v += v
        sum_ux += u * x
        sum_vx += v * x
        sum_x += x
        sum_uy += u * y
        sum_vy += v * y
        sum_y += y
    count = float(len(samples))
    normal = [
        [sum_uu, sum_uv, sum_u],
        [sum_uv, sum_vv, sum_v],
        [sum_u, sum_v, count],
    ]
    a, b, tx = _solve3(normal, [sum_ux, sum_vx, sum_x])
    c, d, ty = _solve3(normal, [sum_uy, sum_vy, sum_y])
    return (a, b, c, d), (tx, ty)


def _finite_number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"AOA calibration {name} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"AOA calibration {name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"AOA calibration {name} must be a finite number")
    return number


# ---- TouchMapping：可序列化的设备触摸映射 ----

@dataclass(frozen=True)
class TouchMapping:
    """一台设备当前显示配置下的 HID↔屏幕 仿射映射与验证结论。

    ``matrix``/``offset`` 描述 HID 逻辑坐标 → 屏幕像素 的正向变换；
    ``samples`` 是标定采样（含单点残差，供误差审计）；``context`` 是
    ``worker_agent.aoa_calibration.display_context`` 采集的显示环境快照，
    作为复用匹配键（model + release + display + rotation + hid_touch_mode）。
    """

    hid_touch_mode: str
    matrix: tuple[float, float, float, float]
    offset: tuple[float, float]
    mapping_policy: str = ""
    samples: tuple[dict, ...] = field(default_factory=tuple)
    rms_error_px: float = 0.0
    max_error_px: float = 0.0
    context: dict = field(default_factory=dict)
    display_pixels: tuple[int, int] = (0, 0)

    def __post_init__(self):
        a, b, c, d = self.matrix
        determinant = a * d - b * c
        if (not all(math.isfinite(v) for v in (*self.matrix, *self.offset))
                or not math.isfinite(determinant)
                or abs(determinant) < _INVERTIBLE_DETERMINANT_MIN):
            raise ValueError("AOA calibration matrix is not invertible")

    # ---- 正向/逆向换算 ----

    def hid_to_screen(self, u: float, v: float) -> tuple[float, float]:
        a, b, c, d = self.matrix
        tx, ty = self.offset
        return (a * u + b * v + tx, c * u + d * v + ty)

    def screen_to_hid(self, x: float, y: float, *, strict: bool = False) -> tuple[int, int]:
        """屏幕像素 → HID；自动转换使用 strict，人工调试可保留 clamp。"""
        a, b, c, d = self.matrix
        determinant = a * d - b * c
        if abs(determinant) < _INVERTIBLE_DETERMINANT_MIN:
            raise ValueError("AOA calibration matrix is not invertible")
        tx, ty = self.offset
        dx, dy = x - tx, y - ty
        u = (d * dx - b * dy) / determinant
        v = (a * dy - c * dx) / determinant
        if not all(math.isfinite(n) for n in (x, y, u, v)):
            raise ValueError("AOA coordinate must be finite")
        if strict and not (-1e-6 <= u <= AOA_TOUCH_MAX_X + 1e-6
                           and -1e-6 <= v <= AOA_TOUCH_MAX_Y + 1e-6):
            raise ValueError("AOA target point is outside the reachable HID region; recalibrate or adapt the profile")
        return (
            min(max(round(u), 0), AOA_TOUCH_MAX_X),
            min(max(round(v), 0), AOA_TOUCH_MAX_Y),
        )

    # ---- 序列化 ----

    def to_dict(self) -> dict:
        return {
            "hid_touch_mode": self.hid_touch_mode,
            "mapping_policy": self.effective_policy,
            "matrix": [round(value, 8) for value in self.matrix],
            "offset": [round(value, 8) for value in self.offset],
            "display_pixels": list(self.display_pixels),
            "rms_error_px": round(self.rms_error_px, 3),
            "max_error_px": round(self.max_error_px, 3),
            "samples": [dict(sample) for sample in self.samples],
            "context": dict(self.context),
            "verified": self.verified,
        }

    @classmethod
    def from_dict(cls, data) -> TouchMapping:
        if not isinstance(data, dict):
            raise ValueError("AOA touch mapping must be an object")
        mode = data.get("hid_touch_mode")
        if mode not in ("pointer", "direct"):
            raise ValueError("AOA touch mapping requires hid_touch_mode pointer or direct")
        matrix_raw = data.get("matrix")
        if not isinstance(matrix_raw, list) or len(matrix_raw) != 4:
            raise ValueError("AOA touch mapping matrix must be four finite numbers")
        matrix = tuple(_finite_number(v, "matrix") for v in matrix_raw)
        offset_raw = data.get("offset", [0.0, 0.0])
        if not isinstance(offset_raw, list) or len(offset_raw) != 2:
            raise ValueError("AOA touch mapping offset must be two finite numbers")
        offset = tuple(_finite_number(v, "offset") for v in offset_raw)
        display_raw = data.get("display_pixels", [0, 0])
        if (not isinstance(display_raw, list) or len(display_raw) != 2
                or any(type(v) is not int or v < 0 or v > 10000 for v in display_raw)):
            raise ValueError("AOA touch mapping display_pixels must be two integers")
        samples_raw = data.get("samples", [])
        if not isinstance(samples_raw, list) or len(samples_raw) > 32 or any(not isinstance(s, dict) for s in samples_raw):
            raise ValueError("AOA touch mapping samples must be a list")
        context = data.get("context", {})
        if not isinstance(context, dict):
            raise ValueError("AOA touch mapping context must be an object")
        rms = _finite_number(data.get("rms_error_px", 0.0), "rms_error_px")
        max_error = _finite_number(data.get("max_error_px", 0.0), "max_error_px")
        if rms < 0 or max_error < 0:
            raise ValueError("AOA touch mapping errors must be non-negative")
        mapping = cls(
            hid_touch_mode=mode,
            matrix=matrix,  # type: ignore[arg-type]
            offset=offset,  # type: ignore[arg-type]
            mapping_policy=validate_mapping_policy(data.get("mapping_policy")),
            samples=tuple(dict(sample) for sample in samples_raw
                          if isinstance(sample, dict)),
            rms_error_px=rms,
            max_error_px=max_error,
            context=context,
            display_pixels=(display_raw[0], display_raw[1]),
        )
        if mapping.samples:
            errors = mapping.sample_errors()
            rms = math.sqrt(sum(e * e for e in errors) / len(errors))
            if abs(max(errors) - max_error) > 0.15 or abs(rms - mapping.rms_error_px) > 0.15:
                raise ValueError("AOA calibration sample errors do not match the matrix")
        if mapping.context.get("display") is not None and list(mapping.display_pixels) != mapping.context["display"]:
            raise ValueError("AOA calibration display context does not match display_pixels")
        if mapping.context.get("hid_touch_mode", mode) != mode:
            raise ValueError("AOA calibration context belongs to another HID mode")
        if mapping.mapping_policy == MAPPING_POLICY_DIRECT_STANDARD:
            # A fitted affine matrix is a device correction, not evidence of
            # standard InputReader classification/display routing.
            raise ValueError("direct_standard requires device compatibility verification; use direct_calibrated")
        if mapping.mapping_policy in (MAPPING_POLICY_DIRECT_CALIBRATED, MAPPING_POLICY_POINTER_CALIBRATED) and not mapping.verified:
            raise ValueError("calibrated mapping requires independent validation samples")
        return mapping

    def sample_errors(self) -> list[float]:
        errors = []
        width, height = self.display_pixels
        for sample in self.samples:
            if not isinstance(sample, dict):
                raise ValueError("AOA calibration sample must be an object")
            hid, cooked = sample.get("hid"), sample.get("cooked")
            if (not isinstance(hid, (list, tuple)) or len(hid) != 2
                    or not isinstance(cooked, (list, tuple)) or len(cooked) != 2):
                raise ValueError("AOA calibration sample requires hid and cooked points")
            u, v = (_finite_number(n, "sample hid") for n in hid)
            x, y = (_finite_number(n, "sample cooked") for n in cooked)
            if not (0 <= u <= AOA_TOUCH_MAX_X and 0 <= v <= AOA_TOUCH_MAX_Y):
                raise ValueError("AOA calibration sample HID point is out of range")
            if width > 0 and height > 0 and not (-8 <= x <= width + 8 and -8 <= y <= height + 8):
                raise ValueError("AOA calibration sample is outside the display")
            px, py = self.hid_to_screen(u, v)
            errors.append(math.hypot(px - x, py - y))
        return errors

    # ---- 派生结论 ----

    @property
    def verified(self) -> bool:
        """映射误差在验收门槛内，且策略不是 unsupported。"""
        if self.mapping_policy == MAPPING_POLICY_UNSUPPORTED:
            return False
        try:
            training = [s for s in self.samples if not s.get("validation", False)]
            validation = [s for s in self.samples if s.get("validation") is True]
            if len(training) < 3 or len(validation) < 2 or min(self.display_pixels) <= 0:
                return False
            fit_affine([(tuple(s["hid"]), tuple(s["cooked"])) for s in training])
            training_points = {tuple(s["hid"]) for s in training}
            if len({tuple(s["hid"]) for s in validation}) < 2:
                return False
            if any(tuple(s["hid"]) in training_points for s in validation):
                return False
            return max(self.sample_errors()) <= MAX_CALIBRATION_ERROR_PX
        except (ValueError, TypeError, KeyError):
            return False

    @property
    def effective_policy(self) -> str:
        return self.mapping_policy or policy_for(self.hid_touch_mode, self.verified)


def mapping_from_samples(hid_touch_mode: str,
                         samples: list[tuple[tuple[float, float], tuple[float, float]]],
                         context: dict | None = None,
                         display_pixels: tuple[int, int] = (0, 0),
                         validation_samples: list | None = None,
                         ) -> TouchMapping:
    """从实测采样构造 TouchMapping：拟合、计算残差、推导 policy。"""
    matrix, offset = fit_affine(samples)
    a, b, c, d = matrix
    tx, ty = offset
    errors = []
    all_samples = samples + list(validation_samples or [])
    for (u, v), (x, y) in all_samples:
        px, py = a * u + b * v + tx, c * u + d * v + ty
        errors.append(math.hypot(px - x, py - y))
    max_error = max(errors)
    verified = max_error <= MAX_CALIBRATION_ERROR_PX and len(validation_samples or []) >= 2
    rms = math.sqrt(sum(error * error for error in errors) / len(errors))
    sample_records = [
        {"hid": [int(u), int(v)], "cooked": [round(float(x), 2), round(float(y), 2)],
         "error_px": round(error, 2), "validation": index >= len(samples)}
        for index, (((u, v), (x, y)), error) in enumerate(zip(all_samples, errors))
    ]
    return TouchMapping(
        hid_touch_mode=hid_touch_mode,
        matrix=matrix,
        offset=offset,
        mapping_policy=policy_for(hid_touch_mode, verified),
        samples=tuple(sample_records),
        rms_error_px=rms,
        max_error_px=max_error,
        context=dict(context or {}),
        display_pixels=display_pixels,
    )


def per_axis_mapping(display_width: int, display_height: int,
                     hid_touch_mode: str = "direct",
                     mapping_policy: str = MAPPING_POLICY_DIRECT_UNVERIFIED,
                     ) -> TouchMapping:
    """按轴 AOSP 估算映射（分母 max-min+1）：免标定的初始坐标。

    结果一律视为 unverified——分辨率换算不能证明设备端 InputReader 分类、
    显示绑定与实际落点（ADR 0026 Context）。
    """
    if display_width <= 0 or display_height <= 0:
        raise ValueError("display size must be positive")
    from foundation.aoa_hid_mode import validate_touch_mode

    validate_touch_mode(hid_touch_mode)
    policy = validate_mapping_policy(mapping_policy) or MAPPING_POLICY_DIRECT_UNVERIFIED
    if hid_touch_mode == "pointer" and policy == MAPPING_POLICY_DIRECT_UNVERIFIED:
        policy = MAPPING_POLICY_ESTIMATED
    if policy in (MAPPING_POLICY_DIRECT_STANDARD, MAPPING_POLICY_DIRECT_CALIBRATED, MAPPING_POLICY_POINTER_CALIBRATED,
                  MAPPING_POLICY_UNSUPPORTED):
        raise ValueError("per-axis estimate cannot claim a verified mapping policy")
    return TouchMapping(
        hid_touch_mode=hid_touch_mode,
        matrix=(display_width / (AOA_TOUCH_MAX_X + 1), 0.0,
                0.0, display_height / (AOA_TOUCH_MAX_Y + 1)),
        offset=(0.0, 0.0),
        mapping_policy=policy,
        display_pixels=(display_width, display_height),
    )


# ---- 跨设备归一化：Profile 坐标重投影 ----

def convert_point(source: TouchMapping, target: TouchMapping, u: int, v: int) -> tuple[int, int]:
    """源设备的逻辑坐标 → 参考像素 → 目标设备的逻辑坐标。"""
    pixel_x, pixel_y = source.hid_to_screen(u, v)
    sw, sh = source.display_pixels
    tw, th = target.display_pixels
    if min(sw, sh, tw, th) <= 0:
        raise ValueError("missing display dimensions for AOA reprojection")
    if source.context.get("rotation", 0) != target.context.get("rotation", 0):
        raise ValueError("AOA reprojection requires matching display rotation")
    if not (0 <= u <= AOA_TOUCH_MAX_X and 0 <= v <= AOA_TOUCH_MAX_Y
            and -1e-6 <= pixel_x < sw and -1e-6 <= pixel_y < sh):
        raise ValueError("AOA source point is outside the display or HID region")
    return target.screen_to_hid(pixel_x * tw / sw, pixel_y * th / sh, strict=True)


def convert_commands(commands: list[str], source: TouchMapping,
                     target: TouchMapping) -> list[str]:
    """把 AOA 命令序列中的触摸坐标从源映射重投影到目标映射。

    key/write/sleep/wake/home/back 等无坐标命令原样保留；点击/滑动经
    "源映射→像素→目标映射"换算后重新走官方语法边界（越界即抛错）。
    """
    converted: list[str] = []
    for raw in commands:
        text = str(raw).strip()
        match = _TOUCH_COMMAND_RE.match(text)
        if not match:
            converted.append(text)
            continue
        kind = match.group("kind")
        args = (match.group("args") or "").split()
        if kind == "swipe":
            if len(args) != 5:
                raise ValueError(f"invalid AOA action: {text}")
            x1, y1, ms, x2, y2 = args
            nx1, ny1 = convert_point(source, target, int(x1), int(y1))
            nx2, ny2 = convert_point(source, target, int(x2), int(y2))
            converted.append(f"swipe {nx1} {ny1} {ms} {nx2} {ny2}")
        else:
            if len(args) != 2:
                raise ValueError(f"invalid AOA action: {text}")
            nx, ny = convert_point(source, target, int(args[0]), int(args[1]))
            converted.append(f"{kind} {nx} {ny}")
    return converted


# ---- 标定记录复用匹配 ----

def complete_context(context: dict) -> bool:
    """Unknown environment values cannot establish device compatibility."""
    if not isinstance(context, dict):
        return False
    for key in ("model", "release", "fingerprint", "kernel", "display_route"):
        value = context.get(key)
        if (not isinstance(value, str) or not value.strip()
                or value.strip().lower() in {"unknown", "none", "null", "未知"}):
            return False
    display = context.get("display")
    return (type(context.get("rotation")) is int and context["rotation"] in range(4)
            and type(context.get("density")) is int and 0 < context["density"] <= 10000
            and context.get("hid_touch_mode") in ("pointer", "direct")
            and isinstance(display, list) and len(display) == 2
            and all(type(v) is int and 0 < v <= 10000 for v in display))


def context_matches(record_context: dict, query: dict) -> bool:
    """设备环境匹配键：model/release/display/rotation/hid_touch_mode 全等。

    Android 版本、显示配置、旋转或输入模式任一变化都可能改变映射
    （ADR 0026 Consequences），不满足条件时必须重新标定而不是复用。
    """
    if not complete_context(record_context) or not complete_context(query):
        return False
    for key in ("model", "release", "rotation", "hid_touch_mode", "fingerprint", "density", "kernel", "display_route"):
        if record_context[key] != query[key]:
            return False
    record_display = record_context.get("display")
    query_display = query.get("display")
    if not (isinstance(record_display, list) and len(record_display) == 2
            and isinstance(query_display, list) and len(query_display) == 2
            and all(type(v) is int and 0 < v <= 10000 for v in (*record_display, *query_display))
            and record_display == query_display):
        return False
    return True


def mapping_options(options: dict) -> dict:
    """Validate the bounded mapping envelope at Controller and Worker boundaries."""
    import json

    mode = options.get("aoa_hid_touch_mode", "pointer")
    if options.get("aoa_semantic_plan") is not None:
        from .aoa_semantics import validate_plan

        if (mode != "direct" or options.get("aoa_touch_mapping") is not None
                or options.get("aoa_source_touch_mapping") is not None
                or options.get("aoa_mapping_policy", "") not in ("", "direct_standard")):
            raise ValueError("certified semantic input requires direct mode and its own verified class transform")
        validate_plan(options["aoa_semantic_plan"], options.get("aoa_direct_certificate"))
        digest = options.get("aoa_firmware_sha256", "")
        if not isinstance(digest, str) or (digest and not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise ValueError("invalid semantic firmware SHA-256")
        return {"aoa_semantic_plan": options["aoa_semantic_plan"],
                "aoa_direct_certificate": options["aoa_direct_certificate"],
                "aoa_firmware_sha256": digest, "aoa_mapping_policy": "direct_standard"}
    result = {}
    for key in ("aoa_touch_mapping", "aoa_source_touch_mapping"):
        raw = options.get(key)
        if raw is None:
            continue
        if len(json.dumps(raw, allow_nan=False).encode()) > 65536:
            raise ValueError("AOA touch mapping exceeds 65536 bytes")
        mapping = TouchMapping.from_dict(raw)
        if mapping.hid_touch_mode != mode:
            raise ValueError("AOA touch mapping belongs to another HID mode")
        result[key] = mapping.to_dict()
    policy = validate_mapping_policy(options.get("aoa_mapping_policy"))
    if policy == MAPPING_POLICY_DIRECT_STANDARD:
        raise ValueError("direct_standard requires device compatibility verification")
    if policy.startswith("direct_") and mode != "direct":
        raise ValueError("direct mapping policy requires direct HID mode")
    if policy == MAPPING_POLICY_POINTER_CALIBRATED and mode != "pointer":
        raise ValueError("pointer_calibrated requires pointer HID mode")
    target = result.get("aoa_touch_mapping")
    if target and policy and target["mapping_policy"] != policy:
        raise ValueError("AOA mapping policy does not match target mapping")
    if policy in (MAPPING_POLICY_DIRECT_CALIBRATED, MAPPING_POLICY_POINTER_CALIBRATED) and target is None:
        raise ValueError("calibrated mapping policy requires a target mapping")
    result["aoa_mapping_policy"] = policy or (target or {}).get("mapping_policy", "")
    return result
