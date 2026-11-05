"""Replay with the same USB/HID lifecycle as independent single-step debugging."""
from __future__ import annotations

import logging
import time

from foundation.aoa_profile import MAX_AOA_OPERATION_SECONDS

from .aoa_control import register, unregister, wait_if_paused
from .aoa_diagnostics import record_failure


logger = logging.getLogger(__name__)


def run_step_sessions(serial, commands, parsed, *, run_one, wait_adb,
                      validate, error_type, sleep, wait_adb_seconds=0,
                      on_event=None, on_progress=None, control_id="", control_source=None,
                      on_control_event=None, touch_mode="pointer", hid_settle_seconds=2):
    # USB lifecycle overhead is substantial. Bound the complete operation,
    # including pauses and the final ADB wait, by the existing watchdog budget.
    deadline = time.monotonic() + MAX_AOA_OPERATION_SECONDS - wait_adb_seconds
    total, completed, reconnects, index = len(commands), 0, 0, 0
    last_input = None
    input_attempted = False

    def emit(phase, step, text=""):
        event = {"phase": phase, "index": step, "total": total,
                 "command": text or (commands[step - 1] if step else "")}
        if on_event:
            try:
                on_event(event)
            except Exception:
                logger.warning("AOA per-command progress failed", exc_info=True)

    def check():
        validate()
        if time.monotonic() >= deadline:
            raise error_type("AOA per-command playback budget exceeded; inspect device before retrying")

    def control_event(event):
        emit(event["event_type"], event["index"])
        if on_control_event:
            on_control_event(event)

    try:
        if control_id:
            register(control_id)
        for index, (text, command) in enumerate(zip(commands, parsed), 1):
            input_attempted = False
            check()
            if control_id:
                wait_if_paused(control_id, index=index, total=total, sleep=sleep,
                               on_event=control_event, on_tick=check, read_source=control_source)
            check()
            emit("running", index, text)
            if command.kind == "sleep":
                # The previous step has already removed its HID devices and
                # restored USB. Let Settings settle with those devices absent.
                sleep(command.sleep_ms / 1000)
                check()
            else:
                input_attempted = True
                result = run_one(text, deadline, lambda event, index=index, text=text: (
                    emit("reconnecting", index, text) if event.get("phase") == "reconnecting" else None))
                if (result.get("success") is not True or result.get("commands_executed") != 1
                        or result.get("native_mode_restored") is not True or result.get("cleanup_errors")):
                    error = error_type(f"AOA step {index}/{total} did not restore clean USB state; reconcile required")
                    error.cleanup_errors = result.get("cleanup_errors") or []
                    raise error
                reconnects += int(result.get("usb_reconnections") or 0)
                check()
                last_input = (index, text)
            completed += 1
            emit("completed", index, text)
            if on_progress:
                try:
                    on_progress(index, total, text)
                except Exception:
                    logger.warning("AOA per-command callback failed", exc_info=True)
    except Exception as exc:
        emit("cancelled" if getattr(exc, "cancelled", False) else "failed", index)
        inner = getattr(exc, "aoa_result", {})
        details = record_failure(exc, index=index, total=total,
            command=commands[index - 1] if index else "", completed=completed,
            reports_sent=inner.get("failed_reports_sent", 0),
            reports_attempted=(inner.get("failed_input_state") != "not_sent"
                               if inner else input_attempted), last_input=last_input)
        if getattr(exc, "cancelled", False):
            raise
        error = error_type(f"AOA per-command step {index}/{total}: {exc}")
        error.aoa_result = details
        if getattr(exc, "cleanup_errors", None):
            error.cleanup_errors = exc.cleanup_errors
        raise error from exc
    finally:
        if control_id:
            unregister(control_id)
    result = {"serial": serial, "success": True, "commands_total": total,
              "commands_executed": completed, "native_mode_restored": True,
              "cleanup_errors": [], "reconcile_required": False, "usb_reconnections": reconnects,
              "hid_touch_mode": touch_mode, "hid_settle_seconds": hid_settle_seconds,
              "hid_session_mode": "per_command"}
    if wait_adb_seconds:
        result.update(wait_adb(serial, wait_adb_seconds))
        result["success"] = result.get("adb_recovered") is True
    return result
