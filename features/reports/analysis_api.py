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
    get_client_id_from_request,
    json,
    logger,
    os,
    safe_upload_target_path,
    save_upload_to_path,
    shutil,
    success_response,
    tempfile,
    test_report_db,
    test_report_manager,
)


router = APIRouter()

# ==================== Analyze Reports ====================

@router.post("/api/reports/analyze")
async def analyze_reports(
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

            result_dir = report.get("result_dir")
            if not result_dir:
                return error_response("Report directory not found", 404)

            result = await asyncio.to_thread(test_report_manager.analyze_report, report_timestamp)
            if not result:
                return error_response("Report analysis failed", 500)

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
                        temp_file_path = safe_upload_target_path(temp_dir, uploaded_file.filename, allow_nested=False)
                    except ValueError as e:
                        return error_response(str(e), 400)

                    bytes_written = await save_upload_to_path(uploaded_file, temp_file_path)
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
                    for uploaded_file in all_files:
                        if uploaded_file.filename:
                            try:
                                file_path = safe_upload_target_path(temp_dir, uploaded_file.filename, allow_nested=True)
                            except ValueError as e:
                                return error_response(str(e), 400)
                            await save_upload_to_path(uploaded_file, file_path)

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


# ==================== Diagnose Report ====================

@router.post("/api/reports/diagnose")
async def diagnose_report_failure(request: ReportDiagnosisRequest):
    """Diagnose one report failure and locate matching suite APK/JAR source."""
    try:
        failure_location = StackTraceUtils.extract_failure_location(request.stack_trace or "")
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
                kb = _get_knowledge_base()
                kb_query = " ".join(keywords[:5]) or request.test_name or request.error_message[:80]
                if kb_query.strip():
                    return await asyncio.to_thread(kb.search, kb_query, 8)
            except Exception as kb_error:
                logger.warning(f"Knowledge base search failed: {kb_error}")
            return []

        suite_target, ai_result, kb_results = await asyncio.gather(
            _resolve_suite_target(),
            _run_ai_analysis(),
            _search_knowledge_base(),
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
    """Delete test report (owner only)."""
    try:
        client_id = get_client_id_from_request(request)
        report = test_report_db.get_report_by_timestamp(timestamp)

        if not report:
            return error_response("Report not found", 404)

        report_client_id = report.get("client_id")
        if report_client_id != client_id:
            logger.warning(f"[DELETE] Permission denied: {client_id} tried to delete {report_client_id}'s report")
            return error_response("No permission to delete this report", 403)

        result_dir = report.get("result_dir")
        if result_dir and os.path.exists(result_dir):
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
async def knowledgebase_search(query: str = Query(..., min_length=1, max_length=256), limit: int = Query(8, ge=1, le=20)):
    """Search the local Redmine-derived GMS knowledge base."""
    try:
        kb = _get_knowledge_base()
        results = await asyncio.to_thread(kb.search, query.strip(), limit)
        return success_response({"query": query.strip(), "results": results, "count": len(results)})
    except Exception as e:
        logger.error(f"Knowledge base search failed: {e}", exc_info=True)
        return error_response(str(e), status_code=500)


@router.get("/api/knowledgebase/stats")
async def knowledgebase_stats():
    """Return local Redmine-derived GMS knowledge base stats."""
    try:
        kb = _get_knowledge_base()
        stats = await asyncio.to_thread(kb.get_stats)
        return success_response({"stats": stats})
    except Exception as e:
        logger.error(f"Knowledge base stats failed: {e}", exc_info=True)
        return error_response(str(e), status_code=500)
