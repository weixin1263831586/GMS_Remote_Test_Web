"""Bounded UI observations, with an operator-bound camera for pre-ADB pages."""
from __future__ import annotations

import importlib.util
import json
import math
import os
import re
import shutil
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from foundation.aoa_display import input_displays, rect
from foundation.aoa_semantics import IDENTIFIER, validate_condition
from foundation.processes import run_local_command

from .aoa_usb import bind_usb, bound_port, validate_binding


class ObservationUnavailableError(RuntimeError):
    """A missing observation is never evidence that a UI element is absent."""


def capture_capabilities():
    available = shutil.which("ffmpeg") is not None and importlib.util.find_spec("PIL") is not None
    bindings = {}
    try:
        raw = os.environ.get("GMS_WORKER_AOA_CAPTURE_BINDINGS", "{}")
        configured = json.loads(raw) if len(raw) <= 65536 else {}
        if isinstance(configured, dict):
            for key in configured:
                if not isinstance(key, str) or "@" not in key:
                    continue
                serial, port = key.rsplit("@", 1)
                try:
                    with bind_usb(port):
                        binding = capture_binding(serial)
                    bindings[key] = {"id": binding["id"], "context": binding["context"]}
                except ObservationUnavailableError:
                    continue
    except (ValueError, TypeError):
        pass
    return {"aoa_visual_capture_v1": available, "aoa_capture_bindings": bindings}


def capture_binding(serial, context=None):
    try:
        raw = os.environ.get("GMS_WORKER_AOA_CAPTURE_BINDINGS", "{}")
        if len(raw) > 65536 or not bound_port():
            raise ValueError("missing physical capture identity")
        binding = json.loads(raw).get(f"{serial}@{bound_port()}")
        if not isinstance(binding, dict) or not re.fullmatch(r"/dev/video\d+", str(binding.get("device"))):
            raise ValueError("no trusted capture device for this serial and USB port")
        if not isinstance(binding.get("id"), str) or not IDENTIFIER.fullmatch(binding["id"]):
            raise ValueError("invalid capture binding ID")
        for key in ("width", "height"):
            if type(binding.get(key)) is not int or not 64 <= binding[key] <= 4096:
                raise ValueError("invalid capture dimensions")
        if type(binding.get("rotate_quarters", 0)) is not int or binding.get("rotate_quarters", 0) not in range(4):
            raise ValueError("invalid capture rotation")
        rect(binding.get("crop", [0, 0, 1, 1]), normalized=True)
        identity = binding.get("context")
        required = ("display", "rotation", "display_route", "display_id")
        if not isinstance(identity, dict) or any(key not in identity for key in required):
            raise ValueError("capture display association is incomplete")
        if context and any(identity[key] != context.get(key) for key in required):
            raise ValueError("capture source is bound to another display configuration")
        return binding
    except (ValueError, TypeError, AttributeError) as exc:
        raise ObservationUnavailableError(f"外部画面源不可用：{exc}") from exc


def capture_fingerprint(binding, roi, timeout=5):
    try:
        from PIL import Image
    except ImportError as exc:
        raise ObservationUnavailableError("Worker 缺少 Pillow，请安装外部画面观察依赖") from exc

    validate_binding()
    with tempfile.TemporaryDirectory(prefix="gms-aoa-frame-") as directory:
        image_path = Path(directory) / "frame.png"
        result = run_local_command(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "v4l2",
            "-video_size", f"{binding['width']}x{binding['height']}", "-i", binding["device"],
            "-frames:v", "1", "-threads", "1", str(image_path)], timeout=max(1, math.ceil(timeout)))
        validate_binding()
        if not result.ok or not image_path.exists() or image_path.stat().st_size > 32 * 1024 * 1024:
            raise ObservationUnavailableError("无法从已绑定外部画面源取得有效画面")
        with Image.open(image_path) as source:
            if source.size != (binding["width"], binding["height"]):
                raise ObservationUnavailableError("外部画面尺寸与绑定不一致")
            def crop(image, bounds):
                left, top, right, bottom = rect(bounds, normalized=True)
                box = (round(left * image.width), round(top * image.height),
                       round(right * image.width), round(bottom * image.height))
                if box[2] <= box[0] or box[3] <= box[1]:
                    raise ObservationUnavailableError("画面参考区域过小")
                return image.crop(box)

            image = crop(source, binding.get("crop", [0, 0, 1, 1]))
            image = image.rotate(-90 * binding.get("rotate_quarters", 0), expand=True)
            image = crop(image, roi).convert("L").resize((16, 16), Image.Resampling.BOX)
            return image.tobytes().hex()


def capture_reference(serial, roi):
    binding = capture_binding(serial)
    return {"success": True, "serial": serial, "condition": {"type": "visual", "roi": roi,
        "fingerprint": capture_fingerprint(binding, roi), "max_delta": 3, "binding_id": binding["id"]},
        "display_context": binding["context"]}


class Observer:
    def __init__(self, serial, context):
        self.serial, self.context = serial, context

    def adb_state(self):
        from .aoa_actions import _wait_adb_state

        state = _wait_adb_state(self.serial, 0).get("adb_state")
        if state not in {"device", "unauthorized", "offline", "absent"}:
            raise ObservationUnavailableError(f"ADB 观察失败：{state}")
        return state

    def verify_context(self):
        """Recheck changeable geometry before input; dispatch inventory is a snapshot."""
        from .aoa_actions import _adb_run

        if self.adb_state() != "device":
            capture_binding(self.serial, self.context)
            return
        display_id = self.context["display_id"]
        dump = _adb_run(self.serial, "shell", "dumpsys", "input", timeout=5)
        size = _adb_run(self.serial, "shell", "wm", "size", "-d", str(display_id), timeout=5)
        density = _adb_run(self.serial, "shell", "wm", "density", "-d", str(display_id), timeout=5)
        displays = input_displays(dump.stdout or "")
        actual = next((item for item in displays if item["display_id"] == display_id), None)
        sizes = re.findall(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", size.stdout or "")
        densities = re.findall(r"(?:Physical|Override) density:\s*(\d+)", density.stdout or "")
        if (dump.returncode or size.returncode or density.returncode or not actual or not sizes or not densities
                or any(actual[key] != self.context[key] for key in ("display", "display_route", "rotation", "display_id"))
                or list(map(int, sizes[-1])) != self.context["display"] or int(densities[-1]) != self.context["density"]):
            raise ObservationUnavailableError("目标显示尺寸、方向、密度或显示路由已经变化，请重新匹配语义模板")

    def nodes(self, timeout=5):
        from .aoa_actions import _adb_run

        if self.context.get("display_id") != 0:
            raise ObservationUnavailableError("uiautomator 无法证明非默认显示器，请使用绑定外部画面源")
        if self.adb_state() != "device":
            raise ObservationUnavailableError("ADB 未授权；此页面断言需要已绑定外部画面源")
        remote = f"/data/local/tmp/gms-aoa-observe-{uuid.uuid4().hex}.xml"
        try:
            dumped = _adb_run(self.serial, "shell", "uiautomator", "dump", remote, timeout=max(1, math.ceil(timeout)))
            if dumped.returncode:
                raise ObservationUnavailableError("无法取得当前页面结构")
            read = _adb_run(self.serial, "exec-out", "cat", remote, timeout=max(1, math.ceil(timeout)))
            if read.returncode or len(read.stdout or "") > 2 * 1024 * 1024:
                raise ObservationUnavailableError("页面结构为空或过大")
            root = ET.fromstring(read.stdout)
            result = []
            for node in root.iter("node"):
                attrs = node.attrib
                bounds = [int(value) for value in re.findall(r"-?\d+", attrs.get("bounds", ""))]
                result.append({"text": attrs.get("text", ""), "resource_id": attrs.get("resource-id", ""),
                    "content_desc": attrs.get("content-desc", ""), "checked": attrs.get("checked") == "true",
                    "enabled": attrs.get("enabled") == "true", "bounds": bounds})
            if not result:
                raise ObservationUnavailableError("页面结构没有可观察元素，无法判断控件是否存在")
            return result
        except (ValueError, ET.ParseError) as exc:
            raise ObservationUnavailableError("页面结构无效") from exc
        finally:
            _adb_run(self.serial, "shell", "rm", "-f", remote, timeout=3)

    def evaluate(self, condition, timeout=5):
        validate_condition(condition)
        cache = {}

        def evaluate(term):
            kind = term["type"]
            if kind in {"all", "any"}:
                outcomes, unavailable = [], []
                for child in term["conditions"]:
                    try:
                        outcomes.append(evaluate(child))
                    except ObservationUnavailableError as exc:
                        unavailable.append(exc)
                if kind == "any" and any(outcomes):
                    return True
                if kind == "all" and False in outcomes:
                    return False
                if unavailable:
                    raise unavailable[0]
                return all(outcomes) if kind == "all" else any(outcomes)
            if kind == "adb_state":
                if "adb" not in cache:
                    cache["adb"] = self.adb_state()
                return cache["adb"] == term["state"]
            if kind == "ui":
                if "nodes" not in cache:
                    cache["nodes"] = self.nodes(timeout)
                matches = self.select(cache["nodes"], term["selector"])
                return bool(matches) == term.get("exists", True)
            binding = capture_binding(self.serial, self.context)
            if binding["id"] != term["binding_id"]:
                raise ObservationUnavailableError("画面参考与当前绑定不一致")
            key = tuple(term["roi"])
            if key not in cache:
                cache[key] = capture_fingerprint(binding, term["roi"], timeout)
            actual, reference = bytes.fromhex(cache[key]), bytes.fromhex(term["fingerprint"])
            return sum(abs(a - b) for a, b in zip(actual, reference)) / 256 <= term.get("max_delta", 3)

        validate_binding()
        result = evaluate(condition)
        validate_binding()
        return result

    @staticmethod
    def select(nodes, selector):
        matches = [node for node in nodes if all(node.get(key) == value for key, value in selector.items())]
        if len(matches) > 1:
            raise ObservationUnavailableError("页面元素不唯一，停止自动点击")
        return matches
