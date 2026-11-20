#!/usr/bin/env python3
"""Build a signed, independently installable Nuitka Standalone distribution.

Run on the trusted Ubuntu 22.04 / x86_64 build machine (not on the customer
host). This command fails closed if the native build or source leakage check
fails. A Nuitka-compiled runtime is required; it is never silently replaced
with Python sources, PyInstaller bytecode, or a development checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from scripts.release.verify_binary_tree import inspect

ROOT = Path(__file__).resolve().parents[2]
RESOURCE_FILES = (
    "scripts/run_GMS_Test_Auto.sh",
    "scripts/run_GSI_Burn.sh",
    "scripts/run_Device_Lock.sh",
    "scripts/configure_local_worker_software.sh",
    "scripts/install_cluster_worker.sh",
)
RUNTIME_ASSETS = (
    "tools/tesseract",
    "tools/jadx",
    "tools/upgrade_tool",
    "tools/adbproxy-rs/dist",
    "tools/gms-worker-native/dist",
)
FORBIDDEN_SOURCE_ENDINGS = (".py", ".pyc", ".pyo", ".pyi", ".map")


def _run(args: list[str], **kwargs) -> None:
    subprocess.run(args, check=True, **kwargs)


def _copy_allowed(source: Path, target: Path) -> None:
    if not source.exists():
        return
    if source.is_symlink():
        raise RuntimeError(f"symlinked asset rejected: {source}")
    if source.is_dir():
        for child in source.iterdir():
            _copy_allowed(child, target / child.name)
        return
    if source.suffix.lower() in FORBIDDEN_SOURCE_ENDINGS or source.name.startswith(".env"):
        # Never copy executable Python sources or debug maps to the recipient.
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _stage_assets(runtime: Path) -> None:
    for rel in ("web/static", "web/shell", "configs/examples", *RESOURCE_FILES, *RUNTIME_ASSETS):
        _copy_allowed(ROOT / rel, runtime / rel)
    # No developer configs/local/, configs/secrets/, data/ or personal tokens.
    required = ("web/static", "web/shell/shell.html", "configs/examples/config.example.json")
    missing = [name for name in required if not (runtime / name).exists()]
    if missing:
        raise RuntimeError(f"missing required runtime assets: {missing}")


def _compile_native(out: Path) -> Path:
    compiler = [
        sys.executable, "-m", "nuitka",
        "--standalone", "--follow-imports",
        "--assume-yes-for-downloads",
        "--python-flag=no_asserts",
        f"--output-dir={out}",
        "--output-filename=gms-runtime",
        "--include-package=bootstrap",
        "--include-package=foundation",
        "--include-package=features",
        "--include-package=worker_agent",
        "--include-package=workflows",
        "--include-module=scripts.gms_backup",
        "--include-module=scripts.sanitize_release_config",
        str(ROOT / "scripts/release/compiled_entry.py"),
    ]
    _run(compiler, cwd=ROOT)
    dist = out / "compiled_entry.dist"
    if not (dist / "gms-runtime").is_file():
        raise RuntimeError(f"Nuitka standalone output missing binary: {dist}")
    return dist


def _write_manifest(stage: Path, version: str) -> None:
    files = {}
    for path in sorted(stage.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rel = path.relative_to(stage).as_posix()
            if rel not in {"manifest.json", "manifest.sha256"}:
                files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    (stage / "manifest.json").write_text(
        json.dumps({"format": "gms-compiled-v1", "version": version,
                    "os": "linux", "arch": "x86_64", "files": files},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    entries = list(files.items())
    entries.append(("manifest.json", hashlib.sha256((stage / "manifest.json").read_bytes()).hexdigest()))
    (stage / "manifest.sha256").write_text(
        "".join(f"{checksum}  {name}\n" for name, checksum in sorted(entries)),
        encoding="utf-8")


def build(args: argparse.Namespace) -> Path:
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise RuntimeError("first supported compiled target is Linux x86_64 / Ubuntu 22.04")
    if not args.signing_key:
        raise RuntimeError("GMS_RELEASE_SIGNING_KEY required: unsigned release forbidden")
    if not args.version or not all(ch.isalnum() or ch in ".-_" for ch in args.version):
        raise RuntimeError("version must contain only letters, numbers, dots, '-' and '_'")
    dest = args.dist_dir.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gms-compiled-build-") as temp:
        temp_root = Path(temp)
        compiled = args.compiled_dist.resolve() if args.compiled_dist else _compile_native(temp_root / "build")
        if not (compiled / "gms-runtime").is_file():
            raise RuntimeError("compiled distro requires an executable gms-runtime")
        stage = temp_root / "gms-web-app"
        runtime = stage / "runtime"
        shutil.copytree(compiled, runtime, symlinks=False)
        _stage_assets(runtime)
        shutil.copy2(ROOT / "scripts/release/install_compiled.sh", stage / "install.sh")
        (stage / "install.sh").chmod(0o755)
        findings = inspect(stage)
        if findings:
            raise RuntimeError("refusing source-leaking release:\n" + "\n".join(findings[:50]))
        # Runtime must pass at least its *static* self-test before signing.
        # A full Controller/Worker browser-and-device smoke test is a release gate.
        _run([str(runtime / "gms-runtime"), "doctor"],
             cwd=runtime, env={**os.environ, "GMS_PROJECT_ROOT": str(runtime)})
        _write_manifest(stage, args.version)
        archive = dest / f"gms-web-app-{args.version}-ubuntu22.04-x86_64.tar.gz"
        with tarfile.open(archive, "w:gz") as package:
            package.add(stage, arcname="gms-web-app", recursive=True)
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        (dest / f"{archive.name}.sha256").write_text(f"{checksum}  {archive.name}\n", encoding="utf-8")
        _run(["gpg", "--batch", "--yes", "--armor", "--detach-sign",
              "--local-user", args.signing_key, "--output", str(archive) + ".sig",
              str(archive)])
        print(f"Signed compiled distribution: {archive}")
        return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--compiled-dist", type=Path,
                        help="previously built Nuitka .dist (still checked for leaks)")
    parser.add_argument("--signing-key", default=os.environ.get("GMS_RELEASE_SIGNING_KEY", ""))
    args = parser.parse_args(argv)
    try:
        build(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"release build failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
