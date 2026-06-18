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


router = APIRouter()

# ==================== List Reports ====================

@router.get("/api/reports/list")
async def list_reports(request: Request, user_only: bool = False):
    """Get test report list from database."""
    import time
    start_time = time.time()

    try:
        client_id_filter = None
        if user_only:
            client_id = get_client_id_from_request(request)
            config = config_manager.load_config()
            configured_ip = config.get("client_ip", "")
            username = config.get("client_username", "unknown")

            if configured_ip and ("@127.0.0.1" in client_id or "@::1" in client_id or "@localhost" in client_id):
                client_id_filter = f"{username}@{configured_ip}"
            else:
                client_id_filter = client_id

        db_start = time.time()
        all_reports = test_report_db.get_reports(limit=30, user_only=client_id_filter)
        db_time = (time.time() - db_start) * 1000

        total_time = (time.time() - start_time) * 1000
        logger.info(f"[API] /api/reports/list completed: {len(all_reports)} reports, DB: {db_time:.2f}ms, Total: {total_time:.2f}ms")

        return JSONResponse(content={"reports": all_reports})
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
            config = config_manager.load_config()
            with dependencies.ssh_manager.optional_connection(config) as ssh:
                if not ssh:
                    return error_response("SSH connection failed", 500)

                cat_cmd = f"cat '{path}' 2>/dev/null"
                output, _error, _code = dependencies.ssh_manager.execute_command(
                    ssh,
                    cat_cmd,
                    timeout=30,
                )

                file_ext = os.path.splitext(path)[1].lower()
                if file_ext in [".xml", ".html"]:
                    content_type = "text/html"
                elif file_ext == ".json":
                    content_type = "application/json"
                else:
                    content_type = "text/plain"

                return JSONResponse(content={"success": True, "content": output, "content_type": content_type})
        else:
            return error_response("Please provide report_timestamp or path parameter", 400)

    except Exception as e:
        logger.error(f"[DOWNLOAD] Request failed: {e}", exc_info=True)
        return error_response(str(e), 500)
