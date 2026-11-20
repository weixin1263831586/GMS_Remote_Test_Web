#!/usr/bin/env python3
"""Build a signed, source-free experimental Controller/Worker distribution.

Run only on a trusted, disposable Ubuntu 22.04 build host with Nuitka,
a C compiler, the project's pinned dependencies and a private GPG key.
The build refuses to fall back to shipping Python source if compilation fails.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
EXCLUDED = {".git", ".venv", "data", "tests", "__pycache__", "secrets", "local", "node_modules", ".pytest_cache"}
SOURCE_SUFFIXES = {".py", ".pyc", ".pyo", ".pyi", ".pyx", ".pxd", ".c", ".h", ".map"}
REQUIRED = {"install.sh", "runtime/gms-runtime", "web", "configs/examples"}


def digest(file: Path) -> str:
    hashed = hashlib.sha256()
    with file.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hashed.update(block)
    return hashed.hexdigest()


def private_tree_findings(root: Path) -> list[str]:
    findings = []
    for file in root.rglob("*"):
        relative = file.relative_to(root)
        if any(p in EXCLUDED for p in relative.parts):
            findings.append(f"private/runtime path: {relative}")
            continue
        if file.is_symlink():
            resolved = file.resolve()
            if resolved != root and root not in resolved.parents:
                findings.append(f"external link: {relative}")
            continue
        if file.is_file():
            if file.suffix.lower() in SOURCE_SUFFIXES:
                findings.append(f"source or debug artifact: {relative}")
            if file.name.lower() in {"gts-rockchip.json", "environment.json", "runtime.json", ".env"}:
                findings.append(f"runtime credential or config: {relative}")
            if file.suffix.lower() in {".pem", ".key"}:
                findings.append(f"unexpected key material: {relative}")
    for item in REQUIRED:
        if not (root / item).exists():
            findings.append(f"missing required artifact: {item}")
    return findings


def _copy_resources(target: Path) -> None:
    shutil.copytree(PROJECT / "web", target / "web",
                    ignore=shutil.ignore_patterns("*.py", "*.pyc", "*.pyo", "*.map", "__pycache__"))
    examples = target / "configs/examples"
    examples.mkdir(parents=True)
    for file in (PROJECT / "configs/examples").glob("*.json"):
        shutil.copy2(file, examples / file.name)
    installer = PROJECT / "scripts/install_private_release.sh"
    if not installer.is_file():
        raise FileNotFoundError(installer)
    shutil.copy2(installer, target / "install.sh")
    (target / "install.sh").chmod(0o755)
    # Only explicitly approved non-Python assets. No machine identities or
    # privileged local scripts from the build workstation are copied.
    for relative in ("tools/tesseract", "tools/adbproxy-rs/dist"):
        src = PROJECT / relative
        if src.exists() and src.is_dir():
            dst = target / relative
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst,
                            ignore=shutil.ignore_patterns("*.py", "*.pyc", "*.pyo", "*.map", "__pycache__"))


def build(args: argparse.Namespace) -> None:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise RuntimeError("Private release builds require Linux x86_64")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.version):
        raise ValueError("Invalid release version")
    if not os.environ.get("GMS_RELEASE_SIGNING_KEY", "").strip():
        raise RuntimeError("GMS_RELEASE_SIGNING_KEY is mandatory for private release signing")
    if shutil.which("gpg") is None:
        raise RuntimeError("gpg is required")
    try:
        import nuitka  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("Install a pinned and reviewed Nuitka version on the build host") from exc
    output = Path(args.dist_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    filename = f"gms-web-app-private-{args.version}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="gms-private-release-") as tmp:
        base = Path(tmp)
        stage = base / "stage"
        stage.mkdir()
        release = stage / "gms-web-app"
        release.mkdir()
        nuitka_output = base / "compile"
        command = [
            sys.executable, "-m", "nuitka",
            "--standalone", "--follow-imports",
            "--assume-yes-for-downloads",
            "--output-filename=gms-runtime",
            f"--output-dir={nuitka_output}",
            "--include-module=app",
            "--include-package=bootstrap",
            "--include-package=foundation",
            "--include-package=features",
            "--include-package=worker_agent",
            "--include-package=scripts",
            "--include-package=plugins",
            "--nofollow-import-to=tests,*.tests",
            str(PROJECT / "deployment/private_runtime.py"),
        ]
        print("Compiling private runtime (Nuitka standalone)...", flush=True)
        subprocess.run(command, cwd=PROJECT, check=True)
        compiled = nuitka_output / "private_runtime.dist"
        if not (compiled / "gms-runtime").is_file():
            raise RuntimeError("Nuitka did not produce the expected standalone executable")
        shutil.move(str(compiled), str(release / "runtime"))
        _copy_resources(release)
        findings = private_tree_findings(release)
        if findings:
            raise RuntimeError("Private release rejected:\n" + "\n".join(findings[:60]))
        manifest = {
            "format": "gms-private-runtime-v1",
            "version": args.version,
            "platform": "linux-x86_64",
            "compiler": "nuitka-standalone",
            "files": {
                str(f.relative_to(release)): digest(f)
                for f in sorted(release.rglob("*")) if f.is_file() and not f.is_symlink()
            },
        }
        (release / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        archive = output / filename
        with tarfile.open(archive, "w:gz", dereference=False) as tar:
            tar.add(release, arcname="gms-web-app")
        checksum = output / (filename + ".sha256")
        checksum.write_text(f"{digest(archive)}  {filename}\n")
        subprocess.run([
            "gpg", "--batch", "--yes", "--armor", "--detach-sign",
            "--local-user", os.environ["GMS_RELEASE_SIGNING_KEY"],
            "--output", str(archive) + ".sig", str(archive),
        ], check=True)
    print(f"Signed private release: {archive}")


def verify(args: argparse.Namespace) -> None:
    root = Path(args.release_root).resolve()
    issues = private_tree_findings(root)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        issues.append("missing manifest.json")
    else:
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("format") != "gms-private-runtime-v1":
            issues.append("unknown private release format")
        expected = manifest.get("files", {})
        if not isinstance(expected, dict):
            issues.append("invalid manifest files")
        else:
            actual = {str(f.relative_to(root)): digest(f) for f in root.rglob("*")
                      if f.is_file() and not f.is_symlink() and f != manifest_path}
            if expected != actual:
                issues.append("manifest file set or SHA-256 mismatch")
    if issues:
        raise RuntimeError("Release verification failed:\n" + "\n".join(issues[:60]))
    print("Private release tree verified")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("build")
    p.add_argument("--version", required=True)
    p.add_argument("--dist-dir", default="dist")
    p = sub.add_parser("verify")
    p.add_argument("release_root")
    args = parser.parse_args()
    (build if args.action == "build" else verify)(args)


if __name__ == "__main__":
    main()
