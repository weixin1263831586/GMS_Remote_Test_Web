"""在项目模块导入前将 JSON 运行配置写入环境变量。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from foundation.config_paths import runtime_environment_path, secret_environment_path
from foundation.private_config import read_json_object


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _expand_project_root(value: str, base_root: Path) -> str:
    """Expand ``${PROJECT_ROOT}`` to the root of the tree holding the file."""

    if "${PROJECT_ROOT}" in value:
        return value.replace("${PROJECT_ROOT}", str(base_root))
    return value


def _candidate_paths() -> list[Path]:
    root = _project_root()
    env_root = os.getenv("GMS_DATA_ROOT")
    paths = [runtime_environment_path(root)]
    if env_root:
        data_root = Path(env_root).resolve()
        paths.append(runtime_environment_path(data_root))
    return paths


def load_runtime_env() -> dict[str, str]:
    """Merge configs/runtime.json into ``os.environ``.

    Existing environment variables always win (the explicit ``systemd``
    ``Environment=`` directive or a real shell variable takes precedence over
    the JSON file), mirroring the previous EnvironmentFile semantics where
    later systemd ``Environment=`` lines override earlier ``EnvironmentFile=``
    entries. Only string values are applied; ``_comment`` and non-strings are
    skipped.
    """

    applied: dict[str, str] = {}
    # Test harnesses set this to prevent the deployment environment JSON from
    # leaking production settings into the test environment.
    if os.getenv("GMS_SKIP_RUNTIME_ENV"):
        return applied
    for path in _candidate_paths():
        base_root = path.parent.parent.parent if path.parent.name == "local" else path.parent.parent
        secret_path = secret_environment_path(base_root)
        if not path.is_file() and not secret_path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        # ${PROJECT_ROOT} 展开为包含该 runtime.json 的部署树根目录
        # （configs/ 的上一级），兼容仓库树与 GMS_DATA_ROOT 发布树。
        if secret_path.exists():
            # Secret environment entries override file defaults; explicit process
            # environment still wins below. Do not silently ignore corrupt secrets.
            payload.update(read_json_object(secret_path))
        for key, value in payload.items():
            if key.startswith("_") or not isinstance(value, str):
                continue
            expanded = _expand_project_root(value, base_root)
            if key not in os.environ:
                os.environ[key] = expanded
                applied[key] = expanded
        break
    return applied
