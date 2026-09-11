#!/usr/bin/env python3
"""Repository-local installer/doctor for the GMS agent package.

This is intentionally a thin wrapper around the shipped ``gms-agent`` CLI:
developers get one stable command from the repository root while package
lifecycle behavior remains owned and tested in ``package_manager.py``.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent" / "gms-remote-test" / "runtime" / "gms-agent"
PACKAGE = ROOT / "agent" / "gms-remote-test"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    install = sub.add_parser("install", help="install this checkout as one package")
    install.add_argument(
        "--client",
        default="codex",
        choices=["auto", "codex", "kimi", "kkagent", "none"],
    )
    install.add_argument(
        "--server",
        default="",
        help="Controller URL (required unless --client none)",
    )
    install.add_argument("--ca-cert", default="")

    doctor = sub.add_parser("doctor", help="inspect the local package integration")
    doctor.add_argument(
        "--client", default="codex", choices=["auto", "codex", "kimi", "kkagent"]
    )
    doctor.add_argument("--profile", default="")
    doctor.add_argument("--json", action="store_true")

    args = parser.parse_args()
    env = dict(os.environ)
    if args.action == "install":
        if args.client != "none" and not args.server:
            parser.error("--server is required unless --client none")
        if args.ca_cert:
            ca_path = Path(args.ca_cert).expanduser().resolve()
            if not ca_path.is_file():
                parser.error(f"CA certificate does not exist: {ca_path}")
            env["GMS_INSTALL_CA_CERT"] = str(ca_path)
        argv = [
            sys.executable,
            str(AGENT),
            "install",
            "--client",
            args.client,
            "--package",
            str(PACKAGE),
        ]
        if args.server:
            argv.extend(["--server", args.server])
    else:
        argv = [
            sys.executable,
            str(AGENT),
            "doctor",
            "--client",
            args.client,
        ]
        if args.profile:
            argv.extend(["--profile", args.profile])
        if args.json:
            argv.append("--json")
    os.execve(sys.executable, argv, env)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
