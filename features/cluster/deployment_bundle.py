"""Helpers for assembling the self-contained remote Worker bundle."""

from __future__ import annotations

import hashlib
import os
import re
import tarfile
from pathlib import Path


_NATIVE_TOOL_NAMES = ("gms-process-inventory", "gms-usbip-control")


def add_worker_python_runtime(
    bundle: tarfile.TarFile,
    project_root: Path,
) -> None:
    """Bundle every in-repository Python package imported by the Worker."""
    bundle.add(project_root / "worker_agent", arcname="worker_agent")
    bundle.add(project_root / "foundation", arcname="foundation")


def add_worker_native_runtime(
    bundle: tarfile.TarFile,
    project_root: Path,
) -> None:
    """Validate and bundle the required Rust Worker executors."""
    installer = project_root / "scripts/install_gms_worker_native.sh"
    dist_root = project_root / "tools/gms-worker-native/dist"
    if (
        not installer.is_file()
        or not os.access(installer, os.X_OK)
        or not dist_root.is_dir()
    ):
        raise RuntimeError(
            "native Worker package is missing; run "
            "scripts/build_gms_worker_native.sh on the Controller"
        )

    architecture_roots = sorted(path for path in dist_root.iterdir() if path.is_dir())
    if not architecture_roots:
        raise RuntimeError(
            "native Worker package is empty; run "
            "scripts/build_gms_worker_native.sh on the Controller"
        )
    for architecture_root in architecture_roots:
        manifest = architecture_root / "SHA256SUMS"
        if not manifest.is_file():
            raise RuntimeError(
                f"native Worker checksum manifest is missing: {manifest}"
            )
        checksums: dict[str, str] = {}
        for line in manifest.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) >= 2 and re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]):
                checksums[Path(fields[-1].lstrip("*")).name] = fields[0].lower()
        for binary_name in _NATIVE_TOOL_NAMES:
            binary = architecture_root / binary_name
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise RuntimeError(f"native Worker executable is missing: {binary}")
            expected = checksums.get(binary_name)
            if expected is None:
                raise RuntimeError(
                    f"native Worker checksum is missing for {binary_name}"
                )
            digest = hashlib.sha256()
            with binary.open("rb") as binary_file:
                for chunk in iter(lambda: binary_file.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise RuntimeError(
                    f"native Worker checksum mismatch for {binary_name}"
                )

    bundle.add(installer, arcname="scripts/install_gms_worker_native.sh")
    bundle.add(dist_root, arcname="tools/gms-worker-native/dist")


def add_worker_runtime(
    bundle: tarfile.TarFile,
    project_root: Path,
) -> None:
    """Bundle the complete Python and native Worker runtime."""
    add_worker_python_runtime(bundle, project_root)
    add_worker_native_runtime(bundle, project_root)
