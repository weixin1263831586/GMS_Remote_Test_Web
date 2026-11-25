#!/usr/bin/env python3
"""Build a signed, independently installable Nuitka Standalone distribution.

Run on the trusted Ubuntu 22.04 / x86_64 build machine (not on the customer
host). This command fails closed if the native build or source leakage check
fails. A Nuitka-compiled runtime is required; it is never silently replaced
with Python sources, PyInstaller bytecode, or a development checkout.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


# Direct execution (python scripts/release/build_binary_release.py) puts only
# scripts/release on sys.path. Add the checkout root before importing the
# release verifier; CI and the documented publishing command both use this mode.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from foundation.native_tools import compiled_worker_members, write_compiled_worker_manifest  # noqa: E402
from scripts.release.compiled_entry import REQUIRED_SOFTWARE_ASSETS  # noqa: E402


RESOURCE_FILES = (
    "scripts/execution/run_GMS_Test_Auto.sh",
    "scripts/execution/run_GSI_Burn.sh",
    "scripts/execution/run_Device_Lock.sh",
    "scripts/execution/fastboot_common.sh",
    "scripts/deployment/worker/install_cluster_worker.sh",
    "scripts/deployment/worker/deploy_worker.sh",
    "scripts/deployment/worker/install_adb_proxy_source_worker.sh",
    *(path for path in REQUIRED_SOFTWARE_ASSETS if path.startswith("scripts/")),
    "scripts/backup/run_production_backup.sh",
)
RUNTIME_ASSETS = (
    "tools/tesseract",
    "tools/jadx",
    "tools/upgrade_tool",
    "tools/adbproxy-rs/dist",
    "tools/gms-worker-native/dist",
)
# Resource paths are loaded from disk by page routers after Nuitka compilation.
# Keep the list explicit and verified: compiling Python alone cannot include them.
ARCHITECTURE_SOURCES = (
    "platform-topology.mmd", "usbip-topology.mmd", "aoa-topology.mmd",
    "firmware-topology.mmd", "test-flow-topology.mmd", "agent-analysis-topology.mmd",
)
REQUIRED_UI_PAGES = (
    "features/automation/ui/page.html",
    "features/cluster/ui/page.html",
    "features/devices/ui/page.html",
    "features/gerrit/ui/page.html",
    "features/redmine/ui/page.html",
    "features/gms/ui/page.html",
    "features/gms/radar/ui/page.html",
)
REQUIRED_UI_SCRIPTS = tuple(str(Path(page).with_name("page.js")) for page in REQUIRED_UI_PAGES)
FORBIDDEN_SOURCE_ENDINGS = (".py", ".pyc", ".pyo", ".pyi", ".map")


def _run(args: list[str], **kwargs) -> None:
    subprocess.run(args, check=True, **kwargs)


def _copy_allowed(
    source: Path, target: Path, *, allowed_root: Path | None = None,
) -> None:
    # Tool bundles (e.g. Tesseract) contain SONAME symlinks to files in the
    # same directory. Preserve usability without ever packaging a link that
    # escapes the explicitly selected source tree.
    if not source.exists() and not source.is_symlink():
        # Some optional host tools are absent on the trusted build machine.
        return
    if allowed_root is None:
        allowed_root = (source if source.is_dir() and not source.is_symlink()
                        else source.parent).resolve(strict=True)
    if source.is_symlink():
        try:
            resolved = source.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(f"broken or cyclic release asset symlink: {source}") from exc
        if not resolved.is_file() or not resolved.is_relative_to(allowed_root):
            raise RuntimeError(f"release asset symlink escapes allowed tree: {source}")
        if (resolved.suffix.lower() in FORBIDDEN_SOURCE_ENDINGS
                or resolved.name.startswith(".env")):
            raise RuntimeError(f"release asset symlink resolves to source or secret: {source}")
    elif not source.exists():
        return
    if source.is_dir():
        for child in sorted(source.iterdir()):
            _copy_allowed(child, target / child.name, allowed_root=allowed_root)
        return
    if source.suffix.lower() in FORBIDDEN_SOURCE_ENDINGS or source.name.startswith(".env"):
        # Never copy executable Python sources or debug maps to the recipient.
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    # Dereference accepted in-tree aliases into ordinary files; manifest
    # verification and tar extraction must never encounter unsafe symlinks.
    shutil.copy2(source, target, follow_symlinks=True)


def _stage_assets(runtime: Path) -> None:
    for rel in (
        "web/static", "web/shell", "web/templates/architecture.html",
        "configs/examples", *RESOURCE_FILES, *REQUIRED_SOFTWARE_ASSETS, *RUNTIME_ASSETS,
    ):
        _copy_allowed(ROOT / rel, runtime / rel)
    # These six exact source files are read by /templates/architecture.html.
    for name in ARCHITECTURE_SOURCES:
        rel = Path("docs/architecture") / name
        _copy_allowed(ROOT / rel, runtime / rel)
    # Embedded feature pages (and their CSS/JS chunks) are read from
    # Path(__file__).with_name("ui") after a compiled Router receives a request.
    # Only UI assets are copied, never the surrounding Python implementation.
    features = ROOT / "features"
    for ui in sorted(features.rglob("ui")):
        if ui.is_dir() and not ui.is_symlink() and "tests" not in ui.relative_to(features).parts:
            rel = ui.relative_to(ROOT)
            _copy_allowed(ui, runtime / rel)
    # No developer configs/local/, configs/secrets/, data/ or personal tokens.
    required = (
        "web/static", "web/shell/shell.html", "configs/examples/config.example.json",
        "web/templates/architecture.html",
        *(f"docs/architecture/{name}" for name in ARCHITECTURE_SOURCES),
        *REQUIRED_UI_PAGES, *REQUIRED_UI_SCRIPTS, *RESOURCE_FILES, *REQUIRED_SOFTWARE_ASSETS,
    )
    missing = [name for name in required if not (runtime / name).is_file()
               and not (runtime / name).is_dir()]
    if missing:
        raise RuntimeError(f"missing required runtime assets: {missing}")
    for rel in RESOURCE_FILES:
        (runtime / rel).chmod(0o755)


def _production_modules() -> list[str]:
    """Explicit module inventory without accidentally compiling tests or fixtures."""
    modules = []
    for top in ("bootstrap", "foundation", "features", "worker", "scripts"):
        for source in (ROOT / top).rglob("*.py"):
            relative = source.relative_to(ROOT)
            if "tests" in relative.parts or "test" in relative.parts or "release" in relative.parts:
                continue
            if source.name.startswith("test_") or source.name in {"conftest.py", "__init__.py"}:
                continue
            modules.append(".".join(relative.with_suffix("").parts))
    return sorted(set(modules))


def _compile_native(out: Path) -> Path:
    agent_module = _write_agent_module(out.parent / "bundled-agent")
    compiler = [
        sys.executable, "-m", "nuitka",
        "--standalone", "--follow-imports",
        "--jobs=4",
        "--assume-yes-for-downloads",
        "--python-flag=no_asserts",
        f"--output-dir={out}",
        "--output-filename=gms-runtime",
        *[f"--include-module={module}" for module in _production_modules()],
        "--include-module=_gms_bundled_agent",
        str(ROOT / "scripts/release/compiled_entry.py"),
    ]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        filter(None, (str(agent_module.parent), os.environ.get("PYTHONPATH", "")))
    )}
    _run(compiler, cwd=ROOT, env=env)
    dist = out / "compiled_entry.dist"
    if not (dist / "gms-runtime").is_file():
        raise RuntimeError(f"Nuitka standalone output missing binary: {dist}")
    return dist


def _write_agent_module(directory: Path) -> Path:
    """Embed the canonical public Agent package without shipping backend sources."""
    # The archive builder itself is stdlib-only. Load that implementation
    # without executing features.system's runtime initialization on the build host.
    build_package_bytes = runpy.run_path(str(ROOT / "features/system/agent_package/builder.py"))["build_package_bytes"]
    declaration = (ROOT / "agent/gms-remote-test/package.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version: (\d+\.\d+\.\d+)\s*$", declaration, re.M)
    if match is None:
        raise RuntimeError("Agent package declaration missing a valid version")
    archive = build_package_bytes(ROOT / "plugins/gms-remote-test", match[1])
    bootstrap = (ROOT / "agent/gms-remote-test/runtime/gms-agent").read_text(encoding="utf-8")
    if not bootstrap or "__GMS_AGENT_DEFAULT_SERVER__" not in bootstrap:
        raise RuntimeError("Agent bootstrap missing server placeholder")
    directory.mkdir(parents=True, exist_ok=True)
    module = directory / "_gms_bundled_agent.py"
    module.write_text(
        "import base64\n"
        f"VERSION = {match[1]!r}\n"
        f"ARCHIVE = base64.b64decode({base64.b64encode(archive)!r})\n"
        f"BOOTSTRAP = {bootstrap!r}\n",
        encoding="utf-8",
    )
    return module


def _smoke_compiled_runtime(runtime: Path, temp_root: Path) -> None:
    # Boot only a DISPOSABLE copy; secrets and admin DB generated by init must
    # never be added to the signed release stage.
    smoke_root = temp_root / "runtime-smoke"
    shutil.copytree(runtime, smoke_root)
    home = temp_root / "smoke-home"
    home.mkdir(mode=0o700)
    # Never inherit developer production/Redmine/AI credentials into the
    # staging instance. Production smoke must exercise newly generated
    # identities, rather than silently succeeding with a real host's secrets.
    blocked = ("GMS_", "REDMINE_", "GERRIT_", "APE_", "KKAGENT_", "UBUNTU_",
               "OPENAI_", "ANTHROPIC_", "GROK_", "GOOGLE_")
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith(blocked)
    }
    env.update({
        "GMS_PROJECT_ROOT": str(smoke_root),
        "GMS_DATA_ROOT": str(smoke_root / "data"),
        "GMS_ENV": "production",
        "HOME": str(home),
    })
    binary = str(smoke_root / "gms-runtime")
    _run([binary, "init", "--host", "127.0.0.1", "--port", "5001",
          "--no-admin-prompt"], cwd=smoke_root, env=env)
    _run([binary, "smoke"], cwd=smoke_root, env=env)
    # Exercise the native-only payload independently of Controller assets.
    remote_root = temp_root / "remote-worker-smoke"
    for name, source in compiled_worker_members(runtime):
        target = remote_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    worker_env = {**env, "GMS_PROJECT_ROOT": str(remote_root),
                  "GMS_WORKER_CONFIG": str(smoke_root / "data/local-worker/config.json")}
    _run([str(remote_root / "gms-runtime"), "remote-worker", "--check"],
         cwd=remote_root, env=worker_env)


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
    from scripts.release.verify_binary_tree import inspect

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
        write_compiled_worker_manifest(runtime)
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
        _smoke_compiled_runtime(runtime, temp_root)
        # Inspect the untouched release after the throwaway production smoke.
        findings = inspect(stage)
        if findings:
            raise RuntimeError("release tree changed unexpectedly:\n" + "\n".join(findings[:50]))
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
