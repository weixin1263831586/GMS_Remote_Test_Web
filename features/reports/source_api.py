from contextlib import suppress

from features.auth import require_authenticated_user
from foundation.outbound import (
    UnsafeOutboundURL,
    same_http_origin,
    url_hostname,
    validate_outbound_url,
)
from foundation.redaction import redact_sensitive_text

from .api_helpers import (
    COMPILED_REDMINE_ATTACHMENT_PATTERN,
    COMPILED_REDMINE_ISSUE_PATTERN,
    COMPILED_REPORT_NAME_PATTERN,
    REDMINE_ISSUE_ID_CACHE,
    REDMINE_ISSUE_ID_CACHE_MAX_SIZE,
    REDMINE_ISSUE_PATTERN,
    APIRouter,
    JSONResponse,
    RedmineClient,
    Request,
    _analyze_report_file,
    _load_redmine_credentials,
    _looks_like_redmine_url,
    _redmine_base_url_for,
    _redmine_config_manager_for_request,
    _redmine_public_url_hint,
    _rename_downloaded_report_if_needed,
    _save_redmine_credentials,
    aiohttp,
    create_basic_auth_header,
    error_response,
    extract_filename_from_content_disposition,
    extract_redmine_issue_id_from_text,
    logger,
    os,
    re,
    safe_upload_target_path,
    shutil,
    strip_redmine_report_prefix,
    tempfile,
    urlparse,
)


router = APIRouter()
MAX_REPORT_URL_DOWNLOAD_BYTES = 512 * 1024 * 1024


def _url_log_target(value: str) -> str:
    """Return an origin/path-only URL suitable for operational logs."""

    parsed = urlparse(str(value or ''))
    return f"{parsed.scheme}://{parsed.hostname or ''}{parsed.path}"


def _request_redmine_config_manager(request: Request):
    require_authenticated_user(request)
    return _redmine_config_manager_for_request(request)


async def _load_redmine_credentials_for_request(request: Request):
    require_authenticated_user(request)
    return await _load_redmine_credentials(request)


async def _save_redmine_credentials_for_request(username: str, password: str, request: Request):
    require_authenticated_user(request)
    return await _save_redmine_credentials(username, password, request)


# ==================== Analyze URL ====================

@router.post("/api/reports/analyze-url")
async def analyze_report_from_url(request: Request):
    """Download and analyze test report from URL (supports Redmine attachment auto-download)."""
    require_authenticated_user(request)
    try:
        body = await request.json()
        url = body.get("url", "").strip()
        redmine_username = body.get("redmine_username", "").strip()
        redmine_password = body.get("redmine_password", "").strip()
        source_issue_id = str(body.get("source_issue_id") or "").strip()
        source_issue_url = body.get("source_issue_url", "").strip()
        source_issue_id = source_issue_id or extract_redmine_issue_id_from_text(source_issue_url)
        source_issue_id = source_issue_id if source_issue_id and source_issue_id.isdigit() else ""

        if not url:
            return error_response("Missing URL parameter", 400)

        logger.info(
            "[Report Analysis] URL analysis request: %s",
            _url_log_target(url),
        )

        parsed_url = urlparse(url)
        filename = os.path.basename(parsed_url.path) or "downloaded_file.zip"

        redmine_config = None
        configured_redmine_match = False
        request_redmine_config = _request_redmine_config_manager(request)
        try:
            redmine_config = request_redmine_config.get_redmine_config()
            configured_redmine_match = same_http_origin(
                url, redmine_config.get("base_url", "")
            )
        except ValueError as exc:
            logger.debug("Redmine config unavailable while classifying report URL: %s", exc)

        redmine_like_url = _looks_like_redmine_url(url)
        if redmine_like_url and not configured_redmine_match:
            public_url = _redmine_public_url_hint(url, redmine_config)
            return error_response(f"请使用公网 Redmine 地址：{public_url}", 400)

        is_redmine = configured_redmine_match
        redmine_base_url = _redmine_base_url_for(redmine_config, configured_redmine_match) if is_redmine else ""

        original_issue_id = source_issue_id or None
        attachment_owner_issue_id = None

        async def _redmine_credentials_for_lookup() -> tuple[str, str]:
            if redmine_username and redmine_password:
                return redmine_username, redmine_password
            stored_creds = await _load_redmine_credentials_for_request(request)
            return (stored_creds or {}).get("username", ""), (stored_creds or {}).get("password", "")

        if is_redmine:
            issue_match = COMPILED_REDMINE_ISSUE_PATTERN.search(url)
            if issue_match and "/attachments/" not in url:
                issue_id = issue_match.group(1)
                original_issue_id = issue_id
                logger.info(f"[Report Analysis] Redmine issue page detected: {issue_id}")

                try:
                    base_url = redmine_base_url
                    if not base_url:
                        return error_response("Redmine base URL unavailable", 404)

                    username, password = await _redmine_credentials_for_lookup()
                    if username and password:
                        logger.info(f"[Report Analysis] Using stored credentials for issue {issue_id}")
                    else:
                        logger.warning("[Report Analysis] No stored credentials, anonymous query")

                    client = RedmineClient(base_url, username, password)
                    try:
                        first_attachment = await client.first_issue_attachment(issue_id)
                        if not first_attachment:
                            return error_response(f"Issue {issue_id} has no attachments", 404)
                        attachment_id = first_attachment.id
                        filename = first_attachment.filename or f"attachment_{attachment_id}"
                        url = first_attachment.content_url or client.download_url(attachment_id)
                        logger.info(
                            "[Report Analysis] Extracted Redmine attachment ID: %s",
                            attachment_id,
                        )
                    finally:
                        await client.close()
                except Exception as extract_error:
                    logger.error(
                        "[Report Analysis] Attachment extraction failed: %s",
                        redact_sensitive_text(extract_error),
                    )
                    return error_response("Cannot extract Redmine attachment", 500)
            else:
                redmine_attach_match = COMPILED_REDMINE_ATTACHMENT_PATTERN.search(url)
                if redmine_attach_match:
                    attachment_id = redmine_attach_match.group(1)
                    logger.info(f"[Report Analysis] Redmine attachment URL, ID: {attachment_id}")
                    if original_issue_id == attachment_id:
                        logger.warning(
                            "[Report Analysis] Ignoring source_issue_id=%s because it matches attachment_id",
                            original_issue_id,
                        )
                        original_issue_id = None

                    if attachment_id in REDMINE_ISSUE_ID_CACHE:
                        cached_issue_id = REDMINE_ISSUE_ID_CACHE[attachment_id]
                        if cached_issue_id == attachment_id:
                            logger.warning(
                                "[Report Analysis] Ignoring cached issue_id=%s because it matches attachment_id",
                                cached_issue_id,
                            )
                        elif not original_issue_id:
                            original_issue_id = cached_issue_id
                    if not original_issue_id:
                        try:
                            base_url = redmine_base_url
                            if not base_url:
                                raise ValueError("Redmine base URL unavailable")
                            username, password = await _redmine_credentials_for_lookup()
                            client = RedmineClient(base_url, username, password)
                            try:
                                attachment_owner_issue_id = await client.find_attachment_issue_id(attachment_id)
                                if attachment_owner_issue_id and (
                                    not original_issue_id or original_issue_id == attachment_id
                                ):
                                    original_issue_id = attachment_owner_issue_id
                            finally:
                                await client.close()
                        except Exception as search_error:
                            logger.warning(
                                "[Report Analysis] Query attachment owner failed: %s",
                                redact_sensitive_text(search_error),
                            )

                    if "/attachments/download/" not in url:
                        if not redmine_base_url:
                            return error_response("Redmine base URL unavailable", 404)
                        url = RedmineClient(redmine_base_url).download_url(attachment_id)
                        logger.info("[Report Analysis] Converted attachment to download URL")

                    filename = f"attachment_{attachment_id}"

        logger.info("[Report Analysis] Downloading staged report")
        temp_dir = tempfile.mkdtemp(prefix="redmine_download_")
        temp_file_path = ""

        try:
            allowed_private_hosts = (
                {url_hostname(redmine_base_url)} if is_redmine else set()
            )
            if is_redmine and not same_http_origin(url, redmine_base_url):
                return error_response(
                    "Redmine attachment URL changed to an unauthorized origin",
                    400,
                )
            try:
                url = validate_outbound_url(
                    url,
                    allowed_private_hosts=allowed_private_hosts,
                )
            except UnsafeOutboundURL as exc:
                return error_response(str(exc), 400)

            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

            if is_redmine:
                if redmine_username and redmine_password:
                    headers.update(create_basic_auth_header(redmine_username, redmine_password))
                    await _save_redmine_credentials_for_request(redmine_username, redmine_password, request)
                else:
                    stored_creds = await _load_redmine_credentials_for_request(request)
                    if stored_creds:
                        redmine_username = stored_creds.get("username")
                        redmine_password = stored_creds.get("password")
                        headers.update(create_basic_auth_header(redmine_username, redmine_password))
                    else:
                        return error_response("Redmine credentials not configured", status_code=401, requires_auth=True, is_redmine=True)

            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=120),
                    headers=headers,
                    allow_redirects=False,
                ) as response,
            ):
                    if response.status == 401 or response.status == 403:
                        if is_redmine:
                            return error_response("Redmine auth failed", status_code=403, requires_auth=True, is_redmine=True)
                        else:
                            return error_response(f"Download failed, HTTP {response.status}", 400)
                    elif 300 <= response.status < 400:
                        return error_response("Download redirects are not allowed", 400)
                    elif response.status != 200:
                        return error_response(f"Download failed, HTTP {response.status}", 400)

                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        try:
                            declared_size = int(content_length)
                        except ValueError:
                            declared_size = 0
                        if declared_size > MAX_REPORT_URL_DOWNLOAD_BYTES:
                            return error_response("Report download is too large", 413)

                    downloaded_size = 0
                    real_filename = extract_filename_from_content_disposition(response.headers.get("Content-Disposition", "")) or filename
                    content_type = response.headers.get("Content-Type", "")
                    temp_file_path = safe_upload_target_path(temp_dir, real_filename, allow_nested=False)

                    redmine_prefix_match = COMPILED_REPORT_NAME_PATTERN.match(real_filename)
                    if redmine_prefix_match:
                        extracted_issue_id = redmine_prefix_match.group(1)
                        if not original_issue_id:
                            original_issue_id = extracted_issue_id

                    with open(temp_file_path, "wb") as f:
                        async for chunk in response.content.iter_chunked(262144):
                            downloaded_size += len(chunk)
                            if downloaded_size > MAX_REPORT_URL_DOWNLOAD_BYTES:
                                raise ValueError("Report download is too large")
                            f.write(chunk)

                    logger.info(f"[Report Analysis] Download complete: {downloaded_size} bytes")
                    temp_file_path, filename = _rename_downloaded_report_if_needed(temp_file_path, real_filename, content_type)

            logger.info("[Report Analysis] Analyzing staged report")
            result = await _analyze_report_file(temp_file_path, temp_dir)
            if result:
                logger.info(f"[Report Analysis] Analysis complete - failures: {len(result.get('failures', []))}")
            else:
                logger.warning("[Report Analysis] Empty analysis result")

            with suppress(Exception):
                shutil.rmtree(temp_dir)

            # 解析为空 ≠ 服务器错误：文件下载成功但不是有效的测试报告
            # （HTML/非报告 XML/空内容等）。返回 422 让前端提示「不是有效报告」，
            # 而不是误导性的 500。
            if not result:
                logger.warning("[Report Analysis] Empty analysis result")
                return error_response(
                    f"无法解析报告「{filename}」：不是有效的测试报告（test_result.xml/zip）",
                    status_code=422,
                )

            report_name = filename

            if is_redmine:
                if original_issue_id:
                    report_filename = strip_redmine_report_prefix(filename)
                    report_name = f"Redmine-{original_issue_id}-{report_filename}"
                    logger.info("[Report Analysis] Redmine report provenance attached")
                else:
                    issue_match = COMPILED_REDMINE_ISSUE_PATTERN.search(url)
                    if issue_match:
                        issue_id = issue_match.group(1)
                        report_name = f"Redmine-{issue_id}-{filename}"
                    else:
                        redmine_attach_match = COMPILED_REDMINE_ATTACHMENT_PATTERN.search(url)
                        if redmine_attach_match:
                            attachment_id = redmine_attach_match.group(1)
                            report_name = f"Redmine attachment {attachment_id}"
                        else:
                            report_name = "Redmine attachment report"

            result["report_name"] = report_name

            # Update cache
            if is_redmine and original_issue_id:
                redmine_attach_match = COMPILED_REDMINE_ATTACHMENT_PATTERN.search(url)
                if redmine_attach_match:
                    attachment_id_for_cache = redmine_attach_match.group(1)
                    if (
                        original_issue_id != attachment_id_for_cache
                        and REDMINE_ISSUE_ID_CACHE.get(attachment_id_for_cache) != original_issue_id
                    ):
                        if len(REDMINE_ISSUE_ID_CACHE) >= REDMINE_ISSUE_ID_CACHE_MAX_SIZE:
                            REDMINE_ISSUE_ID_CACHE.popitem(last=False)
                        REDMINE_ISSUE_ID_CACHE[attachment_id_for_cache] = original_issue_id

            return JSONResponse(content={"success": True, "data": result, "filename": filename, "mode": "url"})

        except Exception as download_error:
            with suppress(Exception):
                shutil.rmtree(temp_dir)
            raise download_error

    except Exception as e:
        logger.error(
            "[Report Analysis] URL analysis failed: %s",
            redact_sensitive_text(e),
        )
        return error_response("Download or analysis failed", 500)


# ==================== Redmine Config ====================

@router.get("/api/config/redmine")
async def get_redmine_config(request: Request):
    """Get Redmine configuration."""
    try:
        redmine_config = _request_redmine_config_manager(request).get_redmine_config()
        return JSONResponse(content={"success": True, "data": redmine_config})
    except ValueError as e:
        return error_response(str(e), status_code=404)
    except Exception:
        return error_response("Failed to get Redmine config", status_code=500)


# ==================== Extract Redmine Attachment ====================

@router.post("/api/reports/extract-redmine-attachment")
async def extract_redmine_attachment(request: Request):
    """Extract first attachment URL from Redmine issue page."""
    try:
        body = await request.json()
        issue_url = body.get("issue_url", "").strip()

        if not issue_url:
            return error_response("Missing issue_url", 400)

        issue_match = re.search(REDMINE_ISSUE_PATTERN, issue_url)
        if not issue_match:
            return error_response("Invalid issue URL", 400)

        issue_id = issue_match.group(1)
        logger.info(f"[Redmine Extract] Extracting issue {issue_id} attachment")

        stored_creds = await _load_redmine_credentials_for_request(request)
        if not stored_creds:
            return error_response("Redmine credentials not configured", 401)

        try:
            redmine_config = _request_redmine_config_manager(request).get_redmine_config()
        except ValueError:
            redmine_config = None

        configured_match = False
        if redmine_config:
            configured_match = same_http_origin(
                issue_url, redmine_config.get("base_url", "")
            )

        if not configured_match:
            public_url = _redmine_public_url_hint(issue_url, redmine_config)
            return error_response(f"请使用公网 Redmine 地址：{public_url}", 400)

        base_url = _redmine_base_url_for(redmine_config, configured_match)
        if not base_url:
            return error_response("Redmine base URL unavailable", 404)

        username = stored_creds.get("username")
        password = stored_creds.get("password")
        client = RedmineClient(base_url, username, password)
        try:
            first_attachment = await client.first_issue_attachment(issue_id)
            if not first_attachment:
                return error_response("Issue has no attachments", 404)
            attachment_url = first_attachment.content_url or client.download_url(first_attachment.id)
        finally:
            await client.close()

        logger.info("[Redmine Extract] Found attachment ID: %s", first_attachment.id)
        return JSONResponse(content={"success": True, "attachment_url": attachment_url, "filename": first_attachment.filename, "attachment_id": first_attachment.id})

    except Exception as e:
        logger.error("[Redmine Extract] Failed: %s", redact_sensitive_text(e))
        return error_response("Extraction failed", 500)
