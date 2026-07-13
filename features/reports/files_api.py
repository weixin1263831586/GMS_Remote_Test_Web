import asyncio
import shlex

from .api_helpers import (
    APIRouter,
    JSONResponse,
    Query,
    Request,
    config_manager,
    dependencies,
    error_response,
    get_client_id_from_request,
    logger,
    os,
    test_report_db,
)


try:
    from features.users import get_client_display_id_from_request as _user_display_id_from_request
    from features.users import get_client_id_from_request as _user_id_from_request
except Exception:  # pragma: no cover - import fallback for isolated tests
    _user_display_id_from_request = None
    _user_id_from_request = None


router = APIRouter()
REPORT_FILE_VIEW_MAX_BYTES = 1024 * 1024


def _is_path_under(path: str, root: str) -> bool:
    target = os.path.abspath(path)
    base = os.path.abspath(root)
    return target == base or target.startswith(base + os.sep)


def _is_registered_report_file_path(path: str) -> bool:
    if not path:
        return False
    for report in test_report_db.get_reports(limit=500):
        result_dir = report.get("result_dir")
        if result_dir and _is_path_under(path, result_dir):
            return True
        timestamp = report.get("timestamp", "")
        if result_dir and timestamp:
            android_suite_dir = os.path.dirname(os.path.dirname(result_dir))
            logs_dir = os.path.join(android_suite_dir, "logs", timestamp)
            if _is_path_under(path, logs_dir):
                return True
    return False


def _client_identity_aliases(request: Request) -> tuple[str, set[str]]:
    display_id = ""
    aliases: set[str] = set()
    try:
        if _user_id_from_request:
            aliases.add(str(_user_id_from_request(request) or "").strip())
    except Exception:
        pass
    try:
        display_id = str(_user_display_id_from_request(request) or "").strip() if _user_display_id_from_request else ""
        aliases.add(display_id)
    except Exception:
        pass
    try:
        legacy_id = str(get_client_id_from_request(request) or "").strip()
        aliases.add(legacy_id)
    except Exception:
        pass
    try:
        config = config_manager.load_config()
        configured_ip = str(config.get("client_ip") or "").strip()
        username = str(config.get("client_username") or "").strip()
        if configured_ip and username:
            aliases.add(f"{username}@{configured_ip}")
            display_id = display_id or f"{username}@{configured_ip}"
    except Exception:
        pass
    return display_id, {item for item in aliases if item}


def _decorate_report_for_client(report: dict, display_id: str, aliases: set[str]) -> dict:
    item = dict(report)
    client_id = str(item.get("client_id") or "").strip()
    stored_display = str(item.get("display_client_id") or item.get("client_name") or "").strip()
    if stored_display:
        item["display_client_id"] = stored_display
    elif display_id and client_id in aliases:
        item["display_client_id"] = display_id
    elif item.get("user") and "@" in client_id:
        item["display_client_id"] = client_id
    return item


def _report_matches_aliases(report: dict, aliases: set[str]) -> bool:
    values = {
        str(report.get("client_id") or "").strip(),
        str(report.get("display_client_id") or "").strip(),
        str(report.get("client_name") or "").strip(),
    }
    return bool(aliases.intersection({item for item in values if item}))

# ==================== List Reports ====================

@router.get("/api/reports/list")
async def list_reports(request: Request, user_only: bool = False, worker_id: str = ""):
    """Get test report list from database."""
    import time
    start_time = time.time()

    try:
        display_id, aliases = _client_identity_aliases(request)
        if user_only:
            client_id_filter = next(iter(aliases), None)
        else:
            client_id_filter = None

        db_start = time.time()
        if user_only and not aliases:
            all_reports = []
        elif user_only:
            candidate_reports = test_report_db.get_reports(limit=200)
            all_reports = [
                _decorate_report_for_client(report, display_id, aliases)
                for report in candidate_reports
                if _report_matches_aliases(report, aliases)
            ][:30]
        else:
            all_reports = [
                _decorate_report_for_client(report, display_id, aliases)
                for report in test_report_db.get_reports(limit=30, user_only=client_id_filter)
            ]
        if worker_id:
            all_reports = [report for report in all_reports if
                           (report.get("worker_id") or "worker-local") == worker_id]
        db_time = (time.time() - db_start) * 1000

        total_time = (time.time() - start_time) * 1000
        logger.info(f"[API] /api/reports/list completed: {len(all_reports)} reports, DB: {db_time:.2f}ms, Total: {total_time:.2f}ms")

        return JSONResponse(content={"reports": all_reports, "worker_id": worker_id})
    except Exception as e:
        logger.error(f"Failed to get report list: {e}")
        return JSONResponse(content={"reports": []})


# ==================== Download Report ====================

@router.get("/api/reports/download")
async def download_report(
    request: Request,
    report_timestamp: str = Query(None),
    download: bool = Query(False),
    path: str = Query(None),
):
    """Unified report interface: list files, download ZIP, or view file content."""
    FileUtils = dependencies.file_utils
    if FileUtils is None:
        return error_response("Report file service is not configured", 500)

    try:
        if report_timestamp:
            report = test_report_db.get_report_by_timestamp(report_timestamp)
            if not report:
                logger.error(f"[DOWNLOAD] Report not found: {report_timestamp}")
                return error_response(f"Report not found: {report_timestamp}", 404)

            report_dir = report.get("result_dir")
            if not report_dir or not os.path.exists(report_dir):
                logger.error(f"[DOWNLOAD] Report directory not found: {report_dir}")
                return error_response(f"Report directory not found: {report_dir}", 404)

            android_suite_dir = os.path.dirname(os.path.dirname(report_dir))
            logs_dir = os.path.join(android_suite_dir, "logs", report_timestamp)
            has_logs = os.path.exists(logs_dir)

            if download:
                logger.info(f"[DOWNLOAD] Download report ZIP: timestamp='{report_timestamp}'")
                dir_mapping = {report_dir: ""}
                if has_logs:
                    dir_mapping[logs_dir] = "logs"

                result = FileUtils.create_zip_from_multiple_directories(dir_mapping, zip_filename=f"{report_timestamp}.zip")
                if result is None:
                    logger.warning("[DOWNLOAD] No files found")
                    return error_response("No files found", 500)

                zip_data, file_count = result
                logger.info(f"[DOWNLOAD] ZIP created: {report_timestamp}.zip, {file_count} files")

                from fastapi.responses import Response
                return Response(
                    content=zip_data,
                    media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{report_timestamp}.zip"'},
                )

            logger.info(f"[DOWNLOAD] Get report file list: timestamp='{report_timestamp}'")
            all_files = []
            result_files = FileUtils.list_directory_files(report_dir, max_files=100, relative_to=report_dir)
            all_files.extend(result_files)

            log_files = []
            if has_logs:
                log_files = FileUtils.list_directory_files(logs_dir, max_files=100, relative_to=logs_dir)
                for file_info in log_files:
                    file_info["relative_path"] = os.path.join("logs", file_info["relative_path"])
                all_files.extend(log_files)

            logger.info(f"[DOWNLOAD] Found {len(all_files)} files (results: {len(result_files)}, logs: {len(log_files) if has_logs else 0})")
            return JSONResponse(content={"success": True, "files": all_files})

        elif path:
            logger.info(f"[DOWNLOAD] View file content: path='{path}'")
            if not _is_registered_report_file_path(path):
                return error_response("File path is not part of a registered report", 403)
            config = config_manager.load_config()
            async with dependencies.ssh_manager.async_optional_connection(config) as ssh:
                if not ssh:
                    return error_response("SSH connection failed", 500)

                cat_cmd = f"head -c {REPORT_FILE_VIEW_MAX_BYTES + 1} -- {shlex.quote(path)} 2>/dev/null"
                output, _error, _code = await asyncio.to_thread(
                    dependencies.ssh_manager.execute_command,
                    ssh,
                    cat_cmd,
                    timeout=30,
                )
                truncated = len(output.encode("utf-8", errors="ignore")) > REPORT_FILE_VIEW_MAX_BYTES
                if truncated:
                    output = output[:REPORT_FILE_VIEW_MAX_BYTES]

                file_ext = os.path.splitext(path)[1].lower()
                if file_ext in [".xml", ".html"]:
                    content_type = "text/html"
                elif file_ext == ".json":
                    content_type = "application/json"
                else:
                    content_type = "text/plain"

                return JSONResponse(content={"success": True, "content": output, "content_type": content_type, "truncated": truncated})
        else:
            return error_response("Please provide report_timestamp or path parameter", 400)

    except Exception as e:
        logger.error(f"[DOWNLOAD] Request failed: {e}", exc_info=True)
        return error_response(str(e), 500)
