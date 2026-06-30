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
    get_client_id_from_request,
    json,
    logger,
    os,
    re,
    safe_upload_target_path,
    save_upload_to_path,
    shutil,
    success_response,
    tempfile,
    test_report_db,
    test_report_manager,
)


router = APIRouter()


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
                    "android_version": _android_version_from_request(request),
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
                    return _rank_kb_hits(merged, probe)

                return await asyncio.to_thread(_gather)
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


# ==================== Knowledge-base relevance ranking ====================


def _android_version_from_request(request: ReportDiagnosisRequest) -> str:
    """Best-effort Android major version from suite_version (e.g. '16_r5' -> '16')."""
    raw = (getattr(request, "suite_version", "") or "").strip()
    m = re.match(r"(\d+)", raw)
    return m.group(1) if m else ""


def _test_method_and_class(test_name: str) -> tuple[str, str]:
    """Return (method_name, simple_class) from 'a.b.C#method' or 'a.b.C'."""
    test_name = (test_name or "").strip()
    method = ""
    cls = test_name
    if "#" in test_name:
        cls, method = test_name.split("#", 1)
    simple_cls = cls.rsplit(".", 1)[-1]
    return method.strip(), simple_cls.strip()


def _rank_kb_hits(hits: list[dict], probe: dict) -> list[dict]:
    """Filter and re-rank KB hits by relevance to the diagnosed failure.

    The raw FTS recall is broad — it surfaces any ticket sharing the module
    (e.g. CtsInputMethodTestCases), including cross-platform / cross-case
    noise. We score each hit against the failure under diagnosis and keep only
    the genuinely relevant ones, so the operator sees a few precise matches
    instead of eight loosely-related tickets.

    Scoring (higher = more relevant):
      exact test method in subject ........ +100  (top tier)
      exact test class in subject ......... +40
      same module ........................ +15
      same Android major version ......... +20
      verified (Closed/Confirmed) ........ +10
      curated case_fact ................... +15

    Hard filter: drop hits that match only the module on a different platform
    *and* lack any case/method overlap — pure cross-platform noise.
    """
    method, simple_cls = _test_method_and_class(probe.get("test_name") or "")
    module = (probe.get("module") or "").strip()
    probe_android = (probe.get("android_version") or "").strip()

    # Anchor platform from exact-method hits — when the failure has identical
    # tickets on one platform, a different-platform same-module ticket is noise.
    anchor_platform = ""
    if method:
        for h in hits:
            if method.lower() in (h.get("subject") or "").lower():
                anchor_platform = (h.get("chip_platform") or "").upper()
                if anchor_platform:
                    break

    def _score(h: dict) -> tuple[float, bool]:
        subject = (h.get("subject") or "").lower()
        root = (h.get("root_cause") or "").lower()
        hay = f"{subject} {root}"
        s = 0.0
        case_hit = method and method.lower() in hay
        cls_hit = simple_cls and simple_cls.lower() in hay
        if case_hit:
            s += 100
        if cls_hit:
            s += 40
        if module and module.lower() in hay:
            s += 15
        if probe_android:
            theirs = (h.get("android_version") or "").strip()
            if theirs and probe_android in theirs:
                s += 20
        status = (h.get("status_name") or "").lower()
        if status in ("closed", "confirmed", "已关闭", "已解决", "resolved"):
            s += 10
        if h.get("source") == "case_facts":
            s += 15
        # A hit carrying a real, distilled solution/root cause is far more
        # valuable as a reference than a bare same-module ticket — reward it so
        # verified historical fixes surface above generic same-module noise.
        sol = (h.get("solution_summary") or h.get("root_cause") or "")
        if len(sol.strip()) >= 40:
            s += 8
        # Cross-platform noise penalty: same-module only, different platform
        # from the anchored exact matches, and no case/class overlap.
        plat = (h.get("chip_platform") or "").upper()
        keep = True
        if anchor_platform and plat and plat != anchor_platform and not (case_hit or cls_hit):
            keep = False
        return s, keep

    scored = []
    for h in hits:
        s, keep = _score(h)
        if not keep:
            continue
        h_out = dict(h)
        h_out["score"] = round(s, 1)
        h_out["similarity_level"] = (
            "exact" if s >= 100 else "high" if s >= 50 else "medium" if s >= 30 else "low"
        )
        scored.append((s, h_out))

    # If filtering removed everything (e.g. no anchor could be established and
    # nothing matched), fall back to the raw recall rather than showing "未命中".
    # Raw hits carry no score (those are added in _score below), so stamp a
    # neutral default on each.
    if not scored:
        return [{**h, "score": 0.0, "similarity_level": "low"} for h in hits[:5]]

    scored.sort(key=lambda x: x[0], reverse=True)
    ordered = [h for _, h in scored]

    # When we have a precise (exact) match, keep the results tight: the exact
    # hit plus high-relevance references, with at most two same-module-only
    # tickets as supporting context. Operators want a few precise matches, not a
    # wall of loosely-related same-module tickets.
    has_exact = any(h["similarity_level"] == "exact" for h in ordered)
    if has_exact:
        precise = [h for h in ordered if h["similarity_level"] in ("exact", "high")]
        same_module = [h for h in ordered if h["similarity_level"] not in ("exact", "high")]
        return (precise + same_module)[:5]

    return ordered[:5]
