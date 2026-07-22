"""Environment-derived immutable process settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(value: str, default: int) -> int:
    try:
        return int(value.strip())
    except (AttributeError, TypeError, ValueError):
        return default


@dataclass(frozen=True)
class RuntimeSettings:
    project_root: Path
    data_root: Path
    server_host: str
    server_port: int
    environment: str
    proxy_headers_enabled: bool
    forwarded_allow_ips: str

    @classmethod
    def from_environment(
        cls,
        *,
        project_root: Path | None = None,
        environ: dict[str, str] | None = None,
    ) -> RuntimeSettings:
        env = os.environ if environ is None else environ
        root = (
            Path(project_root).resolve()
            if project_root
            else Path(__file__).resolve().parents[1]
        )
        environment = env.get("GMS_ENV", "development").strip().lower()
        return cls(
            project_root=root,
            data_root=Path(
                env.get("GMS_DATA_ROOT", str(root / "data"))
            ).resolve(),
            server_host=env.get(
                "GMS_SERVER_HOST",
                "127.0.0.1" if environment == "production" else "0.0.0.0",
            ),
            server_port=_env_int(env.get("GMS_PORT", "5001"), 5001),
            environment=environment,
            proxy_headers_enabled=_env_bool(
                env.get(
                    "GMS_PROXY_HEADERS",
                    "true" if environment == "production" else "false",
                )
            ),
            forwarded_allow_ips=env.get(
                "GMS_FORWARDED_ALLOW_IPS", "127.0.0.1"
            ),
        )
