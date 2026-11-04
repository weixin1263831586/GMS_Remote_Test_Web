"""Refresh an AOA USB session between complete input commands, never mid-gesture."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from foundation import aoa_profile
from foundation.aoa_hid_mode import hid_registrations

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
        self.dispose(old)
        self.device = self.configure(timeout_seconds=max(1, int(timeout - self.clock())))
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
