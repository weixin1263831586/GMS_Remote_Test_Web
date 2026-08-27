"""Suite-facing worker actions: execution, export, and report import.

从 inventory.py 拆出（2026-08 审核第七节）：套件枚举/下载/解压、Tradefed 执行、
报告导入与导出打包。设备探测与烧写见 device_actions.py。
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import shutil
import stat
import subprocess
import tarfile
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import WorkerConfig, _is_production
from .suite_detection import suite_details


def execute_suite_action(config: WorkerConfig, payload: dict[str, Any],
                         progress_callback: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    action = payload.get("action")
    roots = [root.expanduser().resolve() for root in config.suite_roots if root.expanduser().exists()]
    if action == "list_archives":
        archives = []
        extensions = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2")
        for root in roots:
            for path in root.iterdir():
                if path.is_file() and path.name.lower().endswith(extensions):
                    stat = path.stat()
                    name = path.name
                    default = next((name[:-len(ext)] for ext in extensions if name.lower().endswith(ext)), path.stem)
                    archives.append({"name": name, "path": str(path), "size": stat.st_size,
                                     "modified": int(stat.st_mtime), "default_dir_name": default})
        return {"archives": sorted(archives, key=lambda item: item["modified"], reverse=True)}
    if action == "download_url":
        url = str(payload.get("url") or "").strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("suite URL must use http or https")
        if (
            _is_production()
            and parsed.scheme != "https"
        ):
            raise ValueError("production suite downloads require HTTPS")
        filename = str(payload.get("filename") or Path(urllib.parse.unquote(parsed.path)).name)
        if (not filename or Path(filename).name != filename
                or any(ord(character) < 32 for character in filename)):
            raise ValueError("suite URL has an invalid filename")
        target_root = roots[0] if roots else None
        if target_root is None:
            raise ValueError("no configured suite root exists")
        destination = target_root / filename
        temporary = target_root / f".{filename}.part"
        max_bytes = int(os.getenv("GMS_WORKER_SUITE_DOWNLOAD_MAX_BYTES", str(80 * 1024 ** 3)))
        downloaded = 0
        last_reported = 0
        try:
            headers = {"User-Agent": "GMS-Worker/0.1"}
            ssl_context = None
            controller = urllib.parse.urlparse(config.controller_url)
            default_ports = {"http": 80, "https": 443}
            is_controller_download_path = parsed.path.startswith(
                "/api/cluster/suite-library-download/"
            )
            if is_controller_download_path:
                # Browser-visible aliases are not part of the Worker's trust
                # configuration. Route reserved callback paths through the
                # configured Controller origin before attaching credentials.
                controller_path = controller.path.rstrip("/") + parsed.path
                url = urllib.parse.urlunparse((
                    controller.scheme,
                    controller.netloc,
                    controller_path,
                    "",
                    parsed.query,
                    "",
                ))
                parsed = urllib.parse.urlparse(url)
            controller_port = controller.port or default_ports.get(controller.scheme)
            download_port = parsed.port or default_ports.get(parsed.scheme)
            # Worker 凭据只能发往配置的 Controller 同源下载端点。路径本身
            # 不是可信边界，第三方站点可以构造完全相同的 URL path。
            is_controller_callback = (
                parsed.scheme == controller.scheme
                and parsed.hostname == controller.hostname
                and download_port == controller_port
                and is_controller_download_path
            )
            if parsed.scheme == "https":
                import ssl
                if is_controller_callback:
                    headers["Authorization"] = f"Bearer {config.token}"
                    ssl_context = ssl.create_default_context(
                        cafile=config.controller_ca or None
                    )
            elif is_controller_callback:
                headers["Authorization"] = f"Bearer {config.token}"
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=60, context=ssl_context) as response, temporary.open("wb") as output:
                try:
                    expected_bytes = int((getattr(response, "headers", None) or {}).get("Content-Length") or 0)
                except (AttributeError, TypeError, ValueError):
                    expected_bytes = 0
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    downloaded += len(block)
                    if downloaded > max_bytes:
                        raise ValueError("suite download exceeds configured size limit")
                    output.write(block)
                    if progress_callback and downloaded - last_reported >= 16 * 1024 * 1024:
                        headers = getattr(response, "headers", {})
                        total = int(payload.get("size_bytes") or headers.get("Content-Length") or 0)
                        progress_callback({"downloaded_bytes": downloaded, "total_bytes": total})
                        last_reported = downloaded
                # 与 Content-Length 比对，避免把截断的压缩包当成功保存。
                if expected_bytes and downloaded != expected_bytes:
                    raise ValueError(
                        f"suite download incomplete: received {downloaded} of {expected_bytes} bytes"
                    )
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return {"archive_path": str(destination), "file_size": downloaded,
                "message": f"downloaded {filename}"}
    if action == "extract":
        archive = Path(str(payload.get("archive_path") or "")).expanduser().resolve()
        if not archive.is_file() or not any(archive.is_relative_to(root) for root in roots):
            raise ValueError("archive is outside configured suite roots")
        folder = str(payload.get("target_dir_name") or "")
        if not re.fullmatch(r"[A-Za-z0-9._+-]+", folder):
            raise ValueError("invalid extraction folder")
        root = next(root for root in roots if archive.is_relative_to(root))
        destination = (root / folder).resolve()
        if not destination.is_relative_to(root) or destination.exists():
            raise ValueError("extraction destination already exists or is invalid")
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as bundle:
                members = bundle.infolist()
                if any(not (destination / item.filename).resolve().is_relative_to(destination) for item in members):
                    raise ValueError("archive contains an unsafe path")
                # Every archive path is resolved and confined above.
                bundle.extractall(destination)  # nosec B202
                # 恢复压缩包中的 Unix 权限，确保测试启动脚本可执行。
                for item in members:
                    mode = (item.external_attr >> 16) & 0o777
                    target = (destination / item.filename).resolve()
                    if mode and target.exists():
                        target.chmod(mode)
        elif tarfile.is_tarfile(archive):
            with tarfile.open(archive) as bundle:
                members = bundle.getmembers()
                if any(not (destination / item.name).resolve().is_relative_to(destination)
                           or not (item.isfile() or item.isdir()) for item in members):
                    raise ValueError("archive contains an unsafe path or link")
                # Paths and member types are prevalidated above; the data
                # filter adds stdlib ownership/mode/link protections.
                bundle.extractall(destination, members=members, filter="data")
        else:
            if archive.name.lower().endswith(".zip"):
                # is_zipfile 失败多为传输截断：ZIP 缺少结尾 EOCD 记录。
                raise ValueError(
                    f"suite archive {archive.name} is incomplete or corrupted "
                    "(missing ZIP end-of-central-directory record); "
                    "delete and re-download the archive"
                )
            raise ValueError("unsupported suite archive format")
        return {"extracted_path": str(destination), "message": f"extracted {archive.name}"}
    suite_path = Path(str(payload.get("suite_path") or "")).expanduser().resolve()
    suite_root = suite_path.parent if suite_path.name == "tools" else suite_path
    if not any(root.exists() and suite_root.is_relative_to(root.resolve())
               for root in config.suite_roots):
        raise ValueError("suite path is outside configured roots")
    if action == "list_results":
        tools_dir = suite_path if suite_path.name == "tools" else suite_root / "tools"
        launchers = sorted(
            path for path in tools_dir.glob("*-tradefed")
            if path.is_file() and os.access(path, os.X_OK)
        )
        if not launchers:
            raise ValueError("no executable tradefed launcher found in suite tools")
        try:
            completed = subprocess.run(
                [str(launchers[0])],
                input="list results\nexit\n",
                capture_output=True,
                text=True,
                cwd=tools_dir,
                timeout=90,
                check=False,
                env={**os.environ, "TERM": "dumb"},
            )
        except subprocess.TimeoutExpired as exc:
            parts = [exc.stdout or "", exc.stderr or ""]
            output = "\n".join(
                value.decode(errors="replace") if isinstance(value, bytes) else value
                for value in parts if value
            )
            if "Session" not in output:
                raise RuntimeError("tradefed list results timed out") from exc
            return {"raw_output": output, "exit_code": 0, "launcher": launchers[0].name}
        output = "\n".join(filter(None, [completed.stdout, completed.stderr]))
        if completed.returncode != 0 and "Session" not in output:
            raise RuntimeError(output[-4000:] or "tradefed list results failed")
        return {"raw_output": output, "exit_code": completed.returncode,
                "launcher": launchers[0].name}
    if action == "read_file":
        relative = Path(str(payload.get("path") or ""))
        target = (suite_root / relative).resolve()
        if not target.is_relative_to(suite_root) or not target.is_file():
            raise ValueError("invalid suite file")
        max_bytes = int(os.getenv("GMS_WORKER_SUITE_READ_MAX_BYTES", str(32 * 1024 ** 2)))
        if target.stat().st_size > max_bytes:
            raise ValueError("suite file exceeds inline transfer limit")
        return {"filename": target.name, "content_type": mimetypes.guess_type(target.name)[0]
                or "application/octet-stream",
                "content_base64": base64.b64encode(target.read_bytes()).decode("ascii")}
    if action == "list":
        relative = Path(str(payload.get("path") or ""))
        target = (suite_root / relative).resolve()
        if not target.is_relative_to(suite_root) or not target.is_dir():
            raise ValueError("invalid suite directory")
        items = []
        for entry in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            try:
                stat = entry.stat()
            except OSError:
                continue
            rel = str(entry.relative_to(suite_root))
            items.append({"name": entry.name, "path": rel,
                          "type": "directory" if entry.is_dir() else "file",
                          "size": 0 if entry.is_dir() else stat.st_size,
                          "modified": int(stat.st_mtime),
                          "is_apk": entry.suffix.lower() == ".apk",
                          "is_jar": entry.suffix.lower() == ".jar"})
        return {"suite_path": str(suite_path), "suite_root": str(suite_root),
                "path": "" if target == suite_root else str(target.relative_to(suite_root)),
                "items": items}
    if action == "search":
        query = str(payload.get("query") or "").lower()
        limit = max(1, min(200, int(payload.get("limit") or 30)))
        items = []
        for current, dirs, files in os.walk(suite_root):
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            for name in sorted(dirs) + sorted(files):
                if query not in name.lower():
                    continue
                entry = Path(current) / name
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                items.append({"name": name, "path": str(entry.relative_to(suite_root)),
                              "type": "directory" if entry.is_dir() else "file",
                              "size": 0 if entry.is_dir() else stat.st_size,
                              "modified": int(stat.st_mtime),
                              "is_apk": entry.suffix.lower() == ".apk",
                              "is_jar": entry.suffix.lower() == ".jar"})
                if len(items) >= limit:
                    return {"suite_path": str(suite_path), "suite_root": str(suite_root),
                            "query": payload.get("query", ""), "items": items, "count": len(items)}
        return {"suite_path": str(suite_path), "suite_root": str(suite_root),
                "query": payload.get("query", ""), "items": items, "count": len(items)}
    raise ValueError(f"unsupported suite action: {action}")


def prepare_suite_export(config: WorkerConfig, payload: dict[str, Any]) -> tuple[Path, bool]:
    suite_path = Path(str(payload.get("suite_path") or "")).expanduser().resolve()
    suite_root = suite_path.parent if suite_path.name == "tools" else suite_path
    roots = [root.expanduser().resolve() for root in config.suite_roots if root.expanduser().exists()]
    if not any(suite_root.is_relative_to(root) for root in roots):
        raise ValueError("suite path is outside configured roots")
    target = (suite_root / Path(str(payload.get("path") or ""))).resolve()
    if not target.is_relative_to(suite_root) or not target.exists():
        raise ValueError("invalid suite export path")
    directory = bool(payload.get("directory"))
    if directory != target.is_dir():
        raise ValueError("suite export type does not match target")
    if not directory:
        return target, False
    export_root = config.data_root / "exports"
    export_root.mkdir(parents=True, exist_ok=True)
    archive_base = export_root / f"{payload.get('transfer_id')}-{target.name}"
    archive = Path(shutil.make_archive(str(archive_base), "zip", target.parent, target.name))
    return archive, True


_REPORT_DIRECTORY_RE = re.compile(
    r"^\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}(?:\.\d+)?(?:_\d+)?$"
)


def import_suite_report(
    config: WorkerConfig,
    archive_path: Path,
    target_suite_path: str,
    report_name: str,
) -> dict[str, Any]:
    """Safely import one Tradefed result directory into a configured suite."""
    if not _REPORT_DIRECTORY_RE.fullmatch(report_name):
        raise ValueError("invalid Tradefed report directory name")

    suite_path = Path(target_suite_path).expanduser().resolve()
    suite_root = suite_path.parent if suite_path.name == "tools" else suite_path
    roots = [
        root.expanduser().resolve()
        for root in config.suite_roots
        if root.expanduser().exists()
    ]
    if not suite_root.is_dir() or not any(suite_root.is_relative_to(root) for root in roots):
        raise ValueError("target suite path is outside configured roots")

    archive = Path(archive_path).resolve()
    allowed_archive_root = (config.data_root / "report-copies").resolve()
    if not archive.is_file() or not archive.is_relative_to(allowed_archive_root):
        raise ValueError("report archive is outside Worker staging")
    if not zipfile.is_zipfile(archive):
        raise ValueError("report archive is not a ZIP file")

    results_dir = suite_root / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    destination = results_dir / report_name
    if destination.exists():
        raise ValueError(f"target report already exists: {report_name}")

    max_files = max(1, int(os.getenv("GMS_WORKER_REPORT_COPY_MAX_FILES", "200000")))
    max_bytes = max(
        1,
        int(os.getenv("GMS_WORKER_REPORT_COPY_MAX_BYTES", str(20 * 1024 ** 3))),
    )
    staging = results_dir / f".gms-report-copy-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    extracted_bytes = 0
    extracted_files = 0
    seen_paths: set[str] = set()
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if not members or len(members) > max_files:
                raise ValueError("report archive contains an invalid number of entries")
            declared_bytes = sum(max(0, member.file_size) for member in members)
            if declared_bytes > max_bytes:
                raise ValueError("report archive exceeds Worker extraction limit")

            for member in members:
                normalized_name = member.filename.replace("\\", "/").rstrip("/")
                parts = [part for part in normalized_name.split("/") if part]
                if (
                    not parts
                    or parts[0] != report_name
                    or normalized_name.startswith("/")
                    or any(part in {".", ".."} for part in parts)
                ):
                    raise ValueError("report archive contains an unsafe path")
                relative_name = "/".join(parts)
                if relative_name in seen_paths:
                    raise ValueError("report archive contains duplicate paths")
                seen_paths.add(relative_name)

                unix_mode = (member.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(unix_mode)
                if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise ValueError("report archive contains an unsupported entry")

                target = (staging / relative_name).resolve()
                if not target.is_relative_to(staging.resolve()):
                    raise ValueError("report archive contains an unsafe path")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("xb") as output:
                    while block := source.read(1024 * 1024):
                        extracted_bytes += len(block)
                        if extracted_bytes > max_bytes:
                            raise ValueError("report archive exceeds Worker extraction limit")
                        output.write(block)
                extracted_files += 1

        staged_report = staging / report_name
        if not staged_report.is_dir() or extracted_files == 0:
            raise ValueError("report archive does not contain a result directory")
        if destination.exists():
            raise ValueError(f"target report already exists: {report_name}")
        staged_report.rename(destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {
        "report_name": report_name,
        "destination": str(destination),
        "file_count": extracted_files,
        "size_bytes": extracted_bytes,
    }


def scan_suites(config: WorkerConfig) -> list[dict[str, Any]]:
    suites = []
    seen = set()
    names = {"cts-tradefed", "gts-tradefed", "vts-tradefed", "sts-tradefed",
             "cts-v-host-tradefed"}
    for root in config.suite_roots:
        if not root.exists():
            continue
        for current, dirs, files in os.walk(root):
            depth = len(Path(current).relative_to(root).parts)
            if depth > 5:
                dirs[:] = []
                continue
            for filename in names.intersection(files):
                executable = Path(current) / filename
                tools_path = str(executable.parent)
                if tools_path in seen:
                    continue
                seen.add(tools_path)
                suite_type, version = suite_details(executable)
                suites.append({
                    "suite_type": suite_type, "suite_version": version,
                    "suite_key": f"{suite_type}:{version or executable.parent.parent.name}",
                    "tools_path": tools_path, "checksum": "", "size_bytes": 0,
                    "available": os.access(executable, os.X_OK),
                })
    return suites


