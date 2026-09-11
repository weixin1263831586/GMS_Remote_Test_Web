#!/usr/bin/env python3
"""Offline, reversible migration of deployment configuration. Never prints values.

Run without --apply to inspect file movements. The Controller must be stopped
before --apply. Originals are retained in a private recovery directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from foundation.config_paths import runtime_data_root, runtime_environment_path, secret_environment_path
from foundation.private_config import read_json_object, write_private_json
from foundation.runtime_config_store import DEPLOYMENT_KEYS, RuntimeConfigStore, is_secret_field, partition_runtime
from scripts.gms_backup import _offline_controller_lock
from scripts.sanitize_tracked_config import KNOWN_SECRET_ENV, _provider_env_name


SIMPLE_MOVES = {
    "configs/cluster.json": "configs/local/cluster.json",
    "configs/build_servers.json": "configs/local/build_servers.json",
    "configs/automation_profiles.json": "configs/local/automation_profiles.json",
    "configs/worker_tokens.json": "configs/secrets/worker_tokens.json",
    "configs/user_tools_data.json": "data/settings/user_tools.json",
    # The old global map has no authenticated owner. Archive it without assigning
    # other people's identity data to an arbitrary user.
    "configs/redmine_user_map.json": "data/redmine/legacy/redmine_user_map.json",
}
PLACEHOLDER = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::[^}]*)?\}$")


@contextmanager
def _deployment_environment(root: Path):
    original = dict(os.environ)
    values = read_json_object(runtime_environment_path(root))
    values.update(read_json_object(secret_environment_path(root)))
    try:
        for key, value in values.items():
            if isinstance(value, str) and not key.startswith("_"):
                os.environ.setdefault(key, value.replace("${PROJECT_ROOT}", str(root)))
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def _effective_configuration(root: Path) -> dict:
    from foundation.config import ConfigManager

    with _deployment_environment(root):
        return ConfigManager(project_root=root).load_config(force_reload=True)


def _get(payload: dict, keys: tuple[str, ...]):
    value = payload
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _set(payload: dict, keys: tuple[str, ...], value) -> None:
    target = payload
    for key in keys[:-1]:
        target = target.setdefault(key, {})
    target[keys[-1]] = value


def _externalize_secrets(config: dict, environment: dict) -> None:
    mappings = dict(KNOWN_SECRET_ENV)
    for name, provider in (config.get("ai_models", {}).get("providers", {}) or {}).items():
        if isinstance(provider, dict) and "api_key" in provider:
            mappings[("ai_models", "providers", name, "api_key")] = _provider_env_name(name)
    mappings[("external_services", "gms_assistant_api_key")] = "GMS_ASSISTANT_API_KEY"
    for keys, variable in mappings.items():
        value = _get(config, keys)
        if not isinstance(value, str) or not value.strip() or PLACEHOLDER.fullmatch(value):
            continue
        # Preserve both differing values: the static literal used to win for this
        # consumer. Do not silently replace an environment key used elsewhere.
        if environment.get(variable) not in (None, "", value):
            variable = f"{variable}_STATIC_CONFIG"
            if environment.get(variable) not in (None, "", value):
                raise ValueError(f"Conflicting secret reference: {variable}")
        environment[variable] = value
        _set(config, keys, f"${{{variable}}}")


def plan_migration(root: Path) -> tuple[dict[Path, dict], list[Path]]:
    if runtime_data_root(root) != root / "data":
        raise ValueError("Offline migration requires the deployment data root inside the selected project")
    marker = root / "configs/local/layout.json"
    if marker.exists():
        # A repeated run must not import stale files left by old tooling.
        leftovers = [root / name for name in (*SIMPLE_MOVES, "configs/runtime.json", "configs/config_runtime.json") if (root / name).exists()]
        compatibility = root / "configs/config.json"
        if compatibility.exists() and not (
            compatibility.is_symlink() and compatibility.resolve() == root / "configs/local/deployment.json"
        ):
            leftovers.append(compatibility)
        if leftovers:
            raise ValueError("Legacy configuration was recreated after migration; reconcile it before retrying")
        return {}, []
    writes, originals = {}, []
    for source, destination in SIMPLE_MOVES.items():
        source_path = root / source
        if source_path.exists():
            writes[root / destination] = read_json_object(source_path)
            originals.append(source_path)
    static_path = root / "configs/config.json"
    environment_path = root / "configs/runtime.json"
    runtime_path = root / "configs/config_runtime.json"
    config = read_json_object(static_path)
    environment = read_json_object(environment_path)
    runtime = read_json_object(runtime_path)
    for path in (static_path, environment_path, runtime_path):
        if path.exists():
            originals.append(path)
    _externalize_secrets(config, environment)
    # Keep the previous top-level override semantics, and move host identity out
    # of the product configuration. AI defaults deliberately remain static.
    for key in set(config) & set(runtime) - {"ai_models"}:
        config.pop(key)
    for key in DEPLOYMENT_KEYS:
        if key in config:
            runtime[key] = config.pop(key)
    writes[root / "configs/local/config.json"] = config
    writes[root / "configs/local/environment.json"] = {key: value for key, value in environment.items() if not is_secret_field(key)}
    writes[root / "configs/secrets/environment.json"] = {key: value for key, value in environment.items() if is_secret_field(key)}
    # Static secret variants end in _STATIC_CONFIG and must remain private too.
    for key in list(writes[root / "configs/local/environment.json"]):
        if key.endswith("_STATIC_CONFIG"):
            writes[root / "configs/secrets/environment.json"][key] = writes[root / "configs/local/environment.json"].pop(key)
    store = RuntimeConfigStore(root)
    for name, payload in partition_runtime(runtime).items():
        writes[store.paths[name]] = payload
    writes[marker] = {"version": 2}
    for destination in writes:
        if destination.exists():
            raise ValueError(f"Migration target already exists: {destination.relative_to(root)}")
    return writes, originals


def migrate(root: Path, *, apply: bool = False) -> dict:
    root = root.resolve()
    if not apply:
        writes, originals = plan_migration(root)
        return {"targets": [str(path.relative_to(root)) for path in writes], "original_files": len(originals), "applied": False}
    with _offline_controller_lock(root / "data"):
        writes, originals = plan_migration(root)
        if not writes:
            return {"applied": False, "already_migrated": True}
        before = _effective_configuration(root)
        certs = root / "configs/certs"
        target_certs = root / "configs/secrets/certs"
        if certs.exists() and target_certs.exists():
            raise ValueError("Certificate migration target already exists")
        for path in originals:
            if path.is_symlink():
                raise ValueError(f"Refusing a linked source: {path.relative_to(root)}")
        backup_root = root / "data/config-migration-backups"
        backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = Path(tempfile.mkdtemp(prefix="layout-v2-", dir=backup_root))
        for path in originals:
            target = backup / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(path, target)
            target.chmod(0o600)
        write_private_json(backup / "manifest.json", {
            "originals": [str(path.relative_to(root)) for path in originals],
            "targets": [str(path.relative_to(root)) for path in writes],
            "certificates_moved": certs.exists(),
        })
        created = []
        certs_moved = False
        try:
            for destination, payload in writes.items():
                write_private_json(destination, payload)
                created.append(destination)
                if read_json_object(destination) != payload:
                    raise ValueError("Configuration verification failed")
            if certs.exists():
                certs.rename(target_certs)
                target_certs.chmod(0o700)
                certs_moved = True
                # Existing systemd/Worker TLS arguments may still reference this
                # directory. Keep a relative compatibility link, without copies.
                certs.symlink_to("secrets/certs", target_is_directory=True)
            for path in originals:
                path.unlink()
            # Older installed agent CLIs read this path only for host/user/port.
            # Keep discovery working without duplicating static config or secrets.
            (root / "configs/config.json").symlink_to("local/deployment.json")
            if _effective_configuration(root) != before:
                raise ValueError("Effective configuration changed; migration rolled back")
        except Exception:
            compatibility = root / "configs/config.json"
            if compatibility.is_symlink():
                compatibility.unlink()
            for destination in reversed(created):
                destination.unlink(missing_ok=True)
            if certs_moved:
                certs.unlink(missing_ok=True)
                target_certs.rename(certs)
            for path in originals:
                source = backup / path.relative_to(root)
                if not path.exists():
                    shutil.copyfile(source, path)
                    path.chmod(0o600)
            raise
        # The manifest records paths only. Original values remain exclusively in
        # private recovery files and never enter CLI output.
        return {"applied": True, "files_written": len(created), "backup": str(backup)}


def rollback(root: Path, backup: Path) -> dict:
    """Restore originals offline, retaining the replaced layout for recovery."""
    root = root.resolve()
    backup = backup.resolve()
    backup_root = root / "data/config-migration-backups"
    if backup.parent != backup_root or backup.is_symlink():
        raise ValueError("Rollback requires a local configuration migration backup")
    manifest = read_json_object(backup / "manifest.json")
    allowed_originals = {*SIMPLE_MOVES, "configs/config.json", "configs/runtime.json", "configs/config_runtime.json"}
    allowed_targets = {
        *SIMPLE_MOVES.values(), "configs/local/config.json", "configs/local/environment.json",
        "configs/secrets/environment.json", "configs/local/layout.json",
        *(str(path.relative_to(root)) for path in RuntimeConfigStore(root).paths.values()),
    }
    originals, targets = manifest.get("originals"), manifest.get("targets")
    if not isinstance(originals, list) or not isinstance(targets, list) or not set(originals) <= allowed_originals or not set(targets) <= allowed_targets:
        raise ValueError("Invalid migration recovery manifest")
    if not originals:
        raise ValueError("Recovery manifest contains no original configuration")
    for name in originals:
        source = backup / name
        destination = root / name
        is_compatibility = name == "configs/config.json" and destination.is_symlink() and destination.resolve() == root / "configs/local/deployment.json"
        if source.is_symlink() or not source.is_file() or (destination.exists() and not is_compatibility):
            raise ValueError("Recovery source is missing or original target already exists")
    with _offline_controller_lock(root / "data"):
        retained = Path(tempfile.mkdtemp(prefix="layout-v2-replaced-", dir=backup_root))
        # Retain the complete new layout before restoring any original.
        for name in targets:
            path = root / name
            if path.is_file():
                target = retained / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(path, target)
                target.chmod(0o600)
        for name in originals:
            destination = root / name
            if destination.is_symlink():
                destination.unlink()
            shutil.copyfile(backup / name, destination)
            destination.chmod(0o600)
        for name in targets:
            (root / name).unlink(missing_ok=True)
        if manifest.get("certificates_moved"):
            certs = root / "configs/certs"
            if certs.is_symlink():
                certs.unlink()
            (root / "configs/secrets/certs").rename(certs)
        return {"rolled_back": True, "replaced_layout_backup": str(retained)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", type=Path, metavar="BACKUP_DIRECTORY")
    args = parser.parse_args()
    try:
        if args.rollback:
            if not args.apply:
                parser.error("--rollback requires --apply")
            result = rollback(args.project_root, args.rollback)
        else:
            result = migrate(args.project_root, apply=args.apply)
    except Exception as exc:
        # Exception payloads from JSON/OS operations must not expose config data.
        print(json.dumps({"ok": False, "error_type": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
