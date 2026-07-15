import sqlite3

from features.auth import get_authenticated_user
from features.users import get_client_id_from_request
from foundation.config import settings

from .api_helpers import (
    AnalysisMode,
    APIRouter,
    File,
    Form,
    JSONResponse,
    Query,
    ReportAnalyzer,
    ReportDiagnosisRequest,
    Request,
    StackTraceUtils,
    UploadFile,
    _build_patch_draft,
    _ensure_uploaded_report_extension,
    _extract_class_names_from_text,
    _extract_failure_keywords,
    _get_knowledge_base,
    analyze_with_ai,
    asyncio,
    config_manager,
    dependencies,
    error_response,
    extract_report_name_from_upload,
    json,
    logger,
    os,
    re,
    shutil,
    success_response,
    tempfile,
    test_report_db,
    test_report_manager,
)
from .uploads import ReportUploadTooLargeError, stage_report_uploads
from .knowledge_ranking import android_version_from_request, rank_kb_hits


router = APIRouter()

_MAINLINE_DB_PATH = settings.data_root / 'mainline_known_issues.sqlite3'


def _query_mainline_exemptions(request: "ReportDiagnosisRequest") -> list[dict]:
    """Look up Mainline known-issue exemptions for the failing test.

    Read-only; degrades to an empty list when the DB is absent or the query
    fails (mirrors the graceful degradation of the other recall channels).
    """
    if not _MAINLINE_DB_PATH.exists():
        return []
    from features.system import (
        init_mainline_issues_db,
        query_mainline_exemption_match,
    )

    conn = sqlite3.connect(str(_MAINLINE_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        init_mainline_issues_db(conn)
        # test_type is validated against MAINLINE_ISSUE_TYPES inside the matcher
        # (VTS/unknown → ''), so no separate mapping layer is needed here.
        return query_mainline_exemption_match(
            conn,
            test_module=request.module,
            test_case=request.test_name,
            issue_type=request.test_type,
            limit=10,
        )
    finally:
        conn.close()


def _is_safe_report_delete_dir(result_dir: str) -> bool:
    """Return whether result_dir looks like a concrete test report directory."""
    if not result_dir:
        return False

    target = os.path.abspath(result_dir)
    protected_roots = {
        os.path.abspath(os.sep),
        os.path.abspath(os.path.expanduser("~")),
        os.path.abspath(os.getcwd()),
        os.path.abspath(str(settings.data_root)),
    }
    if target in protected_roots:
        return False

    if not os.path.isdir(target):
        return False

    marker_files = {
        "test_result.xml",
        "invocation_summary.txt",
        "test_result_failures_suite.html",
    }
    try:
        entries = set(os.listdir(target))
    except OSError:
        return False
    if entries.intersection(marker_files):
        return True
    try:
        return any(name.startswith("host_log_") and name.endswith(".txt") for name in entries)
    except OSError:
        return False

# ==================== Analyze Reports ====================

@router.post("/api/reports/analyze")
async def analyze_reports(
    request: Request,
    mode: AnalysisMode = Form(default=AnalysisMode.UPLOAD),
    report_timestamp: str | None = Form(default=None),
    test_name: str | None = Form(default=None),
    error_message: str | None = Form(default=None),
    stack_trace: str | None = Form(default=None),
    module: str | None = Form(default=None),
    class_names: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
    files: list[UploadFile] | None = File(default=None),
    files_array: list[UploadFile] | None = File(default=None, alias="files[]"),
):
    """Unified report analysis API."""
    try:
        if mode == AnalysisMode.SAVED:
            if not report_timestamp:
                return error_response("Missing report_timestamp", 400)

            report = test_report_db.get_report_by_timestamp(report_timestamp)
            if not report:
                return error_response("Report not found", 404)
            user = get_authenticated_user(request)
            if user and user.role != "admin" and report.get("client_id") != user.username:
                return error_response("Report not found", 404)

            result_dir = report.get("result_dir")
            if not result_dir:
                return error_response("Report directory not found", 404)

            result = await asyncio.to_thread(test_report_manager.analyze_report, report_timestamp)
            if not result:
                return error_response("Report analysis failed", 500)

            provenance_fields = (
                "report_id", "timestamp", "source_timestamp", "worker_id", "cluster_job_id",
                "attempt_id", "automation_run_id", "artifact_id", "artifact_ids",
                "build_id", "build_artifact_id", "gerrit_change_id", "gerrit_patchset",
                "redmine_issue_id", "source_type", "suite_path",
            )
            result["provenance"] = {
                field: report.get(field)
                for field in provenance_fields
                if report.get(field) not in (None, "", [])
            }
            result["report_id"] = report.get("report_id") or report_timestamp
            result["report_timestamp"] = report_timestamp
            result.setdefault("report_name", report_timestamp)

            return JSONResponse(content={"success": True, "data": result, "mode": "saved"})

        elif mode == AnalysisMode.AI:
            if not test_name:
                return error_response("Missing test_name", 400)

            parsed_class_names = []
            if class_names:
                try:
                    parsed_class_names = json.loads(class_names)
                except json.JSONDecodeError:
                    parsed_class_names = []

            try:
                ai_result = await asyncio.to_thread(analyze_with_ai, test_name, error_message or "", stack_trace or "", module or "", parsed_class_names)
            except Exception:
                ai_result = {"root_cause": "AI analysis unavailable", "analysis": "N/A"}

            return JSONResponse(content={"success": True, "data": ai_result, "mode": "ai"})

        else:  # upload mode
            all_files = []
            if file:
                all_files = [file]
            elif files_array:
                all_files = files_array
            elif files:
                all_files = files

            if not all_files or len(all_files) == 0:
                return error_response("No files uploaded", 400)

            if len(all_files) == 1 and all_files[0].filename == "":
                return error_response("Empty filename", 400)

            with tempfile.TemporaryDirectory() as temp_dir:
                if len(all_files) == 1:
                    uploaded_file = all_files[0]
                    try:
                        staged = await stage_report_uploads(
                            [uploaded_file], temp_dir, allow_nested=False
                        )
                    except ReportUploadTooLargeError as e:
                        return error_response(str(e), 413)
                    except ValueError as e:
                        return error_response(str(e), 400)
                    temp_file_path, bytes_written = staged[0]
                    temp_file_path, detected_filename = _ensure_uploaded_report_extension(
                        temp_file_path,
                        uploaded_file.filename or "uploaded_report",
                        uploaded_file.content_type or "",
                    )
                    logger.info(
                        "[Report Analysis] Uploaded report: filename=%s detected=%s content_type=%s size=%s",
                        uploaded_file.filename,
                        detected_filename,
                        uploaded_file.content_type,
                        bytes_written,
                    )

                    analyzer = ReportAnalyzer(temp_dir=temp_dir)
                    result = await asyncio.to_thread(analyzer.analyze_file, temp_file_path)

                    if result:
                        result["report_name"] = extract_report_name_from_upload([uploaded_file])
                        return JSONResponse(content={"success": True, "data": result, "mode": "upload"})

                    fallback_result = await asyncio.to_thread(analyzer.analyze_log_dir, temp_dir)
                    if fallback_result:
                        fallback_result["report_type"] = "log"
                        fallback_result["report_name"] = extract_report_name_from_upload([uploaded_file])
                        return JSONResponse(content={"success": True, "data": fallback_result, "mode": "upload"})

                    logger.warning(
                        "[Report Analysis] Cannot parse uploaded report: filename=%s detected=%s content_type=%s size=%s",
                        uploaded_file.filename,
                        detected_filename,
                        uploaded_file.content_type,
                        bytes_written,
                    )
                    return JSONResponse(
                        status_code=400,
                        content={
                            "success": False,
                            "error": "Cannot parse report file",
                            "message": f"无法解析上传文件：{uploaded_file.filename or 'unknown'}。请确认文件是 test_result.xml、包含 test_result.xml/host_log 的 ZIP/TAR/RAR 报告包，或选择报告文件夹上传。",
                        },
                    )

                else:
                    try:
                        await stage_report_uploads(
                            all_files, temp_dir, allow_nested=True
                        )
                    except ReportUploadTooLargeError as exc:
                        return error_response(str(exc), 413)
                    except ValueError as exc:
                        return error_response(str(exc), 400)

                    analyzer = ReportAnalyzer(temp_dir=temp_dir)
                    xml_path = await asyncio.to_thread(analyzer.file_handler.find_xml_file)

                    if not xml_path:
                        logger.info("test_result.xml not found, trying HostLog analyzer")
                        result = await asyncio.to_thread(analyzer.analyze_log_dir, temp_dir)
                        if not result:
                            return JSONResponse(status_code=400, content={"success": False, "error": "No test_result.xml or host_log found", "message": f"Received {len(all_files)} files but no valid report files"})

                        result["report_type"] = "log"
                        result["report_name"] = extract_report_name_from_upload(all_files)
                        return JSONResponse(content={"success": True, "data": result, "mode": "upload"})

                    result = await asyncio.to_thread(analyzer.analyze_file, xml_path)
                    if result:
                        result["report_name"] = extract_report_name_from_upload(all_files)
                        return JSONResponse(content={"success": True, "data": result, "mode": "upload"})
                    else:
                        return JSONResponse(status_code=400, content={"success": False, "error": "Cannot parse report file", "message": "test_result.xml invalid or corrupted"})

    except Exception as e:
        logger.error(f"Report analysis failed: {e}")
        return error_response("Report analysis failed", 500, message=str(e))


# ==================== Analyze Suite Log Directory ====================

def _resolve_suite_log_dir(suite_path: str, rel_path: str, config) -> tuple[str, str | None]:
    """Resolve a browsed suite-relative path to an absolute log directory.

    Mirrors the path semantics of ``GET /api/test/suites/files`` so the
    "报告分析" button on a logs subfolder analyzes the exact folder the user
    sees. Returns ``(abs_path, error_message)``; on success error_message is
    None. Enforces that the resolved path stays inside the configured suites
    root (no ``..`` escape), so a malicious ``path`` cannot point elsewhere.
    """
    raw = (suite_path or "").replace("\\", "/").strip().rstrip("/")
    if not raw or not raw.startswith("/"):
        return "", "无效的测试套件路径"

    # suite_path points at the .../tools dir; the suite root is its parent.
    suite_root = raw[:-len("/tools")] if raw.endswith("/tools") else raw

    rel = (rel_path or "").replace("\\", "/").strip().strip("/")
    parts = [p for p in rel.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        return "", "非法路径"

    base = (config.get("suites_path") or "").replace("\\", "/").strip().rstrip("/")
    if base and not (suite_root == base or suite_root.startswith(base + "/")):
        return "", "测试套件不在配置的套件目录内"

    abs_path = suite_root if not parts else f"{suite_root}/{'/'.join(parts)}"
    return abs_path, None


def _suite_result_dir_for_log_dir(suite_path: str, log_dir: str) -> str | None:
    suite_root = (suite_path or "").replace("\\", "/").strip().rstrip("/")
    if suite_root.endswith("/tools"):
        suite_root = suite_root[:-len("/tools")]
    if not suite_root:
        return None
    normalized = os.path.normpath(log_dir)
    parts = normalized.split(os.sep)
    if "logs" not in parts:
        return None
    logs_index = parts.index("logs")
    if len(parts) <= logs_index + 1:
        return None
    timestamp = parts[logs_index + 1]
    if not re.match(r"^\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}(?:\.\d+)?(?:_\d+)?$", timestamp):
        return None
    candidate = os.path.join(suite_root, "results", timestamp)
    xml_path = os.path.join(candidate, "test_result.xml")
    return candidate if os.path.isfile(xml_path) else None


@router.post("/api/reports/analyze-log-dir")
async def analyze_suite_log_dir(
    suite_path: str = Form(...),
    path: str = Form(default=""),
):
    """Analyze a test-run log folder browsed from the suite browser.

    Triggered by the per-folder "报告分析" button inside a ``.../logs`` dir.
    Resolves ``suite_path``+``path`` to an absolute directory (same semantics
    as the suite file browser) and runs the host-log analyzer over it. The
    analyzer walks the tree recursively, so pointing it at a
    ``logs/<timestamp>`` folder finds the nested ``inv_*/host_log_*.txt``.

    The suite directory must be readable from the web host itself. When the
    configured test host is remote and the path is not mounted locally, the
    caller gets a clear error instead of a silent failure.
    """
    config = config_manager.load_config()
    abs_path, err = _resolve_suite_log_dir(suite_path, path, config)
    if err:
        return error_response(err, 400)

    if not os.path.isdir(abs_path):
        return error_response(
            "日志目录在 Web 服务器本地不可访问",
            400,
            message=(
                f"无法访问 {abs_path}：该路径位于远程测试主机，Web 服务器无法直接读取。"
                "请将报告文件夹打包后通过上传模式分析。"
            ),
        )

    try:
        analyzer = ReportAnalyzer()
        result_dir = _suite_result_dir_for_log_dir(suite_path, abs_path)
        if result_dir:
            result = await asyncio.to_thread(
                analyzer.analyze_file,
                os.path.join(result_dir, "test_result.xml"),
            )
            if result:
                result["report_type"] = "xml"
                result["report_name"] = os.path.basename(result_dir.rstrip("/")) or "suite result"
                result["result_dir"] = result_dir
        else:
            result = await asyncio.to_thread(analyzer.analyze_log_dir, abs_path)
    except Exception as e:
        logger.error(f"[Report Analysis] Suite log-dir analysis failed: {e}")
        return error_response("Report analysis failed", 500, message=str(e))

    if not result:
        return error_response(
            "未找到可分析的日志",
            400,
            message=f"在 {abs_path} 下未找到 host_log_*.txt，无法解析报告。",
        )

    result.setdefault("report_type", "log")
    result.setdefault("report_name", os.path.basename(abs_path.rstrip("/")) or "suite log")
    return JSONResponse(content={"success": True, "data": result, "mode": "suite_log_dir"})


# ==================== Diagnose Report ====================

@router.post("/api/reports/diagnose")
async def diagnose_report_failure(request: ReportDiagnosisRequest, http_request: Request):
    """Diagnose one report failure and locate matching suite APK/JAR source."""
    try:
        failure_location = StackTraceUtils.extract_failure_location(request.stack_trace or "", request.test_name)
        class_names = [c for c in (request.class_names or []) if c]
        if not class_names:
            class_names = _extract_class_names_from_text(request.test_name, request.error_message)

        keywords = _extract_failure_keywords(request.test_name, request.error_message, request.stack_trace, request.module, class_names)

        # Resolve suite target inline (simplified from monolith)
        async def _resolve_suite_target():
            try:
                if dependencies.resolve_suite_target is None:
                    raise RuntimeError("Suite target resolver is not configured")
                return await asyncio.to_thread(
                    dependencies.resolve_suite_target,
                    config_manager.load_config(),
                    test_type=request.test_type,
                    suite_version=request.suite_version,
                    module=request.module,
                    test_name=request.test_name,
                    class_names=class_names,
                    source_path=request.source_path,
                )
            except Exception as target_error:
                logger.warning(f"Suite target resolution failed: {target_error}")
                if dependencies.make_empty_suite_target is None:
                    raise RuntimeError(
                        "Empty suite target factory is not configured"
                    ) from target_error
                return dependencies.make_empty_suite_target(
                    test_type=request.test_type,
                    suite_version=request.suite_version,
                    test_name=request.test_name,
                    class_names=class_names,
                    match_notes=[f"Resolution failed: {target_error}"],
                )

        async def _run_ai_analysis():
            try:
                return await asyncio.to_thread(analyze_with_ai, request.test_name, request.error_message or "", request.stack_trace or "", request.module or "", class_names)
            except Exception:
                return {"root_cause": "AI analysis unavailable", "analysis": "N/A"}

        async def _search_knowledge_base():
            try:
                kb = _get_knowledge_base(http_request)
                kb_query = " ".join(keywords[:5]) or request.test_name or request.error_message[:80]
                if not kb or not kb_query.strip():
                    return []
                # Relevance dimensions extracted from the failure under diagnosis,
                # used to filter out broad "same-module, wrong-platform/wrong-case"
                # FTS noise (e.g. an RK3399 Android15 ticket surfacing for an
                # RK3576 Android16 SearchView failure).
                probe = {
                    "test_name": request.test_name or "",
                    "module": request.module or "",
                    "android_version": android_version_from_request(request),
                }

                def _gather() -> list[dict]:
                    # Two recall channels — the synced issue store (largest, most
                    # current) and the curated case_facts — each adapt to the same
                    # canonical hit shape via _adapt_hit, so dedup + scoring stay
                    # in one place.
                    merged: list[dict] = []
                    seen: set[int] = set()

                    def _adapt(row: dict, source: str, *, issue_store: bool = False) -> None:
                        iid = int(row.get("issue_id") or 0)
                        if not iid or iid in seen:
                            return
                        seen.add(iid)
                        if issue_store:
                            module = row.get("category") or row.get("module") or ""
                            root_cause = row.get("error_analysis") or ""
                            error_signature = ""
                            solution = (row.get("solution") or "")[:600]
                        else:
                            module = row.get("module") or ""
                            root_cause = row.get("root_cause") or ""
                            error_signature = row.get("error_signature") or ""
                            solution = row.get("solution") or row.get("reply_template") or ""
                        merged.append({
                            "id": iid,
                            "subject": row.get("subject") or "",
                            "status_name": row.get("status_name") or "",
                            "module": module,
                            "chip_platform": row.get("chip_platform") or row.get("soc_platform") or "",
                            "android_version": row.get("android_version") or "",
                            "error_signature": error_signature,
                            "root_cause": root_cause,
                            "solution_summary": solution,
                            "source": source,
                        })

                    try:
                        repo = getattr(kb, "issue_repository", None)
                        if repo is not None:
                            for issue in repo.search_similar(kb_query, 0, 20):
                                _adapt(issue, "issue_store", issue_store=True)
                    except Exception as exc:
                        logger.debug(f"Issue-store KB recall skipped: {exc}")
                    try:
                        for s in kb.search_similar(kb_query, limit=20):
                            _adapt(s, "case_facts")
                    except Exception as exc:
                        logger.debug(f"Case-facts KB recall skipped: {exc}")
                    return rank_kb_hits(merged, probe)

                return await asyncio.to_thread(_gather)
            except Exception as kb_error:
                logger.warning(f"Knowledge base search failed: {kb_error}")
            return []

        async def _search_mainline_exemptions():
            """Recall Google Mainline known-issue exemptions for this failure."""
            try:
                return await asyncio.to_thread(_query_mainline_exemptions, request)
            except Exception as exc:
                logger.debug(f"Mainline exemption lookup skipped: {exc}")
                return []

        suite_target, ai_result, kb_results, mainline_exemptions = await asyncio.gather(
            _resolve_suite_target(),
            _run_ai_analysis(),
            _search_knowledge_base(),
            _search_mainline_exemptions(),
        )

        source_search_results = []
        analyzer = ReportAnalyzer()
        search_terms = list(class_names[:3])
        if failure_location and failure_location.get("file_name"):
            search_terms.append(failure_location["file_name"])
        search_terms.extend(keywords[:3])

        source_search_coros = [
            asyncio.to_thread(analyzer.rk_codesearch, term, failure_location, 5)
            for term in search_terms if term
        ]
        if source_search_coros:
            seen_paths = set()
            for hits in await asyncio.gather(*source_search_coros, return_exceptions=True):
                if isinstance(hits, Exception):
                    logger.warning(f"Source search failed: {hits}")
                    continue
                for hit in hits or []:
                    dedup_key = f"{hit.get('path', '')}:{hit.get('line', '')}"
                    if dedup_key not in seen_paths:
                        seen_paths.add(dedup_key)
                        source_search_results.append(hit)

        diagnosis = {
            "test_name": request.test_name,
            "error_message": request.error_message,
            "stack_trace": request.stack_trace,
            "module": request.module,
            "report_name": request.report_name,
            "failure_index": request.failure_index,
            "source_path": request.source_path,
            "source_attached": bool(request.source_code),
            "failure_location": failure_location,
            "class_names": class_names,
            "keywords": keywords,
            "ai_result": ai_result,
            "source_search_results": source_search_results[:10],
            "knowledge_base_results": kb_results[:8],
            "suite_target": suite_target,
            "mainline_exemptions": mainline_exemptions,
            "mainline_exempt": bool(mainline_exemptions),
        }
        diagnosis["summary"] = ai_result.get("root_cause") or ai_result.get("analysis") or "Diagnosis orchestration complete"
        diagnosis["patch_draft"] = _build_patch_draft(diagnosis)

        return success_response(diagnosis, message="Diagnosis complete")
    except Exception as e:
        logger.error(f"Report diagnosis failed: {e}", exc_info=True)
        return error_response("Report diagnosis failed", status_code=500, detail=str(e))


# ==================== Delete Report ====================

@router.delete("/api/reports/delete")
async def delete_report(request: Request, timestamp: str = Query(...)):
    """Delete test report (owner or admin only)."""
    try:
        client_id = get_client_id_from_request(request)
        user = get_authenticated_user(request)
        report = test_report_db.get_report_by_timestamp(timestamp)

        if not report:
            return error_response("Report not found", 404)

        report_client_id = report.get("client_id")
        is_admin = bool(user and getattr(user, "role", None) == "admin")
        if report_client_id != client_id and not is_admin:
            logger.warning(f"[DELETE] Permission denied: {client_id} tried to delete {report_client_id}'s report")
            return error_response("No permission to delete this report", 403)

        result_dir = report.get("result_dir")
        if result_dir and os.path.exists(result_dir):
            if not _is_safe_report_delete_dir(result_dir):
                logger.warning(f"[DELETE] Refusing unsafe report directory deletion: {result_dir}")
                return error_response("Unsafe report directory, refusing to delete files", 400)
            try:
                shutil.rmtree(result_dir)
                logger.info(f"Deleted report directory: {result_dir}")
            except Exception as e:
                logger.error(f"Failed to delete report directory: {e}")
                return error_response(f"Failed to delete directory: {e!s}", 500)

        success = test_report_db.delete_report(timestamp)
        if success:
            return success_response(message="Report deleted")
        else:
            return error_response("Failed to delete DB record", 500)

    except Exception as e:
        logger.error(f"Error deleting report: {e}")
        return error_response(str(e), 500)


# ==================== Knowledge Base ====================

@router.get("/api/knowledgebase/search")
async def knowledgebase_search(request: Request, query: str = Query(..., min_length=1, max_length=256), limit: int = Query(8, ge=1, le=20)):
    """Search the local Redmine-derived GMS knowledge base."""
    try:
        kb = _get_knowledge_base(request)
        if not kb:
            return success_response({"query": query.strip(), "results": [], "count": 0})
        results = await asyncio.to_thread(kb.search_similar, query.strip(), limit)
        return success_response({"query": query.strip(), "results": results, "count": len(results)})
    except Exception as e:
        logger.error(f"Knowledge base search failed: {e}", exc_info=True)
        return error_response(str(e), status_code=500)


@router.get("/api/knowledgebase/stats")
async def knowledgebase_stats(request: Request):
    """Return local Redmine-derived GMS knowledge base stats."""
    try:
        kb = _get_knowledge_base(request)
        if not kb:
            return success_response({"stats": {"total": 0, "mature_cases": 0}})
        data = await asyncio.to_thread(lambda: kb.list_case_facts(limit=1))
        mature = await asyncio.to_thread(lambda: kb.list_mature_cases(limit=1))
        stats = {"total": data.get("total", 0), "mature_cases": mature.get("total", 0)}
        return success_response({"stats": stats})
    except Exception as e:
        logger.error(f"Knowledge base stats failed: {e}", exc_info=True)
        return error_response(str(e), status_code=500)

