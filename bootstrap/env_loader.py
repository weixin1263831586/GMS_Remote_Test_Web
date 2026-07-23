"""在项目模块导入前将 JSON 运行配置写入环境变量。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from foundation.config_paths import runtime_environment_path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


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
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            if key.startswith("_") or not isinstance(value, str):
                continue
            if key not in os.environ:
                os.environ[key] = value
                applied[key] = value
        break
    return applied
