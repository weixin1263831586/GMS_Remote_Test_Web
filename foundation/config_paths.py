"""Canonical paths for the structured configuration directory."""

from __future__ import annotations

from pathlib import Path


def config_root(project_root: Path | str) -> Path:
    return Path(project_root).resolve() / "configs"


def runtime_config_path(project_root: Path | str) -> Path:
    return config_root(project_root) / "config_runtime.json"


def runtime_environment_path(project_root: Path | str) -> Path:
    return config_root(project_root) / "runtime.json"


def user_tools_path(project_root: Path | str) -> Path:
    return config_root(project_root) / "user_tools_data.json"


def automation_profiles_path(project_root: Path | str) -> Path:
    return config_root(project_root) / "automation_profiles.json"


def build_servers_path(project_root: Path | str) -> Path:
    return config_root(project_root) / "build_servers.json"
