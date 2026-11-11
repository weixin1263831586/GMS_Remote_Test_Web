#!/usr/bin/env python3
"""Update installed TLS paths without configuration symlinks.

Exit 0 means the unit is current or was updated, 3 means a dry run found an
update, and 1 means the unit could not be checked or updated. Applying changes
requires write access to the unit directory. The caller reloads systemd.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import find_repo_root


def update_unit(unit: Path, project_root: Path, cert_dir: Path, *, apply: bool) -> int:
    unit = unit.resolve(strict=True)
    original = unit.read_bytes()
    legacy = str(project_root / "configs/certs") + "/"
    canonical = str(cert_dir) + "/"
    updated = original.replace(legacy.encode(), canonical.encode())
    if updated == original:
        return 0
    if not apply:
        print("systemd HTTPS 证书路径需要更新")
        return 3

    metadata = unit.stat()
    backup_fd, backup_name = tempfile.mkstemp(prefix=unit.name + ".pre-canonical-tls-", dir=unit.parent)
    with os.fdopen(backup_fd, "wb") as backup:
        backup.write(original)
        backup.flush()
        os.fsync(backup.fileno())
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + unit.name + ".", dir=unit.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(updated)
            os.fchmod(output.fileno(), stat.S_IMODE(metadata.st_mode))
            os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
            output.flush()
            os.fsync(output.fileno())
        if unit.read_bytes() != original:
            raise RuntimeError("服务文件在更新期间发生变化，请重新执行")
        os.replace(temporary, unit)
        directory_fd = os.open(unit.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"systemd HTTPS 证书路径已更新，原服务文件备份至 {backup_name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--unit", type=Path, required=True)
    parser.add_argument("--cert-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        root = args.project_root.resolve() if args.project_root else find_repo_root()
        cert_dir = args.cert_dir.resolve() if args.cert_dir else root / "configs/secrets/certs"
        return update_unit(args.unit, root, cert_dir, apply=args.apply)
    except (OSError, RuntimeError) as exc:
        print(f"无法更新 systemd HTTPS 证书路径（需要服务目录写权限） {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
