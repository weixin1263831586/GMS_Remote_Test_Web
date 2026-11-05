"""Execute a certified plan, checking actual state around each single input."""
from __future__ import annotations

import logging
import time

from foundation.aoa_certification import matching_case
from foundation.aoa_display import rect, standard_inverse, standard_point
from foundation.aoa_profile import MAX_AOA_OPERATION_SECONDS, parse_profile_commands
from foundation.aoa_semantics import validate_plan

from .aoa_control import register, unregister, wait_if_paused
from .aoa_observation import ObservationUnavailableError, Observer
from .aoa_usb import validate_binding


logger = logging.getLogger(__name__)


def resolve_live_command(command, step, layout, case, context, observer, timeout):
    """Exact selectors adapt to measured control bounds; ambiguous nodes stop."""
    parsed = parse_profile_commands([command])[0]
    names = ([step["target"]] if parsed.kind in {"click", "longClick"} else
             [step["from_target"], step["to_target"]] if parsed.kind == "swipe" else [])
    if not any(layout["targets"][name].get("selector") for name in names):
        return command
    nodes = observer.nodes(timeout)
    points = []
    for name in names:
        target = layout["targets"][name]
        if not target.get("selector"):
            raise ObservationUnavailableError("动态滑动的起点和终点均须配置唯一元素选择器")
        matches = observer.select(nodes, target["selector"])
        if len(matches) != 1:
            raise ObservationUnavailableError("目标元素不存在，停止自动点击")
        left, top, right, bottom = rect(matches[0]["bounds"])
        if case["max_error_px"] > min(8, (right - left) / 4, (bottom - top) / 4):
            raise ObservationUnavailableError("当前控件过小，超过实测输入误差的安全范围")
        frame = layout.get("frame", [0, 0, 1, 1])
        x, y = (left + right) / 2, (top + bottom) / 2
        width, height = context["display"]
        if not (frame[0] * width < x < frame[2] * width and frame[1] * height < y < frame[3] * height):
            raise ObservationUnavailableError("目标元素位于已验证应用窗口之外")
        point = standard_inverse(x, y, context["display"], case["axis_rotation"], case["input"]["viewport"])
        cooked = standard_point(*point, context["display"], case["axis_rotation"], case["input"]["viewport"])
        error = ((cooked[0] - x) ** 2 + (cooked[1] - y) ** 2) ** 0.5 + case["max_error_px"]
        if error > min(8, (right - left) / 4, (bottom - top) / 4):
            raise ObservationUnavailableError("控件中心量化后的误差超过安全范围")
        points.append(point)
    if parsed.kind == "swipe":
        return f"swipe {points[0][0]} {points[0][1]} {parsed.duration_ms} {points[1][0]} {points[1][1]}"
    return f"{parsed.kind} {points[0][0]} {points[0][1]}"


def run_semantic_sequence(serial, commands, options, *, on_event=None, on_progress=None,
                          observer=None, sleep=time.sleep, clock=time.monotonic):
    from .aoa_actions import AoaError, _wait_adb_state, run_aoa_sequence

    plan = validate_plan(options.get("aoa_semantic_plan"), options.get("aoa_direct_certificate"))
    if list(commands) != plan["source_commands"]:
        raise AoaError("semantic source commands differ from the authenticated plan")
    context = plan["context"]
    observer = observer or Observer(serial, context)
    case = matching_case(options["aoa_direct_certificate"], context)
    layout = next(item for item in plan["template"]["layouts"] if item["id"] == plan["layout_id"])
    control_id = options.get("playback_control_id") or ""
    parsed = parse_profile_commands(plan["commands"])
    deadline = clock() + MAX_AOA_OPERATION_SECONDS - int(options.get("wait_adb_seconds") or 0)
    index, completed, skipped, verified, sent = 0, 0, 0, 0, False
    last_input = (0, "")
    stage = "checking"

    def event(phase):
        if on_event:
            # 进度回调只报告，不参与执行：回调异常不得顶掉原始 AoaError。
            try:
                on_event({"phase": phase, "index": index, "total": len(commands),
                          "command": commands[index - 1] if index else "", "semantic": True})
            except Exception:
                logger.warning("semantic AOA progress event failed", exc_info=True)

    def tick():
        validate_binding()
        if clock() >= deadline:
            raise AoaError("semantic AOA playback time budget exceeded")

    def gate():
        nonlocal deadline
        tick()
        if control_id:
            deadline += wait_if_paused(control_id, index=index, total=len(commands), sleep=sleep,
                on_event=lambda item: event(item["event_type"]), on_tick=validate_binding,
                read_source=options.get("_aoa_control_source"))
        tick()

    def wait_condition(condition, timeout):
        if condition is None:
            return
        until = clock() + timeout
        unavailable = None
        while True:
            before_gate = clock()
            gate()
            until += clock() - before_gate
            try:
                if observer.evaluate(condition, max(0.1, min(5, until - clock()))):
                    return
            except ObservationUnavailableError as exc:
                unavailable = exc
            tick()
            if clock() >= until:
                raise AoaError(str(unavailable) if unavailable else "语义页面断言超时；请检查当前页面与模板")
            sleep(min(0.25, max(0, until - clock())))

    if control_id:
        register(control_id)
    try:
        for index, (command, step, item) in enumerate(zip(plan["commands"], plan["template"]["steps"], parsed), 1):
            sent, stage = False, "checking"
            gate()
            event("checking")
            timeout = step.get("timeout_seconds", 3)
            if step.get("skip_if"):
                try:
                    already_done = observer.evaluate(step["skip_if"], min(5, timeout))
                except ObservationUnavailableError:
                    already_done = False
                if already_done:
                    skipped += 1
                    completed += 1
                    event("skipped")
                    continue
            wait_condition(step.get("before"), timeout)
            gate()
            if item.kind == "sleep":
                until = clock() + item.sleep_ms / 1000
                while clock() < until:
                    before_gate = clock()
                    gate()
                    until += clock() - before_gate
                    sleep(min(0.2, max(0, until - clock())))
            else:
                if hasattr(observer, "verify_context"):
                    observer.verify_context()
                gate()
                command = resolve_live_command(command, step, layout, case, context, observer, timeout)
                stage = "input"
                event("running")
                # The proof was validated above; this inner transport receives already compiled HID coordinates.
                sent = True
                receipt = run_aoa_sequence(serial, [command], touch_mode="direct", mapping_policy="direct_unverified",
                    hid_settle_seconds=options.get("aoa_hid_settle_seconds", 2), _deadline=deadline,
                    suppress_adb_daemon=bool(options.get("suppress_adb_daemon")),
                    suppress_adb_daemon_gate=options.get("suppress_adb_daemon_gate"))
                if receipt.get("success") is not True or receipt.get("cleanup_errors") or receipt.get("native_mode_restored") is False:
                    error = AoaError("AOA 单步 USB 清理未完成，停止语义回放并核对设备状态")
                    error.cleanup_errors = receipt.get("cleanup_errors", [])
                    raise error
                last_input = (index, commands[index - 1])
            stage = "verifying"
            event("verifying")
            wait_condition(step.get("after"), timeout)
            completed += 1
            verified += int(step.get("after") is not None)
            event("completed")
            if on_progress:
                on_progress(index, len(commands), commands[index - 1])
        result = {"success": True, "serial": serial, "commands_total": len(commands),
            "commands_executed": completed, "verified_steps": verified, "skipped_steps": skipped,
            "mapping_policy": "direct_standard", "certificate_id": plan["certificate_id"],
            "layout_id": plan["layout_id"], "native_mode_restored": True, "cleanup_errors": []}
        if options.get("wait_adb_seconds"):
            result.update(_wait_adb_state(serial, int(options["wait_adb_seconds"])))
            result["success"] = result.get("adb_recovered") is True
        return result
    except Exception as exc:
        original = getattr(exc, "aoa_result", {})
        error = exc if isinstance(exc, AoaError) else AoaError(str(exc))
        error.cancelled = getattr(exc, "cancelled", False)
        error.cleanup_errors = getattr(exc, "cleanup_errors", [])
        error.aoa_result = {**original, "success": False, "commands_total": len(commands),
            "commands_executed": completed, "failed_index": index, "failed_command": commands[index - 1] if index else "",
            "failed_input_state": original.get("failed_input_state", "uncertain" if sent else "not_sent"),
            "last_input_index": last_input[0], "last_input_command": last_input[1],
            "semantic_stage": stage, "mapping_policy": "direct_standard", "reconcile_required": True}
        event("failed")
        raise error from exc
    finally:
        if control_id:
            unregister(control_id)
