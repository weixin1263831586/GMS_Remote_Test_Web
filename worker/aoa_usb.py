"""Physical USB topology binding for one lease-protected AOA operation."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class UsbBinding:
    port: str = ""
    validate: Callable[[], None] | None = None
    cleanup_errors: list[dict] = field(default_factory=list)


_binding: ContextVar[UsbBinding | None] = ContextVar("aoa_usb_binding", default=None)


def port_path(device) -> str:
    bus = getattr(device, "bus", None)
    ports = getattr(device, "port_numbers", None)
    return f"{bus}-" + ".".join(map(str, ports)) if bus is not None and ports else ""


@contextmanager
def bind_usb(port: str = "", validate=None):
    token = _binding.set(UsbBinding(port, validate))
    try:
        yield
    finally:
        _binding.reset(token)


def bound_port() -> str:
    binding = _binding.get()
    return binding.port if binding else ""


def cleanup_diagnostics() -> list[dict]:
    binding = _binding.get()
    return binding.cleanup_errors if binding else []


def validate_binding() -> None:
    binding = _binding.get()
    if binding and binding.validate:
        binding.validate()


def find_bound_device(usb_core, serial: str, serial_finder):
    binding = _binding.get()
    if binding is None:
        return serial_finder()
    validate_binding()
    if not binding.port:
        device = serial_finder()
        if device is None:
            return None
        binding.port = port_path(device)
        if not binding.port:
            raise RuntimeError("USB physical port is unavailable; refusing AOA input")
        return device
    candidates = list(usb_core.find(find_all=True, custom_match=lambda d: port_path(d) == binding.port))
    # Test doubles and older backends may ignore custom_match.
    candidates = [d for d in candidates if port_path(d) == binding.port]
    if len(candidates) > 1:
        raise RuntimeError("ambiguous USB physical port; refusing AOA input")
    if not candidates:
        return None
    device = candidates[0]
    accessory = device.idVendor == 0x18D1 and 0x2D00 <= device.idProduct <= 0x2D05
    if not accessory and device.serial_number != serial:
        raise RuntimeError("USB identity changed on the reserved port; refusing AOA input")
    return device


def cleanup_steps(steps, errors: list[dict]) -> None:
    """Keep every cleanup independent and retain diagnostics."""
    for name, cleanup in steps:
        try:
            result = cleanup()
            if result is False or (isinstance(result, int) and result < 0):
                raise RuntimeError(f"{name} did not complete")
        except Exception as exc:
            errors.append({"step": name, "error": str(exc)})
