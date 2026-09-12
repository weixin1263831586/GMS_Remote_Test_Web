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


def sanitize_owner_id(owner_id: object) -> str:
    """owner 标识清洗为单级安全目录名（空值回退 anonymous，防路径穿越）。"""
    safe = "".join(
        ch if ch.isalnum() or ch in ("-", "_") else "_"
        for ch in str(owner_id or "").strip()
    )
    return safe or "anonymous"


def owner_config_path(project_root: Path | str, feature: str, owner_id: str) -> Path:
    """按用户隔离的「配置/凭据」路径（ADR-0007）。

    canonical 在 ``configs/secrets/<feature>/by_user/<owner>/config_runtime.json``：
    属于配置/凭证，随部署持久，删 ``data/`` 不得影响；legacy 落在
    ``<data_root>/<feature>/by_user/<owner>/config_runtime.json``（0.20 前的
    位置），迁移期 canonical 缺失时回退读取，写入优先 canonical。
    """
    owner = sanitize_owner_id(owner_id)
    canonical = config_root(project_root) / "secrets" / feature / "by_user" / owner / "config_runtime.json"
    legacy = runtime_data_root(project_root) / feature / "by_user" / owner / "config_runtime.json"
    return _prefer_existing(canonical, legacy)


def ensure_owner_config_dir(path: Path) -> Path:
    """创建 per-owner 配置目录，权限 0700（父目录缺级时一并收紧）。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    return path


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
