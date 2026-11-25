"""AOA USB/HID sessions, debug steps and per-command replay lifecycle."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from foundation import aoa_profile
from foundation.aoa_hid_mode import hid_registrations
from foundation.aoa_profile import MAX_AOA_OPERATION_SECONDS

from .aoa_control import register, unregister, wait_if_paused
from .aoa_diagnostics import record_failure
from .aoa_usb import bound_port, validate_binding


logger = logging.getLogger(__name__)


def register_hids(device, *, transfer, serial: str, sleep, error_type,
                  required_channels: frozenset[int] | None = None,
                  touch_mode: str = "pointer", settle_seconds: float = 2) -> set[int]:
    """Retry HID setup before input; reject missing devices without resending input."""
    channels: set[int] = set()
    for hid_id, descriptor in hid_registrations(touch_mode):
        if required_channels is not None and hid_id not in required_channels:
            continue
        for attempt in range(3):
            try:
                result = transfer(device, aoa_profile.AOA_ACCESSORY_REGISTER_HID,
                                  hid_id, len(descriptor))
                break
            except error_type as exc:
                logger.warning("AOA HID registration %s attempt %s on %s: %s",
                               hid_id, attempt + 1, serial, exc)
                if attempt < 2:
                    sleep(1.0)
        else:
            continue
        if result == -4:
            raise error_type(f"device disappeared while registering HID {hid_id}")
        try:
            result = transfer(device, aoa_profile.AOA_ACCESSORY_SET_HID_REPORT_DESC,
                              hid_id, 0, descriptor)
        except error_type as exc:
            logger.warning("AOA HID descriptor for channel %s rejected by %s: %s", hid_id, serial, exc)
            continue
        if result == -4:
            raise error_type(f"device disappeared while loading HID {hid_id} descriptor")
        channels.add(hid_id)
    if channels and settle_seconds:
        sleep(settle_seconds)
    return channels


def usb_generation(device) -> tuple[int, ...] | None:
    values = tuple(getattr(device, key, None) for key in ('bus', 'address', 'idVendor', 'idProduct'))
    return values if all(isinstance(value, int) for value in values) else None


def device_gone(error: Exception) -> bool:
    return getattr(error, 'backend_error_code', None) == -4 or getattr(error, 'errno', None) == 19


@dataclass
class AoaSession:
    device: Any
    registered: set[int]
    find: Callable
    configure: Callable
    detach: Callable
    register: Callable
    dispose: Callable
    sleep: Callable
    error_type: type[Exception]
    clock: Callable = time.monotonic
    rebuilds: int = 0
    # Last handle that was successfully obtained (initial build or rebuild).
    # ``device`` is nulled while a rebuild is in flight; cleanup must fall
    # back to ``last_device`` so a rebuild failure can still run the USB
    # level steps (reset/dispose/restore) instead of skipping them entirely.
    last_device: Any = None

    def __post_init__(self) -> None:
        if self.device is not None and self.last_device is None:
            self.last_device = self.device

    def _find(self):
        try:
            return self.find()
        except Exception as exc:
            if device_gone(exc):
                return None
            raise

    def _alive(self) -> bool:
        validate_binding()
        try:
            # Standard, read-only GET_DESCRIPTOR(Device). It detects an invalid
            # old handle even when Linux has reused the same USB address.
            descriptor = self.device.ctrl_transfer(0x80, 6, 0x0100, 0, 18, timeout=1000)
        except Exception as exc:
            if device_gone(exc):
                return False
            raise self.error_type(f'USB preflight read failed before input: {exc}') from exc
        if len(descriptor) != 18:
            raise self.error_type('USB preflight returned an incomplete device descriptor')
        return True

    def prepare(self, deadline: float, on_reconnecting: Callable) -> bool:
        original = usb_generation(self.device)
        # Backends without generation metadata cannot safely rebuild a session;
        # their existing send path still fails rather than retrying an input.
        if original is None:
            return False
        validate_binding()
        current = self._find()
        if current is not None and usb_generation(current) == original and self._alive():
            return False
        if not bound_port():
            raise self.error_type('USB connection changed without a bound physical port; reconcile required')
        on_reconnecting()
        timeout = min(deadline, self.clock() + 20.0)
        logger.warning('AOA connection changed before input: port=%s old=%s new=%s',
                       bound_port(), original, usb_generation(current))
        while current is None:
            validate_binding()
            if self.clock() >= timeout:
                raise self.error_type('USB reconnect timed out before input; reconcile required')
            self.sleep(min(0.25, timeout - self.clock()))
            current = self._find()
        validate_binding()
        if self.clock() >= timeout:
            raise self.error_type('USB reconnect budget exceeded before input; reconcile required')
        # The old handle must not receive cleanup writes after re-enumeration.
        old = self.device
        self.device = None
        self.registered = set()
        try:
            self.dispose(old)
        except Exception as exc:
            # Best-effort: a failed stale-handle dispose must not abort the
            # rebuild — the action layer keeps ``last_device`` (the old
            # handle) for its USB-level cleanup steps.
            logger.warning('AOA old-handle dispose failed during rebuild: %s', exc)
        self.device = self.configure(timeout_seconds=max(1, int(timeout - self.clock())))
        self.last_device = self.device
        validate_binding()
        self.detach(self.device)
        self.sleep(1.0)
        self.registered = self.register(self.device)
        self.sleep(1.0)
        self.rebuilds += 1
        if not self._alive():
            return self.prepare(timeout, on_reconnecting)
        logger.info('AOA connection rebuilt before input: port=%s new=%s',
                    bound_port(), usb_generation(self.device))
        return True


def debug_steps(source, frozen, *, tick, sleep, clock, error_type, idle_seconds=120, transform=None):
    channels = aoa_profile.required_hid_channels(frozen)
    last_input = clock()
    while True:
        tick()
        closing = clock() - last_input >= idle_seconds
        entry = source(close=closing)
        if entry is None:
            if closing:
                return
            sleep(0.2)
            continue
        text = transform(entry["command"]) if transform else entry["command"]
        parsed = aoa_profile.parse_profile_commands([text])
        if not aoa_profile.required_hid_channels(parsed).issubset(channels):
            raise error_type("debug input requires another HID channel; restart the session")
        yield entry["sequence"], parsed[0], entry["command"]
        last_input = clock()


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
    # 走到这里说明全部命令已执行；回放完成证据独立于 ADB 终验（见
    # firmware_recovery.recover_after_flash 的 REPLAY_DONE_ADB_UNAVAILABLE）。
    result["replay_completed"] = True
    if wait_adb_seconds:
        result.update(wait_adb(serial, wait_adb_seconds))
        result["success"] = result.get("adb_recovered") is True
    return result
