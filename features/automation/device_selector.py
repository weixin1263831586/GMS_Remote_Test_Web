"""Automatic device selection for automation runs.

Profiles may carry a ``device_selector`` block (``min_count``, ``exclusive``,
optional ``serial_prefix``/``board`` filters). The orchestrator's
``select_devices`` stage delegates here so a run without manually chosen
devices does not deadlock at ``waiting_device``.

Lock strategy (Phase 1, agreed): the selector only *picks* idle devices — it
does not pre-lock them. Locking is left to the burn API, which avoids
client-id contention between this selector and the loopback flash request.
At worker concurrency 1 this is race-free enough; tracked as a known
limitation for a later phase.
"""

from __future__ import annotations

import json
from typing import Any


class DeviceSelector:
    def __init__(self, device_manager: Any, lock_manager: Any):
        self.device_manager = device_manager
        self.lock_manager = lock_manager

    def _run_device_selector(self, run: dict[str, Any]) -> dict[str, Any]:
        plan = json.loads(run.get("test_plan_json") or "{}")
        selector = plan.get("device_selector") if isinstance(plan.get("device_selector"), dict) else {}
        return selector

    def _busy_serials(self) -> set[str]:
        """Serials currently locked by anyone."""
        try:
            return {str(device_id) for device_id in self.lock_manager.get_all_locks()}
        except Exception:
            return set()

    def _matches_filters(self, serial: str, selector: dict[str, Any]) -> bool:
        prefix = str(selector.get("serial_prefix") or "").strip()
        if prefix and not serial.startswith(prefix):
            return False
        board = str(selector.get("board") or "").strip()
        if board:
            try:
                info = self.device_manager.get_device_info(serial) or {}
            except Exception:
                return False
            product_board = str(info.get("board") or info.get("ro.product.board") or "")
            if board.lower() not in product_board.lower():
                return False
        return True

    def select(self, run: dict[str, Any]) -> dict[str, Any]:
        # Manual override: devices already attached to the run win.
        existing = json.loads(run.get("devices_json") or "[]")
        serials = []
        for item in existing:
            if isinstance(item, dict) and item.get("serial"):
                serials.append(str(item["serial"]))
            elif isinstance(item, str):
                serials.append(item)
        if serials:
            return {"success": True, "devices": [{"serial": s} for s in serials]}

        selector = self._run_device_selector(run)
        min_count = max(1, int(selector.get("min_count") or 1))

        try:
            connected = [str(s) for s in self.device_manager.get_connected_devices()]
        except Exception:
            connected = []

        busy = self._busy_serials()
        candidates = [
            serial
            for serial in connected
            if serial not in busy and self._matches_filters(serial, selector)
        ]

        if len(candidates) < min_count:
            # retry:True keeps the run in waiting_device for the next tick.
            return {
                "success": False,
                "error": (
                    f"not enough idle devices: have {len(candidates)}, "
                    f"need {min_count} (connected={len(connected)}, busy={len(busy)})"
                ),
                "retry": True,
            }

        chosen = candidates[:min_count]
        return {"success": True, "devices": [{"serial": serial} for serial in chosen]}
