"""Resolve account-scoped AOA profiles once, before a run is persisted."""
from __future__ import annotations

import hashlib
import json

from foundation import aoa_profile
from foundation.aoa_hid_mode import profile_hid_options
from foundation.aoa_recovery_policy import firmware_compatibility


def commands_digest(commands: list[str]) -> str:
    return hashlib.sha256(json.dumps(commands, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def freeze_recovery(plan: dict, owner_id: str) -> None:
    recovery = plan.get("aoa")
    if recovery is None:
        return
    if not isinstance(recovery, dict):
        raise ValueError("AOA recovery configuration must be an object")
    if recovery.get("mode") in (None, "", "off"):
        return
    if recovery.get("mode") != "user_build_recovery":
        raise ValueError("unsupported AOA recovery mode")
    commands = recovery.get("commands") or []
    if not isinstance(commands, list) or any(not isinstance(item, str) for item in commands):
        raise ValueError("AOA recovery commands must be an array of strings")
    profile = {}
    if not commands and recovery.get("profile"):
        from features.devices import profile_store

        profile = next((p for p in profile_store().list_profiles(owner_id, include_all=False)
                        if p["name"] == recovery["profile"] or p["id"] == recovery["profile"]), {})
        if not profile:
            raise ValueError("AOA profile not found in the creating account")
        commands = profile["commands"]
    commands = [str(item).strip() for item in commands if str(item).strip()]
    aoa_profile.parse_profile_commands(commands)
    profile_hid_options(profile.get("metadata", recovery.get("metadata") or {}))
    firmware_compatibility(profile.get("metadata", recovery.get("metadata") or {}), commands)
    raw_wait = recovery.get("wait_adb_seconds", profile.get("wait_adb_seconds", 60))
    if isinstance(raw_wait, bool) or not isinstance(raw_wait, (str, int)):
        raise ValueError("AOA recovery wait_adb_seconds must be an integer")
    wait = int(raw_wait)
    if not 1 <= wait <= 600:
        raise ValueError("AOA recovery wait_adb_seconds must be 1..600")
    recovery.update({
        "commands": commands, "commands_sha256": commands_digest(commands),
        "wait_adb_seconds": wait,
        "profile_id": profile.get("id", ""),
        "profile_revision": profile.get("revision", ""),
        "metadata": profile.get("metadata", recovery.get("metadata") or {}),
    })
