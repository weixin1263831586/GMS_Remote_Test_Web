#!/usr/bin/env python3
"""Nuitka entrypoint for an independent, source-free GMS Controller distribution.

This file is only an input to the trusted build. It MUST NOT be shipped.
Modes: init (interactive, no passwords in argv), controller, worker, backup, doctor.
"""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def release_root() -> Path:
    return Path(os.environ.get("GMS_PROJECT_ROOT") or Path(sys.argv[0]).resolve().parent).resolve()


def _set_env(root: Path) -> None:
    os.environ["GMS_PROJECT_ROOT"] = str(root)
    os.environ.setdefault("GMS_DATA_ROOT", str(root / "data"))
    os.environ.setdefault("GMS_ENV", "production")
    os.chdir(root)


def _write_private(path: Path, content: bytes, *, overwrite: bool = False) -> None:
    """Create a secret without truncating an existing secret or following a symlink."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise RuntimeError(f"refusing symlinked secret: {path}")
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if not overwrite:
            path.unlink(missing_ok=True)
        raise
    path.chmod(0o600)


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid JSON object: {path}")
    return value


def _save_json(path: Path, payload: dict) -> None:
    """Atomic replacement of configuration, mode 0600; never print secrets."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise RuntimeError(f"refusing symlinked config file: {path}")
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        _write_private(tmp, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _certificates(root: Path, host: str) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    certs = root / "configs/secrets/certs"
    key_path, cert_path = certs / "gms-local.key", certs / "gms-local.crt"
    ca_key_path, ca_path = certs / "gms-local-ca.key", certs / "gms-local-ca.crt"
    required = (key_path, cert_path, ca_key_path, ca_path)
    if any(path.exists() for path in required):
        if not all(path.is_file() and not path.is_symlink() for path in required):
            raise RuntimeError("incomplete HTTPS/CA certificate set; refusing unsafe regeneration")
        return
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    tls_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    names = [x509.DNSName("localhost")]
    try:
        names.append(x509.IPAddress(ipaddress.ip_address(host)))
    except ValueError:
        if not host or any(ch in host for ch in "/\\@:"):
            raise RuntimeError("host must be an IP address or DNS hostname")
        names.append(x509.DNSName(host))
    names.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    now = datetime.now(timezone.utc)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "GMS Instance CA")])
    tls_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    ca_cert = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1095))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True,
            crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .sign(ca_key, hashes.SHA256()))
    server_cert = (x509.CertificateBuilder().subject_name(tls_name).issuer_name(ca_name)
        .public_key(tls_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256()))
    for path, key in ((ca_key_path, ca_key), (key_path, tls_key)):
        _write_private(path, key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    _write_private(ca_path, ca_cert.public_bytes(serialization.Encoding.PEM))
    _write_private(cert_path, server_cert.public_bytes(serialization.Encoding.PEM))


def _bootstrap(root: Path, host: str, port: int) -> None:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if not (1 <= port <= 65535):
        raise RuntimeError("port must be between 1 and 65535")
    if not host or any(ch in host for ch in "/\\@:"):
        raise RuntimeError("invalid host name/IP")
    configs = root / "configs"
    local, private = configs / "local", configs / "secrets"
    local.mkdir(parents=True, exist_ok=True, mode=0o700)
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_root = Path(os.environ["GMS_DATA_ROOT"])
    data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (data_root / "local-worker").mkdir(parents=True, exist_ok=True, mode=0o700)
    _certificates(root, host)

    master, audit = private / "master.key", private / "audit_hmac.key"
    signing = private / "skill-signing-ed25519.pem"
    worker_token = private / "local-worker.token"
    backup_key = private / "backup.key"
    _write_private(master, Fernet.generate_key())
    _write_private(audit, secrets.token_hex(32).encode())
    _write_private(signing, Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    _write_private(worker_token, secrets.token_urlsafe(48).encode())
    _write_private(backup_key, secrets.token_bytes(32))
    token_config = private / "worker_tokens.json"
    tokens = _read_json(token_config)
    worker_id = "ats-worker-controller"
    worker_tokens = tokens.get("worker_tokens") or {}
    if not isinstance(worker_tokens, dict):
        raise RuntimeError("invalid existing Worker token configuration")
    worker_tokens.setdefault(worker_id, worker_token.read_text(encoding="utf-8").strip())
    _save_json(token_config, {"worker_tokens": worker_tokens})

    existing = _read_json(private / "environment.json")
    defaults = {
        "GMS_BOOTSTRAP_TOKEN": secrets.token_urlsafe(48),
        "GMS_METRICS_TOKEN": secrets.token_urlsafe(48),
        "GMS_AUTOMATION_WEBHOOK_TOKEN": secrets.token_urlsafe(48),
        "GMS_SECRET_KEY_FILE": str(master),
        "GMS_AUDIT_HMAC_KEY_FILE": str(audit),
        "GMS_SKILL_SIGNING_KEY_FILE": str(signing),
        "GMS_WORKER_TOKENS_FILE": str(token_config),
        "GMS_AUTOMATION_OWNER_ID": "service-automation",
        "GMS_AUTH_REQUIRED": "true",
        "GMS_SECURE_COOKIES": "true",
    }
    for key, value in defaults.items():
        existing.setdefault(key, value)
    _save_json(private / "environment.json", existing)
    env = _read_json(local / "environment.json")
    env.update({
        "GMS_ENV": "production", "GMS_PORT": str(port),
        "GMS_ALLOWED_ORIGINS": f"https://{host}:{port},https://localhost:{port}",
        "TRUSTED_HOSTS": f"{host},localhost,127.0.0.1",
    })
    _save_json(local / "environment.json", env)

    from bootstrap.env_loader import load_runtime_env
    load_runtime_env()
    # Install an editable, sanitized default configuration, never copied from
    # the developer host. Modules may then use the existing ConfigManager.
    config = local / "config.json"
    if not config.exists():
        example = configs / "examples/config.example.json"
        if not example.is_file():
            raise RuntimeError("missing sanitized config.example.json in compiled release")
        from scripts.sanitize_release_config import sanitize_product_config
        _save_json(config, sanitize_product_config(_read_json(example)))

    cert_pem = (private / "certs/gms-local-ca.crt").read_text(encoding="utf-8")
    run_home = Path.home()
    worker_config = data_root / "local-worker/config.json"
    if not worker_config.exists():
        _save_json(worker_config, {
            "worker_id": worker_id, "name": f"{worker_id} (local)",
            "controller_url": f"https://127.0.0.1:{port}",
            "address": "127.0.0.1", "ssh_user": getpass.getuser(),
            "controller_ca": cert_pem, "worker_token_file": str(worker_token),
            "heartbeat_interval_seconds": 15, "suite_scan_interval_seconds": 300,
            "max_jobs": 2,
            "suite_roots": [str(run_home / "GMS-Suite"), "/opt/GMS-Suite"],
            "data_root": str(data_root / "local-worker/runtime"),
        })
    (run_home / "GMS-Suite").mkdir(parents=True, exist_ok=True)


def _admin_and_redmine(noninteractive: bool) -> None:
    from features.auth import auth_service
    from features.redmine.config import RedmineConfig

    auth_service.initialize()
    if auth_service.setup_required():
        if noninteractive:
            print("Admin setup pending: use the existing secure web bootstrap flow.")
            return
        print("\n首次初始化：创建平台管理员（输入密码时不会显示字符）")
        username = input("管理员账号 [admin]: ").strip() or "admin"
        display_name = input("显示名称 [Administrator]: ").strip() or "Administrator"
        while True:
            password = getpass.getpass("管理员密码: ")
            repeated = getpass.getpass("确认密码: ")
            if password != repeated:
                print("两次密码不一致，请重试")
            elif len(password) < 8:
                print("密码至少需要 8 个字符")
            else:
                break
        admin = auth_service.create_initial_admin(username, password, display_name)
        del password, repeated
        print("平台管理员已创建。")
    else:
        # Do not reassign another user's per-owner credentials on reinstall.
        print("平台管理员已存在；不会覆盖账号、会话和凭据。")
        return

    url = input("Redmine URL（可留空，稍后在平台设置）: ").strip()
    if not url:
        return
    manager = RedmineConfig().for_owner(admin.id)
    if not manager.save_redmine_base_url(url):
        raise RuntimeError("failed to save Redmine URL")
    mode = input("Redmine 凭据类型 [api_key/password/skip] (api_key): ").strip().lower() or "api_key"
    if mode == "api_key":
        key = getpass.getpass("Redmine API Key（留空跳过）: ").strip()
        if key and not manager.save_redmine_api_key(key):
            raise RuntimeError("failed to save Redmine API Key")
    elif mode == "password":
        user = input("Redmine 用户名: ").strip()
        password = getpass.getpass("Redmine 密码: ")
        if user and password and not manager.save_redmine_credentials(user, password):
            raise RuntimeError("failed to save Redmine credentials")
    elif mode != "skip":
        raise RuntimeError("invalid Redmine credential mode")
    print("Redmine 配置已保存到当前管理员的专属加密配置；网络连通性请在 UI 中验证。")


def init(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Initialize independent GMS release")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--no-admin-prompt", action="store_true",
                        help="leave secure administrator creation to web bootstrap")
    args = parser.parse_args(argv)
    root = release_root()
    _set_env(root)
    os.umask(0o077)
    _bootstrap(root, args.host, args.port)
    _admin_and_redmine(args.no_admin_prompt)
    print(f"Initialization completed: https://{args.host}:{args.port}")
    return 0


def controller() -> int:
    root = release_root()
    _set_env(root)
    from bootstrap.env_loader import load_runtime_env
    load_runtime_env()
    from bootstrap.application import create_app
    from bootstrap.http_protocol import ShutdownHttpProtocol
    import uvicorn
    app = create_app()
    port = int(os.environ.get("GMS_PORT", "5001"))
    tls = root / "configs/secrets/certs"
    uvicorn.run(app, host="0.0.0.0", port=port, workers=1, http=ShutdownHttpProtocol,
                backlog=2048, limit_concurrency=512, timeout_keep_alive=120,
                access_log=True, ssl_keyfile=str(tls / "gms-local.key"),
                ssl_certfile=str(tls / "gms-local.crt"))
    return 0


def worker() -> int:
    root = release_root()
    _set_env(root)
    from bootstrap.env_loader import load_runtime_env
    load_runtime_env()
    os.environ.setdefault("GMS_WORKER_CONFIG", str(Path(os.environ["GMS_DATA_ROOT"]) / "local-worker/config.json"))
    from worker_agent.entrypoint import main
    main()
    return 0


def smoke() -> int:
    """Import and assemble a production Controller + Worker without listening.

    Runs only against a disposable, fully initialized copy of the binary tree.
    Missing native imports, dynamic routers and resource paths must fail before
    any customer archive is signed.
    """
    root = release_root()
    _set_env(root)
    from bootstrap.env_loader import load_runtime_env
    load_runtime_env()
    from bootstrap.application import create_app
    from worker_agent.entrypoint import main as worker_entrypoint
    from bootstrap.production_security import validate_production_security_configuration
    validate_production_security_configuration()
    app = create_app()
    routes = {getattr(route, "path", "") for route in app.routes}
    needed = {"/api/auth/status", "/api/auth/setup"}
    if not needed.issubset(routes) or len(routes) < 30:
        raise RuntimeError("compiled Controller smoke test: missing routes")
    if not callable(worker_entrypoint):
        raise RuntimeError("compiled Worker entrypoint missing")
    print(json.dumps({"smoke": "ok", "route_count": len(routes)}))
    return 0


def doctor() -> int:
    required = ["web/shell/shell.html", "web/static", "configs/examples/config.example.json"]
    checks = {entry: (root / entry).exists() for entry in required}
    checks["binary_root"] = root.is_dir()
    print(json.dumps({"root": str(root), "checks": checks}, ensure_ascii=False))
    return 0 if all(checks.values()) else 1


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: gms-runtime {init|controller|worker|backup|doctor|smoke}", file=sys.stderr)
        return 2
    action = sys.argv[1]
    if action == "init":
        return init(sys.argv[2:])
    if action == "controller":
        return controller()
    if action == "worker":
        return worker()
    if action == "backup":
        root = release_root()
        _set_env(root)
        from scripts.gms_backup import main as backup_main
        return backup_main(sys.argv[2:])
    if action == "doctor":
        return doctor()
    if action == "smoke":
        return smoke()
    print(f"unknown mode: {action}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
