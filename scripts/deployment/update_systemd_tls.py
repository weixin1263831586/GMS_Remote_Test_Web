#!/usr/bin/env python3
"""Update installed systemd service entrypoints and Controller TLS paths.

Exit 0 means the unit is current or was updated, 3 means a dry run found an
update, and 1 means the unit could not be checked or updated. Applying changes
requires write access to the unit directory. The caller reloads systemd.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import tempfile
from pathlib import Path


PYTHON_MODULE_COMMAND = (
    rb'(^[ \t]*ExecStart=[-@:+!]*(?:"[^"\r\n]*/python(?:3(?:\.\d+)?)?"'
    rb'|(?:[^ \t\r\n"]*/)?python(?:3(?:\.\d+)?)?)(?:[ \t]+-[A-Za-z]+)*?[ \t]+-m[ \t]+)'
)
MODULE_NAME = rb"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*"
MODULE_ENTRYPOINTS = {
    "controller": (rb"uvicorn(?=\s|$)", b"bootstrap.server"),
    "worker": (MODULE_NAME + rb"(?=\s|$)", b"worker.app"),
    "daily-brief": (MODULE_NAME + rb"(?=[ \t]+run-(?:nightly|delta)(?=\s|$))", b"features.redmine.daily_brief.cli"),
}
SCRIPT_ENTRYPOINTS = {
    "worker-software": (
        ("scripts/configure_local_worker_software.sh", "scripts/deployment/worker/configure_worker_software.sh"),
        ("scripts/configure_worker_software.sh", "scripts/deployment/worker/configure_worker_software.sh"),
    ),
    "backup": (("scripts/run_production_backup.sh", "scripts/backup/run_production_backup.sh"),),
}


def update_unit(
    unit: Path, project_root: Path, cert_dir: Path, *, apply: bool, entrypoint: str = "controller",
) -> int:
    unit = unit.resolve(strict=True)
    original = unit.read_bytes()
    updated = original
    if entrypoint == "controller":
        legacy = str(project_root / "configs/certs") + "/"
        canonical = str(cert_dir) + "/"
        updated = updated.replace(legacy.encode(), canonical.encode())
    lines = []
    for line in updated.splitlines(keepends=True):
        if line.lstrip().startswith(b"ExecStart="):
            if entrypoint in MODULE_ENTRYPOINTS:
                pattern, current = MODULE_ENTRYPOINTS[entrypoint]
                line = re.sub(PYTHON_MODULE_COMMAND + pattern, rb"\g<1>" + current, line, count=1)
            for previous_path, current_path in SCRIPT_ENTRYPOINTS.get(entrypoint, ()):
                pattern = re.escape(os.fsencode(project_root / previous_path)) + rb"""(?=[\s"']|$)"""
                replacement = os.fsencode(project_root / current_path)
                line = re.sub(pattern, lambda _, value=replacement: value, line, count=1)
        lines.append(line)
    updated = b"".join(lines)
    if updated == original:
        return 0
    if entrypoint in {"worker", "daily-brief"}:
        directories = re.findall(rb"(?m)^[ \t]*WorkingDirectory[ \t]*=[ \t]*-?([^\r\n]+)", original)
        if directories:
            working_dir = Path(os.fsdecode(directories[-1]).strip().strip("\"'"))
            module = MODULE_ENTRYPOINTS[entrypoint][1].decode("ascii")
            module_file = working_dir / (module.replace(".", "/") + ".py")
            if working_dir.is_absolute() and not module_file.is_file():
                raise RuntimeError(f"{unit.name} 的工作目录缺少 {module}，请先部署当前运行时")
    if not apply:
        print(f"systemd 服务配置需要更新: {unit.name}")
        return 3

    metadata = unit.stat()
    backup_prefix = ".pre-canonical-tls-" if entrypoint == "controller" else ".pre-canonical-entrypoint-"
    backup_fd, backup_name = tempfile.mkstemp(prefix=unit.name + backup_prefix, dir=unit.parent)
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
    print(f"systemd 服务配置已更新，原服务文件备份至 {backup_name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--unit", type=Path, required=True)
    parser.add_argument("--cert-dir", type=Path)
    parser.add_argument("--entrypoint", choices=(*MODULE_ENTRYPOINTS, *SCRIPT_ENTRYPOINTS), default="controller")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        root = args.project_root.resolve() if args.project_root else Path(__file__).resolve().parents[2]
        cert_dir = args.cert_dir.resolve() if args.cert_dir else root / "configs/secrets/certs"
        return update_unit(args.unit, root, cert_dir, apply=args.apply, entrypoint=args.entrypoint)
    except (OSError, RuntimeError) as exc:
        print(f"无法更新 systemd 服务配置 {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
