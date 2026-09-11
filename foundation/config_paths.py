"""Canonical paths for the structured configuration directory."""

from __future__ import annotations

from pathlib import Path

from foundation.runtime_settings import RuntimeSettings


def _prefer_existing(canonical: Path, legacy: Path) -> Path:
    return legacy if not canonical.exists() and legacy.exists() else canonical


def config_root(project_root: Path | str) -> Path:
    return Path(project_root).resolve() / "configs"


def runtime_data_root(project_root: Path | str) -> Path:
    return RuntimeSettings.from_environment(project_root=Path(project_root)).data_root


def static_config_path(project_root: Path | str) -> Path:
    root = config_root(project_root)
    return _prefer_existing(root / "local/config.json", root / "config.json")


def example_config_path(project_root: Path | str, name: str = "config.json") -> Path:
    """Prefer grouped templates, with read compatibility for older deployments."""
    root = config_root(project_root)
    example_name = Path(name).with_suffix(".example.json").name
    canonical = root / "examples" / example_name
    legacy = root / example_name
    if not canonical.is_file() and legacy.is_file():
        return legacy
    return canonical


def runtime_config_path(project_root: Path | str) -> Path:
    root = Path(project_root).resolve()
    return _prefer_existing(runtime_data_root(root) / "settings/preferences.json", root / "configs/config_runtime.json")


def runtime_environment_path(project_root: Path | str) -> Path:
    root = config_root(project_root)
    return _prefer_existing(root / "local/environment.json", root / "runtime.json")


def secret_environment_path(project_root: Path | str) -> Path:
    return config_root(project_root) / "secrets/environment.json"


def cluster_config_path(project_root: Path | str) -> Path:
    root = config_root(project_root)
    return _prefer_existing(root / "local/cluster.json", root / "cluster.json")


def worker_tokens_path(project_root: Path | str) -> Path:
    root = config_root(project_root)
    return _prefer_existing(root / "secrets/worker_tokens.json", root / "worker_tokens.json")


def certificates_path(project_root: Path | str) -> Path:
    root = config_root(project_root)
    return _prefer_existing(root / "secrets/certs", root / "certs")


def user_tools_path(project_root: Path | str) -> Path:
    root = Path(project_root).resolve()
    return _prefer_existing(runtime_data_root(root) / "settings/user_tools.json", root / "configs/user_tools_data.json")


def automation_profiles_path(project_root: Path | str) -> Path:
    root = config_root(project_root)
    return _prefer_existing(root / "local/automation_profiles.json", root / "automation_profiles.json")


def build_servers_path(project_root: Path | str) -> Path:
    root = config_root(project_root)
    return _prefer_existing(root / "local/build_servers.json", root / "build_servers.json")


def default_suites_path(config: dict[str, str], ubuntu_user: str) -> str:
    """Resolve the GMS suite root: an explicit ``suites_path`` wins, else
    the suite is assumed under the configured Ubuntu user's home."""
    return config.get("suites_path", f"/home/{ubuntu_user}/GMS-Suite")
