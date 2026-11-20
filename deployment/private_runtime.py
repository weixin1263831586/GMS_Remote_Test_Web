"""Compiled entrypoint for a source-free Controller/local-Worker distribution.

No developer credentials are built into this program. Installation creates a
fresh identity and keeps secrets on the customer host. Never execute the
administrator setup through argv (passwords would leak via ps).
"""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import re
import secrets
import sys
from pathlib import Path


def _root() -> Path:
    value = os.getenv("GMS_PROJECT_ROOT", "").strip()
    if not value:
        raise RuntimeError("GMS_PROJECT_ROOT is required for a binary installation")
    return Path(value).resolve()


def _private_file(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value.rstrip("\n") + "\n")
    os.chmod(path, 0o600)


def _private_json(path: Path, value: dict) -> None:
    from foundation.private_config import write_private_json
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private_json(path, value)
    os.chmod(path, 0o600)


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Invalid configuration object: {path}")
    return value


def prepare(args: argparse.Namespace) -> int:
    """Create instance identity; safe to repeat without rotating established keys."""
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from scripts.sanitize_release_config import sanitize_product_config, sanitize_cluster_config

    root = _root()
    data = Path(os.getenv("GMS_DATA_ROOT", str(root / "data"))).resolve()
    secrets_dir = root / "configs/secrets"
    local = root / "configs/local"
    key_dir = data / "secrets"
    for path in (data, secrets_dir, local, key_dir, data / "local-worker"):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing = _read_json(local / "environment.json")
    secret_env = _read_json(secrets_dir / "environment.json")
    _private_file(key_dir / "master.key", Fernet.generate_key().decode())
    _private_file(key_dir / "audit_hmac.key", secrets.token_hex(32))
    _private_file(key_dir / "local-worker.token", secrets.token_urlsafe(48))
    _private_file(key_dir / "webhook.token", secrets.token_urlsafe(48))
    _private_file(key_dir / "metrics.token", secrets.token_urlsafe(48))
    skill_key = key_dir / "skill-signing-ed25519.pem"
    if not skill_key.exists():
        pem = Ed25519PrivateKey.generate().private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        _private_file(skill_key, pem.decode())
    _private_file(key_dir / "bootstrap.token", secrets.token_urlsafe(48))
    if not (local / "config.json").exists():
        template = _read_json(root / "configs/examples/config.example.json")
        _private_json(local / "config.json", sanitize_product_config(template))
    if not (local / "cluster.json").exists():
        template = _read_json(root / "configs/examples/cluster.example.json")
        _private_json(local / "cluster.json", sanitize_cluster_config(template))
    for name, default in (
        ("build_servers.json", {"servers": [], "templates": []}),
        ("automation_profiles.json", {"profiles": []}),
    ):
        if not (local / name).exists():
            _private_json(local / name, default)
    host = args.host
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host):
            raise ValueError("Invalid hostname")
    if not 1 <= args.port <= 65535:
        raise ValueError("Invalid HTTPS port")
    origin = f"https://{host}:{args.port}"
    env = {
        **existing,
        "GMS_ENV": "production",
        "GMS_AUTH_REQUIRED": "true",
        "GMS_SECURE_COOKIES": "true",
        "GMS_ALLOWED_ORIGINS": f"{origin},https://localhost:{args.port}",
        "TRUSTED_HOSTS": f"{host},localhost,127.0.0.1",
        "GMS_AUTOMATION_OWNER_ID": "service-automation",
        "GMS_SECRET_KEY_FILE": str(key_dir / "master.key"),
        "GMS_AUDIT_HMAC_KEY_FILE": str(key_dir / "audit_hmac.key"),
        "GMS_SKILL_SIGNING_KEY_FILE": str(skill_key),
        "GMS_WORKER_TOKENS_FILE": str(secrets_dir / "worker_tokens.json"),
    }
    _private_json(local / "environment.json", env)
    secret_env.update({
        "GMS_BOOTSTRAP_TOKEN": (key_dir / "bootstrap.token").read_text().strip(),
        "GMS_METRICS_TOKEN": (key_dir / "metrics.token").read_text().strip(),
        "GMS_AUTOMATION_WEBHOOK_TOKEN": (key_dir / "webhook.token").read_text().strip(),
    })
    _private_json(secrets_dir / "environment.json", secret_env)
    local_token = (key_dir / "local-worker.token").read_text().strip()
    worker_id = "ats-worker-controller"
    tokens_path = secrets_dir / "worker_tokens.json"
    tokens = dict(_read_json(tokens_path).get("worker_tokens") or {})
    tokens[worker_id] = local_token
    _private_json(tokens_path, {"worker_tokens": tokens})
    worker_path = data / "local-worker/config.json"
    if not worker_path.exists():
        _private_json(worker_path, {
            "worker_id": worker_id,
            "name": "Local Worker",
            "controller_url": f"https://127.0.0.1:{args.port}",
            "address": "127.0.0.1",
            "ssh_user": args.user,
            "controller_ca": str(root / "configs/secrets/certs/gms-local.crt"),
            "worker_token_file": str(key_dir / "local-worker.token"),
            "heartbeat_interval_seconds": 15,
            "suite_scan_interval_seconds": 300,
            "max_jobs": 2,
            "suite_roots": [str(Path(args.home) / "GMS-Suite")],
            "data_root": str(data / "local-worker/runtime"),
        })
    print("Instance secrets and local Worker configuration prepared (secrets not shown).")
    return 0


def configure(_args: argparse.Namespace) -> int:
    """One-time local TTY wizard. Existing administrators are never replaced."""
    if not sys.stdin.isatty():
        raise RuntimeError("Interactive configuration needs a terminal; use browser bootstrap otherwise")
    from bootstrap.env_loader import load_runtime_env
    load_runtime_env()
    from features.auth.service import auth_service
    from features.redmine.config import RedmineConfig
    auth_service.initialize()
    if auth_service.setup_required():
        print("Create the first platform administrator")
        username = input("Administrator username: ").strip()
        display_name = input("Display name (optional): ").strip()
        password = getpass.getpass("Administrator password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            raise ValueError("Passwords do not match")
        admin = auth_service.create_initial_admin(username, password, display_name)
        del password, confirm
        print(f"Administrator created: {admin.username}")
    else:
        print("An administrator/user already exists; account initialization skipped.")
        username = input("Existing administrator username for Redmine settings (blank to skip): ").strip()
        if not username:
            return 0
        admin = auth_service.get_enabled_user(username)
        if admin is None or admin.role != "admin":
            raise ValueError("The selected account is not an enabled administrator")
    use_redmine = input("Configure Redmine for this administrator now? [y/N]: ").strip().lower()
    if use_redmine not in ("y", "yes"):
        print("Redmine can be configured later in the user's Web settings.")
        return 0
    base_url = input("Redmine base URL (https://...): ").strip().rstrip("/")
    from urllib.parse import urlsplit
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Provide a credential-free HTTPS Redmine URL")
    manager = RedmineConfig().for_owner(admin.id)
    if not manager.save_redmine_base_url(base_url):
        raise RuntimeError("Unable to save Redmine URL")
    method = input("Authentication: [1] API Key, [2] username/password: ").strip()
    if method == "1":
        key = getpass.getpass("Redmine API Key: ")
        if not key or not manager.save_redmine_api_key(key):
            raise ValueError("Unable to save Redmine API Key")
        del key
    elif method == "2":
        user = input("Redmine username: ").strip()
        password = getpass.getpass("Redmine password: ")
        if not user or not password or not manager.save_redmine_credentials(user, password):
            raise ValueError("Unable to save Redmine account")
        del password
    else:
        raise ValueError("Unknown Redmine authentication option")
    print("Redmine credentials saved for the administrator only; test connectivity in Web settings.")
    return 0


def controller(args: argparse.Namespace) -> int:
    from bootstrap.env_loader import load_runtime_env
    load_runtime_env()
    from app import app
    import uvicorn
    from bootstrap.http_protocol import ShutdownHttpProtocol
    root = _root()
    certs = root / "configs/secrets/certs"
    uvicorn.run(
        app, host="0.0.0.0", port=args.port, http=ShutdownHttpProtocol,
        ssl_certfile=str(certs / "gms-local.crt"),
        ssl_keyfile=str(certs / "gms-local.key"),
        timeout_keep_alive=120, limit_concurrency=512, access_log=True,
    )
    return 0


def worker(_args: argparse.Namespace) -> int:
    from bootstrap.env_loader import load_runtime_env
    load_runtime_env()
    from worker_agent.app import main
    main()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="GMS private deployment runtime")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="Generate local secrets and initial configuration")
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=5001)
    p.add_argument("--user", required=True)
    p.add_argument("--home", required=True)
    sub.add_parser("configure", help="Create administrator and optionally Redmine credentials")
    p = sub.add_parser("controller", help="Start Controller HTTPS server")
    p.add_argument("--port", type=int, default=5001)
    sub.add_parser("worker", help="Start local Worker")
    args = parser.parse_args()
    return {"prepare": prepare, "configure": configure, "controller": controller, "worker": worker}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
