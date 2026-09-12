"""GMS Assistant local boot shell and same-origin HTTPS proxy.

The proxy must never hold the browser's DOM lifecycle hostage: the bare
``/gms-assistant`` document returns a tiny LOCAL shell immediately
(DOMContentLoaded fires right away) and the upstream SPA loads
asynchronously inside a nested frame with a boot overlay, a hard timeout
and a retry button.  An unconfigured/slow/dead Assistant degrades to an
error card instead of a 15s+ blank navigation.

Split out of ``api.py`` to keep the system router reviewable
(see docs/architecture/adr/0002-feature-foundation-boundary.md).
"""

import logging
import os
import re
from urllib.parse import urlparse

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    Response,
)

from foundation.config import config_manager


logger = logging.getLogger(__name__)
router = APIRouter()

# Query marker that makes /gms-assistant/ proxy the upstream index. The
# local boot shell points its inner frame at it; without the marker the
# empty path returns the boot shell itself (no recursion).
_ASSISTANT_UPSTREAM_BOOT_QUERY = "__gms_boot=upstream"

# Hard cap for buffered upstream responses: the proxy reads the whole body
# into memory, so an unbounded upstream could exhaust Controller memory.
_GMS_ASSISTANT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024

# Hard cap for proxied request bodies (chat POSTs etc.). Same rationale as
# the response cap — the body is fully buffered before relay — and it must
# hold even when the upstream later grows attachment support.
_GMS_ASSISTANT_MAX_REQUEST_BYTES = 16 * 1024 * 1024

# Plaintext HTTP upstreams are only tolerated on loopback (dev servers).
_GMS_ASSISTANT_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class _UpstreamResponseTooLargeError(Exception):
    """Upstream body exceeded _GMS_ASSISTANT_MAX_RESPONSE_BYTES."""


class _RequestBodyTooLargeError(Exception):
    """Client request body exceeded _GMS_ASSISTANT_MAX_REQUEST_BYTES."""


async def _read_capped_request_body(request: Request) -> bytes:
    """Read the request body with a hard size cap (awaitable)."""

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _GMS_ASSISTANT_MAX_REQUEST_BYTES:
            raise _RequestBodyTooLargeError()
        chunks.append(chunk)
    return b"".join(chunks)

_ASSISTANT_BOOT_SHELL = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GMS助手</title>
<style>
  html, body { margin: 0; height: 100%; background: #f7f8fa;
      font-family: system-ui, sans-serif; }
  #assistant-upstream-frame { position: fixed; inset: 0; width: 100%;
      height: 100%; border: 0; }
  #assistant-boot { position: fixed; inset: 0; display: flex;
      align-items: center; justify-content: center; background: #f7f8fa;
      transition: opacity .25s ease; }
  #assistant-boot.done { opacity: 0; pointer-events: none; }
  .boot-card { text-align: center; color: #243042; }
  .boot-spinner { width: 34px; height: 34px; margin: 0 auto 14px;
      border: 3px solid #d7dce3; border-top-color: #3b82f6;
      border-radius: 50%; animation: boot-spin 0.9s linear infinite; }
  #assistant-boot.error .boot-spinner { display: none; }
  @keyframes boot-spin { to { transform: rotate(360deg); } }
  #assistant-boot-retry { margin-top: 14px; padding: 8px 22px;
      border: 1px solid #c9d0d9; border-radius: 8px; background: #fff;
      cursor: pointer; font-size: 14px; }
  #assistant-boot-retry:hover { background: #f0f2f5; }
</style>
</head>
<body>
<iframe id="assistant-upstream-frame" src="/gms-assistant/?__GMS_BOOT_MARKER__"
        title="GMS助手"></iframe>
<div id="assistant-boot"><div class="boot-card">
  <div class="boot-spinner"></div>
  <div id="assistant-boot-text">正在连接 GMS 助手…</div>
  <button id="assistant-boot-retry" type="button" hidden>重试</button>
</div></div>
<script>
(function () {
  var BOOT_TIMEOUT_MS = 20000;
  var frame = document.getElementById('assistant-upstream-frame');
  var boot = document.getElementById('assistant-boot');
  var text = document.getElementById('assistant-boot-text');
  var retry = document.getElementById('assistant-boot-retry');
  var settled = false;
  var timer = null;

  function armTimer() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(fail, BOOT_TIMEOUT_MS);
  }
  function settle() {
    if (settled) return;
    settled = true;
    if (timer) clearTimeout(timer);
    boot.classList.add('done');
  }
  function fail() {
    if (settled) return;
    settled = true;
    if (timer) clearTimeout(timer);
    boot.classList.add('error');
    text.textContent = 'GMS 助手服务暂不可用（未配置或上游无响应）';
    retry.hidden = false;
  }
  retry.addEventListener('click', function () {
    settled = false;
    boot.classList.remove('error');
    retry.hidden = true;
    text.textContent = '正在连接 GMS 助手…';
    frame.src = '/gms-assistant/?__GMS_BOOT_MARKER__&retry=' + Date.now();
    armTimer();
  });
  frame.addEventListener('error', fail);
  frame.addEventListener('load', function () {
    // Same-origin peek: the proxy answers hard failures with a small JSON
    // error body instead of the upstream document — treat that as a boot
    // failure instead of hiding the overlay over an error page.
    try {
      var doc = frame.contentDocument;
      var bodyText = doc && doc.body ? (doc.body.textContent || '') : '';
      var payload = null;
      try { payload = JSON.parse(bodyText); } catch (parseError) { payload = null; }
      if (payload && payload.boot_error === true) { fail(); return; }
    } catch (err) { /* cross-origin: trust the load event */ }
    settle();
  });
  armTimer();
})();
</script>
</body>
</html>
"""

# The controller shell deliberately keeps style-src restricted to
# same-origin CSS.  Remove the assistant's optional Google Fonts link so
# the iframe uses its local/system fallback fonts without producing CSP
# violations.
_EXTERNAL_GOOGLE_FONT_LINK_RE = re.compile(
    r"<link\b(?=[^>]*\bhref\s*=\s*['\"]https://fonts\.(?:googleapis|gstatic)\.com(?:/[^'\"]*)?['\"])[^>]*>\s*",
    re.IGNORECASE,
)


def _gms_assistant_boot_shell() -> str:
    return _ASSISTANT_BOOT_SHELL.replace("__GMS_BOOT_MARKER__", _ASSISTANT_UPSTREAM_BOOT_QUERY)


def _gms_assistant_upstream() -> str:
    """Resolve the optional upstream from environment or product config."""
    env_url = str(os.getenv("GMS_ASSISTANT_URL") or "").strip()
    if env_url:
        return env_url.rstrip("/")
    config = config_manager.load_config()
    external = config.get("external_services") or {}
    return str(external.get("gms_assistant_url") or "").strip().rstrip("/")


def _gms_assistant_api_key() -> str:
    """Return the server-side API key used by the Assistant upstream."""
    env_key = str(os.getenv("GMS_ASSISTANT_API_KEY") or "").strip()
    if env_key:
        return env_key
    external = config_manager.load_config().get("external_services", {})
    return str(external.get("gms_assistant_api_key") or "").strip()


def _gms_assistant_upstream_allowed(upstream: str) -> bool:
    """Require HTTPS upstreams; allow plaintext HTTP on loopback only.

    The proxied hop carries the server-side assistant API key, so a remote
    plaintext hop would leak it; loopback HTTP stays available for local
    development servers.
    """
    parsed = urlparse(upstream)
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and (
        parsed.hostname or ""
    ) in _GMS_ASSISTANT_LOOPBACK_HOSTS


def _rewrite_gms_assistant_content(
    text: str,
    request: Request,
    proxy_base: str = "",
    upstream: str = "",
) -> str:
    """Rewrite upstream absolute URLs to this HTTPS origin."""
    text = _EXTERNAL_GOOGLE_FONT_LINK_RE.sub("", text)
    upstream = upstream or _gms_assistant_upstream()
    if not upstream:
        return text
    upstream_https = re.sub(r"^http://", "https://", upstream)
    base = proxy_base.rstrip("/")
    replacements = {
        upstream: base,
        upstream_https: base,
        upstream.replace("/", "\\/"): base.replace("/", "\\/"),
        upstream_https.replace("/", "\\/"): base.replace("/", "\\/"),
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    root_prefixes = ("/assets/", "/api/")
    for prefix in root_prefixes:
        text = text.replace(f'"{prefix}', f'"{base}{prefix}')
        text = text.replace(f"'{prefix}", f"'{base}{prefix}")
        text = text.replace(f"`{prefix}", f"`{base}{prefix}")
    return text


def _assistant_boot_error(message: str, request: Request, status_code: int) -> JSONResponse:
    return JSONResponse(
        content={"success": False, "boot_error": True, "error": message,
                 "request_id": getattr(request.state, "request_id", None)},
        status_code=status_code,
    )


async def _proxy_gms_assistant_path(path: str, request: Request, proxy_base: str = ""):
    """Same-origin HTTPS proxy for the external GMS assistant upstream."""
    upstream = _gms_assistant_upstream()
    if upstream and not _gms_assistant_upstream_allowed(upstream):
        logger.warning(
            "[GMS_ASSISTANT_PROXY] 上游必须为 HTTPS（仅回环地址允许 HTTP），忽略: %s",
            upstream,
        )
        upstream = ""
    if not upstream:
        if path.startswith("public/agents/") and path.endswith("/chat"):
            return HTMLResponse(
                """<!doctype html><html lang='zh-CN'><meta charset='utf-8'>
<title>GMS助手未配置</title><style>
body{font-family:system-ui,sans-serif;margin:0;padding:32px;color:#243042;background:#f7f8fa}
main{max-width:640px;margin:8vh auto;padding:28px;background:#fff;border:1px solid #e3e7ed;border-radius:12px}
code{background:#f0f2f5;padding:3px 6px;border-radius:4px}
</style><main><h2>GMS助手暂未配置</h2>
<p>请在服务器配置中设置 <code>external_services.gms_assistant_url</code>，然后刷新此页面。</p>
</main>""",
                status_code=200,
            )
        return _assistant_boot_error(
            "GMS助手未配置，请设置 external_services.gms_assistant_url", request, 503
        )
    upstream_url = f"{upstream}/{path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    # 请求头白名单：绝不把浏览器的会话凭证（Cookie/Authorization，即
    # Controller 全域登录态）转发给 Assistant 上游——上游拿到即可重放
    # 直连 Controller。上游鉴权只使用服务端保管的 X-API-Key。
    forwarded_request_headers = {
        "accept",
        "accept-language",
        "content-type",
        "user-agent",
        "x-request-id",
        "x-trace-id",
    }
    request_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in forwarded_request_headers
    }
    if path.startswith("api/public/agents/"):
        assistant_api_key = _gms_assistant_api_key()
        if assistant_api_key:
            request_headers["X-API-Key"] = assistant_api_key
    request_headers["Host"] = urlparse(upstream).netloc

    # 响应头黑名单：除跳板/缓存类头外，必须剥离会话写入与服务器指纹。
    excluded_response_headers = {
        "connection",
        "content-encoding",
        "content-length",
        "content-security-policy",
        "date",
        "etag",
        "expires",
        "keep-alive",
        "last-modified",
        "proxy-authenticate",
        "server",
        "set-cookie",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "www-authenticate",
        "x-frame-options",
    }

    try:
        body = await _read_capped_request_body(request)
    except _RequestBodyTooLargeError:
        logger.warning(
            "[GMS_ASSISTANT_PROXY] 请求体超过 %d 字节上限，已拒绝: %s/%s",
            _GMS_ASSISTANT_MAX_REQUEST_BYTES,
            upstream,
            path,
        )
        return JSONResponse(
            content={
                "success": False,
                "error": "请求体超过代理上限",
                "request_id": getattr(request.state, "request_id", None),
            },
            status_code=413,
        )

    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.request(
            request.method,
            upstream_url,
            headers=request_headers,
            data=body,
            allow_redirects=False,
        ) as upstream_response:
            chunks = []
            total = 0
            while True:
                chunk = await upstream_response.content.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > _GMS_ASSISTANT_MAX_RESPONSE_BYTES:
                    raise _UpstreamResponseTooLargeError(upstream_url)
                chunks.append(chunk)
            body = b"".join(chunks)
            content_type = upstream_response.headers.get("content-type", "")
            response_headers = {
                key: value
                for key, value in upstream_response.headers.items()
                if key.lower() not in excluded_response_headers
            }

            if upstream_response.status in {301, 302, 303, 307, 308}:
                location = response_headers.get("Location") or response_headers.get("location")
                if location:
                    response_headers["Location"] = location.replace(upstream, "/gms-assistant")

            if any(marker in content_type for marker in ("text/", "javascript", "json")):
                try:
                    text = body.decode(upstream_response.charset or "utf-8", errors="replace")
                    body = _rewrite_gms_assistant_content(
                        text, request, proxy_base=proxy_base, upstream=upstream
                    ).encode("utf-8")
                    response_headers.pop("Content-Length", None)
                    response_headers.pop("content-length", None)
                except Exception:
                    logger.debug("[GMS_ASSISTANT_PROXY] 跳过内容重写: %s", upstream_url, exc_info=True)

            return Response(
                content=body,
                status_code=upstream_response.status,
                media_type=content_type.split(";")[0] if content_type else None,
                headers=response_headers,
            )
    except _UpstreamResponseTooLargeError:
        logger.error(
            "[GMS_ASSISTANT_PROXY] 上游响应超过 %d 字节上限，已中止: %s",
            _GMS_ASSISTANT_MAX_RESPONSE_BYTES,
            upstream_url,
        )
        return _assistant_boot_error("GMS助手服务暂不可用", request, 502)
    except Exception:
        # 详细异常只写服务端日志；前端只拿到通用错误与 request_id，
        # 避免把内部连接细节（地址/超时/证书错误）泄漏给浏览器。
        logger.exception("[GMS_ASSISTANT_PROXY] 代理失败 %s", upstream_url)
        return _assistant_boot_error("GMS助手服务暂不可用", request, 502)


@router.api_route(
    "/gms-assistant/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_gms_assistant(path: str, request: Request):
    # The bare document must never await the upstream — return the local
    # boot shell immediately and let it load the upstream asynchronously
    # (marker query opts back into proxying, no recursion).
    if (
        not path
        and request.method == "GET"
        and _ASSISTANT_UPSTREAM_BOOT_QUERY not in request.url.query
    ):
        return HTMLResponse(_gms_assistant_boot_shell())
    return await _proxy_gms_assistant_path(path, request, proxy_base="/gms-assistant")


@router.get("/gms-assistant", include_in_schema=False, response_class=HTMLResponse)
async def gms_assistant_root():
    """Local boot shell for the exact /gms-assistant URL (no redirect)."""
    return HTMLResponse(_gms_assistant_boot_shell())


# --- root-level compatibility routes (DEPRECATED) --------------------------
# Content rewriting already points every upstream asset at
# /gms-assistant/...; these root-level aliases only exist for older
# bookmarked/registered URLs. They keep working (proxying is unchanged) but
# advertise their removal via Deprecation/Sunset; drop them once the
# successor scoped routes are confirmed as the only seen traffic.
_ASSISTANT_ROOT_SHIM_SUNSET = "Wed, 31 Dec 2025 23:59:59 GMT"


def _deprecation_headers() -> dict[str, str]:
    return {
        "Deprecation": "true",
        "Sunset": _ASSISTANT_ROOT_SHIM_SUNSET,
        'Link': '</gms-assistant>; rel="successor-version"',
    }


@router.api_route(
    "/public/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_gms_assistant_public(path: str, request: Request):
    response = await _proxy_gms_assistant_path(f"public/{path}", request)
    response.headers.update(_deprecation_headers())
    return response


@router.api_route(
    "/assets/{path:path}",
    methods=["GET"],
    include_in_schema=False,
)
async def proxy_gms_assistant_assets(path: str, request: Request):
    response = await _proxy_gms_assistant_path(f"assets/{path}", request)
    response.headers.update(_deprecation_headers())
    return response


def _gms_assistant_dev_proxy_enabled() -> bool:
    """Vite dev-server proxy routes are dev-only.

    /@vite, /@react-refresh, /@id, /@fs, /src, /node_modules exist only to
    serve the Assistant's Vite DEVELOPMENT server. They pollute the root
    route namespace and must never be reachable in a production deployment;
    set GMS_ASSISTANT_DEV_PROXY=1 when developing against a dev upstream.
    """

    return os.getenv("GMS_ASSISTANT_DEV_PROXY", "").strip() == "1"


if _gms_assistant_dev_proxy_enabled():

    @router.api_route(
        "/@vite/{path:path}",
        methods=["GET"],
        include_in_schema=False,
    )
    async def proxy_gms_assistant_vite(path: str, request: Request):
        return await _proxy_gms_assistant_path(f"@vite/{path}", request)

    @router.api_route(
        "/@react-refresh",
        methods=["GET"],
        include_in_schema=False,
    )
    async def proxy_gms_assistant_react_refresh(request: Request):
        return await _proxy_gms_assistant_path("@react-refresh", request)

    @router.api_route(
        "/src/{path:path}",
        methods=["GET"],
        include_in_schema=False,
    )
    async def proxy_gms_assistant_src(path: str, request: Request):
        return await _proxy_gms_assistant_path(f"src/{path}", request)

    @router.api_route(
        "/node_modules/{path:path}",
        methods=["GET"],
        include_in_schema=False,
    )
    async def proxy_gms_assistant_node_modules(path: str, request: Request):
        return await _proxy_gms_assistant_path(f"node_modules/{path}", request)

    @router.api_route(
        "/@id/{path:path}",
        methods=["GET"],
        include_in_schema=False,
    )
    async def proxy_gms_assistant_vite_id(path: str, request: Request):
        return await _proxy_gms_assistant_path(f"@id/{path}", request)

    @router.api_route(
        "/@fs/{path:path}",
        methods=["GET"],
        include_in_schema=False,
    )
    async def proxy_gms_assistant_vite_fs(path: str, request: Request):
        return await _proxy_gms_assistant_path(f"@fs/{path}", request)


@router.api_route(
    "/api/public/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_gms_assistant_public_api(path: str, request: Request):
    response = await _proxy_gms_assistant_path(f"api/public/{path}", request)
    response.headers.update(_deprecation_headers())
    return response
