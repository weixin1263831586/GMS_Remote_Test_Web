"""Automation profile loading and normalization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from foundation.private_config import write_private_json


SECRET_MASK = "***"
_SECRET_KEYS = frozenset(
    {"api_token", "token", "password", "http_password", "secret", "server_password"}
)


def _is_secret_key(key: str) -> bool:
    lowered = str(key).lower()
    return lowered in _SECRET_KEYS or lowered.endswith(("_password", "_token", "_secret", "_private_key"))


def _mask_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        masked = {}
        for key, item in value.items():
            if _is_secret_key(key) and item:
                masked[key] = SECRET_MASK
            else:
                masked[key] = _mask_secrets(item)
        return masked
    if isinstance(value, list):
        return [_mask_secrets(item) for item in value]
    return value


def mask_profile_secrets(profile: dict[str, Any]) -> dict[str, Any]:
    """遮蔽 profile 中递归出现的凭据字段，供 API 读回表面使用。

    真实值只保留在私有存储与执行路径（orchestrator/executors 直读
    load_profiles，不经此函数）。
    """
    return _mask_secrets(profile or {})


def _restore_masked_secrets(incoming: Any, stored: Any) -> Any:
    """把「未修改」的遮蔽哨兵还原为已存真实值。

    管理端 UI 先读遮蔽列表再整体保存；若把 "***" 原样写回会覆盖真实
    凭据，因此保存前用已存 profile 还原哨兵字段。
    """
    if isinstance(incoming, dict):
        stored_map = stored if isinstance(stored, dict) else {}
        merged = {}
        for key, item in incoming.items():
            if (
                _is_secret_key(key)
                and item == SECRET_MASK
                and key in stored_map
            ):
                merged[key] = stored_map[key]
            else:
                merged[key] = _restore_masked_secrets(item, stored_map.get(key))
        return merged
    if isinstance(incoming, list):
        stored_list = stored if isinstance(stored, list) else []
        return [
            _restore_masked_secrets(
                item, stored_list[idx] if idx < len(stored_list) else None
            )
            for idx, item in enumerate(incoming)
        ]
    return incoming


def normalize_profile(raw: dict[str, Any]) -> dict[str, Any]:
    profile_id = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or profile_id).strip()
    return {
        "id": profile_id,
        "name": name,
        "enabled": bool(raw.get("enabled", True)),
        "gerrit": raw.get("gerrit") if isinstance(raw.get("gerrit"), dict) else {},
        "build": raw.get("build") if isinstance(raw.get("build"), dict) else {},
        "jenkins": raw.get("jenkins") if isinstance(raw.get("jenkins"), dict) else {},
        "device_selector": raw.get("device_selector") if isinstance(raw.get("device_selector"), dict) else {},
        "flash": raw.get("flash") if isinstance(raw.get("flash"), dict) else {},
        "test_plan": raw.get("test_plan") if isinstance(raw.get("test_plan"), dict) else {},
        "reporting": raw.get("reporting") if isinstance(raw.get("reporting"), dict) else {},
    }


def load_profiles(path: str | Path, enabled_only: bool = False) -> list[dict[str, Any]]:
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


def save_profiles(path: str | Path, profiles: list[dict[str, Any]]) -> dict[str, Any]:
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
    write_private_json(profile_path, data)
    return data


def upsert_profile(path: str | Path, raw_profile: dict[str, Any]) -> dict[str, Any]:
    profile_id = str((raw_profile or {}).get("id") or "").strip()
    if not profile_id:
        raise ValueError("profile id is required")
    profiles = load_profiles(path)
    stored = next((item for item in profiles if item["id"] == profile_id), None)
    if stored is not None:
        raw_profile = _restore_masked_secrets(raw_profile or {}, stored)
    profile = normalize_profile(raw_profile)
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
