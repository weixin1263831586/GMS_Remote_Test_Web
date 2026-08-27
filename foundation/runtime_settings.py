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


def runtime_environment(environ: dict[str, str] | None = None) -> str:
    """Return the deployment environment name (single source of truth).

    ``GMS_ENV`` wins; every production/development decision in the
    Controller must go through this helper so validation and runtime
    middleware can never disagree (the bug where security checks ran in
    production mode while TrustedHost defaulted to ``*``).
    """
    env = os.environ if environ is None else environ
    return env.get("GMS_ENV", "development").strip().lower()


def is_production_environment(environ: dict[str, str] | None = None) -> bool:
    return runtime_environment(environ) == "production"


def allowed_origins(environ: dict[str, str] | None = None) -> list[str]:
    """Return the configured browser origins (single CORS source of truth).

    ``GMS_ALLOWED_ORIGINS`` is the only variable; production validation and
    the actual CORS middleware both consume this list so a deployment that
    passes validation can never run with a different runtime policy.
    """
    env = os.environ if environ is None else environ
    return [
        item.strip()
        for item in env.get("GMS_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    ]


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
