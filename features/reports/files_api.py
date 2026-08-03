import asyncio

from features.auth import (
    authentication_required,
    require_authenticated_user,
)
from features.users import get_client_display_id_from_request

from .access import (
    can_access_report,
    filter_accessible_reports,
    get_accessible_report_by_timestamp,
    report_request_user,
)
from .api_helpers import (
    APIRouter,
    JSONResponse,
    Query,
    Request,
    dependencies,
    error_response,
    logger,
    os,
    test_report_db,
)
from .display import (
    report_client_display_id,
    report_display_name,
    report_download_filename,
    tradefed_result_folder_name,
)
from .downloads import (
    create_local_report_bundle,
    create_remote_report_bundle,
    remove_report_bundle,
)


router = APIRouter()
REPORT_FILE_VIEW_MAX_BYTES = 1024 * 1024


def _is_path_under(path: str, root: str) -> bool:
    target = os.path.realpath(path)
    base = os.path.realpath(root)
    return target == base or target.startswith(base + os.sep)


def _is_registered_report_file_path(
    path: str,
    request: Request,
) -> bool:
    if not path:
        return False
    principal = require_authenticated_user(request)
    for report in test_report_db.get_reports(
        limit=500,
        owner_id=None if principal.role == "admin" else principal.id,
        include_all=principal.role == "admin",
    ):
        if not can_access_report(request, report):
            continue
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


def _decorate_report_for_client(
    report: dict,
    display_id: str = "",
) -> dict:
    """Return a browser-safe report record without host paths or owner keys."""

    item = dict(report)
    item["display_client_id"] = report_client_display_id(item, display_id)
    report_name = report_display_name(item)
    item["report_name"] = report_name
    # Older cluster records stored XML start_display ("Fri Jul ...") in
    # source_timestamp.  Derive the actual Tradefed folder without exposing
    # the controller artifact path to the browser.
    source_timestamp = tradefed_result_folder_name(
        report_name,
        item.get("source_timestamp"),
        item.get("result_dir"),
        item.get("timestamp"),
    )
    item["source_timestamp"] = source_timestamp
    for private_key in ("owner_id", "client_id", "result_dir"):
        item.pop(private_key, None)
    return item

# ==================== List Reports ====================

@router.get("/api/reports/list")
async def list_reports(
    request: Request,
    user_only: bool = False,
    worker_id: str = "",
    cluster_job_id: str = "",
    attempt_id: str = "",
    automation_run_id: str = "",
    report_timestamp: str = "",
):
    """Get test report list from database."""
    import time
    start_time = time.time()
    principal = report_request_user(request)
    if principal is None and authentication_required():
        principal = require_authenticated_user(request)

    try:
        display_id = (
            (
                str(getattr(principal, "display_name", "") or "")
                or principal.username
            )
            if principal
            else get_client_display_id_from_request(request)
        )

        db_start = time.time()
        exact_filter = any((cluster_job_id, attempt_id, automation_run_id, report_timestamp))
        owner_filter = (
            None
            if principal is None or (principal.role == "admin" and not user_only)
            else principal.id
        )
        if report_timestamp:
            exact = get_accessible_report_by_timestamp(
                test_report_db, request, report_timestamp
            )
            candidate_reports = [exact] if exact else []
        elif exact_filter:
            candidate_reports = test_report_db.get_reports(
                limit=500,
                owner_id=owner_filter,
                include_all=owner_filter is None,
            )
        else:
            candidate_reports = []
        if not exact_filter:
            candidate_reports = test_report_db.get_reports(
                limit=500,
                owner_id=owner_filter,
                include_all=owner_filter is None,
            )
        accessible = (
            candidate_reports
            if principal is None
            else filter_accessible_reports(request, candidate_reports)
        )
        if principal and principal.role == "admin" and user_only:
            accessible = [
                report for report in accessible
                if str(report.get("owner_id") or "") == principal.id
            ]
        all_reports = [
            report for report in accessible
            if (not cluster_job_id or report.get("cluster_job_id") == cluster_job_id)
            and (not attempt_id or report.get("attempt_id") == attempt_id)
            and (not automation_run_id or report.get("automation_run_id") == automation_run_id)
        ]
        if worker_id:
            all_reports = [
                report for report in all_reports
                if str(report.get("worker_id") or "") == worker_id
            ]
        all_reports = [
            _decorate_report_for_client(
                report,
                display_id
                if principal
                and str(report.get("owner_id") or "") == principal.id
                else "",
            )
            for report in all_reports[:30]
        ]
        db_time = (time.time() - db_start) * 1000

        total_time = (time.time() - start_time) * 1000
        logger.info(f"[API] /api/reports/list completed: {len(all_reports)} reports, DB: {db_time:.2f}ms, Total: {total_time:.2f}ms")

        return JSONResponse(content={"reports": all_reports, "worker_id": worker_id})
    except Exception as e:
        logger.error(f"Failed to get report list: {e}")
        return error_response("Failed to load reports", 500)


# ==================== Download Report ====================

@router.get("/api/reports/download")
async def download_report(
    request: Request,
    report_id: str = Query(None),
    report_timestamp: str = Query(None),
    download: bool = Query(False),
    file: str = Query(None),
    path: str = Query(None, include_in_schema=False),
):
    """Unified report interface: list files, download ZIP, or view file content."""
    require_authenticated_user(request)
    FileUtils = dependencies.file_utils
    try:
        if path:
            return error_response(
                "Absolute report paths are no longer accepted; use report_timestamp and file",
                410,
            )
        if report_id or report_timestamp:
            principal = require_authenticated_user(request)
            report = (
                test_report_db.get_report(
                    report_id,
                    owner_id=None if principal.role == "admin" else principal.id,
                    include_all=principal.role == "admin",
                )
                if report_id and hasattr(test_report_db, "get_report")
                else get_accessible_report_by_timestamp(
                    test_report_db, request, report_timestamp
                )
            )
            if not report:
                logger.error("[DOWNLOAD] Report not found")
                return error_response("Report not found", 404)
            if not can_access_report(request, report):
                return error_response("Report not found", 404)

            report_timestamp = str(report.get("timestamp") or report_timestamp or "")
            display_name = report_display_name(report)
            download_filename = report_download_filename(report)

            if download:
                bundle = await asyncio.to_thread(create_local_report_bundle, report)
                if bundle is None:
                    bundle = await create_remote_report_bundle(
                        report,
                        owner_id=principal.id,
                    )
                if bundle is None:
                    return error_response(
                        "Report results and logs directories were not found",
                        404,
                    )
                logger.info(
                    "[DOWNLOAD] Report bundle ready: report_id='%s', filename='%s', "
                    "sources=%s, files=%s",
                    report.get("report_id") or report_timestamp,
                    download_filename,
                    ",".join(bundle.sources),
                    bundle.file_count,
                )
                from fastapi.responses import FileResponse
                from starlette.background import BackgroundTask

                return FileResponse(
                    bundle.path,
                    media_type="application/zip",
                    filename=download_filename,
                    background=BackgroundTask(remove_report_bundle, bundle.path),
                )

            report_dir = report.get("result_dir")
            if not report_dir or not os.path.exists(report_dir):
                logger.error(f"[DOWNLOAD] Report directory not found: {report_dir}")
                return error_response(f"Report directory not found: {report_dir}", 404)

            android_suite_dir = os.path.dirname(os.path.dirname(report_dir))
            run_folder = tradefed_result_folder_name(
                display_name,
                report.get("source_timestamp"),
                report_timestamp,
            ) or report_timestamp
            logs_dir = os.path.join(android_suite_dir, "logs", run_folder)
            has_logs = os.path.exists(logs_dir)

            if file:
                relative = str(file or "").strip().replace("\\", "/")
                if not relative or relative.startswith("/") or ".." in relative.split("/"):
                    return error_response("Report file not found", 404)
                if relative.startswith("logs/"):
                    candidate = os.path.join(logs_dir, relative[5:])
                else:
                    candidate = os.path.join(report_dir, relative)
                if not _is_registered_report_file_path(candidate, request) or not os.path.isfile(candidate):
                    return error_response("Report file not found", 404)
                with open(candidate, "rb") as report_file:
                    raw_content = report_file.read(REPORT_FILE_VIEW_MAX_BYTES + 1)
                truncated = len(raw_content) > REPORT_FILE_VIEW_MAX_BYTES
                output = raw_content[:REPORT_FILE_VIEW_MAX_BYTES].decode(
                    "utf-8", errors="replace"
                )
                file_ext = os.path.splitext(candidate)[1].lower()
                content_type = (
                    "text/html" if file_ext in {".xml", ".html"}
                    else "application/json" if file_ext == ".json"
                    else "text/plain"
                )
                return JSONResponse(content={
                    "success": True,
                    "content": output,
                    "content_type": content_type,
                    "truncated": truncated,
                })

            logger.info(f"[DOWNLOAD] Get report file list: timestamp='{report_timestamp}'")
            if FileUtils is None:
                return error_response("Report file service is not configured", 500)
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
            safe_files = []
            for file_info in all_files:
                item = dict(file_info)
                item.pop("path", None)
                item["file"] = item.get("relative_path", "")
                safe_files.append(item)
            return JSONResponse(content={"success": True, "files": safe_files})
        else:
            return error_response("Please provide report_id or report_timestamp", 400)

    except Exception as e:
        logger.error(f"[DOWNLOAD] Request failed: {e}", exc_info=True)
        return error_response(str(e), 500)
