"""Automation profile loading and normalization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


def normalize_profile(raw: Dict[str, Any]) -> Dict[str, Any]:
    profile_id = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or profile_id).strip()
    return {
        "id": profile_id,
        "name": name,
        "enabled": bool(raw.get("enabled", True)),
        "gerrit": raw.get("gerrit") if isinstance(raw.get("gerrit"), dict) else {},
        "jenkins": raw.get("jenkins") if isinstance(raw.get("jenkins"), dict) else {},
        "device_selector": raw.get("device_selector") if isinstance(raw.get("device_selector"), dict) else {},
        "flash": raw.get("flash") if isinstance(raw.get("flash"), dict) else {},
        "test_plan": raw.get("test_plan") if isinstance(raw.get("test_plan"), dict) else {},
        "reporting": raw.get("reporting") if isinstance(raw.get("reporting"), dict) else {},
    }


def load_profiles(path: str | Path, enabled_only: bool = False) -> List[Dict[str, Any]]:
    profile_path = Path(path)
    if not profile_path.exists():
        return []
    data = json.loads(profile_path.read_text(encoding="utf-8") or "{}")
    raw_profiles = data.get("profiles") if isinstance(data, dict) else []
    profiles = []
    seen = set()
    for item in raw_profiles or []:
        if not isinstance(item, dict):
            continue
        profile = normalize_profile(item)
        if not profile["id"] or profile["id"] in seen:
            continue
        if enabled_only and not profile["enabled"]:
            continue
        seen.add(profile["id"])
        profiles.append(profile)
    return profiles


def save_profiles(path: str | Path, profiles: List[Dict[str, Any]]) -> Dict[str, Any]:
    profile_path = Path(path)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = []
    seen = set()
    for item in profiles or []:
        if not isinstance(item, dict):
            continue
        profile = normalize_profile(item)
        if not profile["id"] or profile["id"] in seen:
            continue
        seen.add(profile["id"])
        normalized.append(profile)
    data = {"profiles": normalized}
    profile_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def upsert_profile(path: str | Path, raw_profile: Dict[str, Any]) -> Dict[str, Any]:
    profile = normalize_profile(raw_profile or {})
    if not profile["id"]:
        raise ValueError("profile id is required")
    profiles = load_profiles(path)
    replaced = False
    next_profiles = []
    for item in profiles:
        if item["id"] == profile["id"]:
            next_profiles.append(profile)
            replaced = True
        else:
            next_profiles.append(item)
    if not replaced:
        next_profiles.append(profile)
    save_profiles(path, next_profiles)
    return profile
