from contextlib import suppress

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
    _redmine_public_url_hint,
    _rename_downloaded_report_if_needed,
    _save_redmine_credentials,
    aiohttp,
    config_manager,
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

# ==================== Analyze URL ====================

@router.post("/api/reports/analyze-url")
async def analyze_report_from_url(request: Request):
    """Download and analyze test report from URL (supports Redmine attachment auto-download)."""
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

        logger.info(f"[Report Analysis] URL analysis request: {url}")

        parsed_url = urlparse(url)
        filename = os.path.basename(parsed_url.path) or "downloaded_file.zip"

        redmine_config = None
        configured_redmine_match = False
        try:
            redmine_config = config_manager.get_redmine_config()
            configured_domain = (redmine_config.get("domain") or "").lower()
            configured_base_host = urlparse(redmine_config.get("base_url", "")).netloc.lower()
            current_host = parsed_url.netloc.lower()
            configured_redmine_match = (
                bool(configured_domain and configured_domain in url.lower())
                or bool(configured_base_host and configured_base_host == current_host)
            )
        except ValueError:
            pass

        redmine_like_url = _looks_like_redmine_url(url)
        if redmine_like_url and not configured_redmine_match:
            public_url = _redmine_public_url_hint(url, redmine_config)
            return error_response(f"请使用公网 Redmine 地址：{public_url}", 400)

        is_redmine = configured_redmine_match
        redmine_base_url = _redmine_base_url_for(redmine_config, configured_redmine_match) if is_redmine else ""

        original_issue_id = source_issue_id or None
        attachment_owner_issue_id = None

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

                    stored_creds = await _load_redmine_credentials()
                    if stored_creds:
                        username = stored_creds.get("username")
                        password = stored_creds.get("password")
                        logger.info(f"[Report Analysis] Using stored credentials for issue {issue_id}")
                    else:
                        username = ""
                        password = ""
                        logger.warning("[Report Analysis] No stored credentials, anonymous query")

                    client = RedmineClient(base_url, username, password)
                    first_attachment = await client.first_issue_attachment(issue_id)
                    if not first_attachment:
                        return error_response(f"Issue {issue_id} has no attachments", 404)
                    attachment_id = first_attachment.id
                    filename = first_attachment.filename or f"attachment_{attachment_id}"
                    url = first_attachment.content_url or client.download_url(attachment_id)
                    logger.info(f"[Report Analysis] Extracted attachment: {filename} -> {url}")
                except Exception as extract_error:
                    logger.error(f"[Report Analysis] Attachment extraction failed: {extract_error}")
                    return error_response(f"Cannot extract attachment: {extract_error!s}", 500)
            else:
                redmine_attach_match = COMPILED_REDMINE_ATTACHMENT_PATTERN.search(url)
                if redmine_attach_match:
                    attachment_id = redmine_attach_match.group(1)
                    logger.info(f"[Report Analysis] Redmine attachment URL, ID: {attachment_id}")

                    if attachment_id in REDMINE_ISSUE_ID_CACHE:
                        cached_issue_id = REDMINE_ISSUE_ID_CACHE[attachment_id]
                        if not original_issue_id:
                            original_issue_id = cached_issue_id
                    else:
                        try:
                            base_url = redmine_base_url
                            if not base_url:
                                raise ValueError("Redmine base URL unavailable")
                            stored_creds = await _load_redmine_credentials()
                            client = RedmineClient(base_url, (stored_creds or {}).get("username", ""), (stored_creds or {}).get("password", ""))
                            attachment_owner_issue_id = await client.find_attachment_issue_id(attachment_id)
                            if attachment_owner_issue_id and not original_issue_id:
                                original_issue_id = attachment_owner_issue_id
                        except Exception as search_error:
                            logger.warning(f"[Report Analysis] Query attachment owner failed: {search_error}")

                    if "/attachments/download/" not in url:
                        if not redmine_base_url:
                            return error_response("Redmine base URL unavailable", 404)
                        url = RedmineClient(redmine_base_url).download_url(attachment_id)
                        logger.info(f"[Report Analysis] Converted to download URL: {url}")

                    filename = f"attachment_{attachment_id}"

        logger.info(f"[Report Analysis] Downloading: {filename}")
        temp_dir = tempfile.mkdtemp(prefix="redmine_download_")
        temp_file_path = ""

        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

            if is_redmine:
                if redmine_username and redmine_password:
                    headers.update(create_basic_auth_header(redmine_username, redmine_password))
                    await _save_redmine_credentials(redmine_username, redmine_password)
                else:
                    stored_creds = await _load_redmine_credentials()
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
                ) as response,
            ):
                    if response.status == 401 or response.status == 403:
                        if is_redmine:
                            return error_response("Redmine auth failed", status_code=403, requires_auth=True, is_redmine=True)
                        else:
                            return error_response(f"Download failed, HTTP {response.status}", 400)
                    elif response.status != 200:
                        return error_response(f"Download failed, HTTP {response.status}", 400)

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
                            f.write(chunk)
                            downloaded_size += len(chunk)

                    logger.info(f"[Report Analysis] Download complete: {downloaded_size} bytes")
                    temp_file_path, filename = _rename_downloaded_report_if_needed(temp_file_path, real_filename, content_type)

            logger.info(f"[Report Analysis] Analyzing: {temp_file_path}")
            result = await _analyze_report_file(temp_file_path, temp_dir)
            if result:
                logger.info(f"[Report Analysis] Analysis complete - failures: {len(result.get('failures', []))}")
            else:
                logger.warning("[Report Analysis] Empty analysis result")

            with suppress(Exception):
                shutil.rmtree(temp_dir)

            if result:
                report_name = filename

                if is_redmine:
                    if original_issue_id:
                        report_filename = strip_redmine_report_prefix(filename)
                        report_name = f"Redmine-{original_issue_id}-{report_filename}"
                        logger.info(f"[Report Analysis] Redmine prefix: {report_name}")
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
                        if REDMINE_ISSUE_ID_CACHE.get(attachment_id_for_cache) != original_issue_id:
                            if len(REDMINE_ISSUE_ID_CACHE) >= REDMINE_ISSUE_ID_CACHE_MAX_SIZE:
                                REDMINE_ISSUE_ID_CACHE.popitem(last=False)
                            REDMINE_ISSUE_ID_CACHE[attachment_id_for_cache] = original_issue_id

                return JSONResponse(content={"success": True, "data": result, "filename": filename, "mode": "url"})
            else:
                return error_response("Report analysis failed", 500)

        except Exception as download_error:
            with suppress(Exception):
                shutil.rmtree(temp_dir)
            raise download_error

    except Exception as e:
        logger.error(f"[Report Analysis] URL analysis failed: {e}", exc_info=True)
        return error_response(f"Download or analysis failed: {e!s}", 500)


# ==================== Redmine Config ====================

@router.get("/api/config/redmine")
async def get_redmine_config(request: Request):
    """Get Redmine configuration."""
    try:
        redmine_config = config_manager.get_redmine_config()
        return JSONResponse(content={"success": True, "data": redmine_config})
    except ValueError as e:
        return error_response(str(e), status_code=404)
    except Exception as e:
        return error_response(f"Failed to get Redmine config: {e!s}", status_code=500)


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

        stored_creds = await _load_redmine_credentials()
        if not stored_creds:
            return error_response("Redmine credentials not configured", 401)

        try:
            redmine_config = config_manager.get_redmine_config()
        except ValueError:
            redmine_config = None

        parsed_issue_url = urlparse(issue_url)
        configured_match = False
        if redmine_config:
            configured_domain = (redmine_config.get("domain") or "").lower()
            configured_base_host = urlparse(redmine_config.get("base_url", "")).netloc.lower()
            current_host = parsed_issue_url.netloc.lower()
            configured_match = (
                bool(configured_domain and configured_domain in issue_url.lower())
                or bool(configured_base_host and configured_base_host == current_host)
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
        first_attachment = await client.first_issue_attachment(issue_id)
        if not first_attachment:
            return error_response("Issue has no attachments", 404)
        attachment_url = first_attachment.content_url or client.download_url(first_attachment.id)

        logger.info(f"[Redmine Extract] Found attachment: {first_attachment.filename} (ID: {first_attachment.id})")
        return JSONResponse(content={"success": True, "attachment_url": attachment_url, "filename": first_attachment.filename, "attachment_id": first_attachment.id})

    except Exception as e:
        logger.error(f"[Redmine Extract] Failed: {e}")
        return error_response(f"Extraction failed: {e!s}", 500)
