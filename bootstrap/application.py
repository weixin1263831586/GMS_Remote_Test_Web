from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from bootstrap.dependencies import AppServices, build_services
from bootstrap.lifecycle import create_lifespan
from bootstrap.production_security import (
    validate_production_security_configuration,
)
from bootstrap.routes import include_routes
from features.auth import (
    AUTH_COOKIE_NAME,
    auth_service,
    authentication_required,
    csrf_rejection_reason,
)
from features.system.metrics import observe_request
from features.system.security_audit_utils import (
    can_audit_path,
    get_audit_operation,
    sanitize_audit_path,
    should_audit_request,
    summarize_audit_request,
    summarize_audit_response,
)
from features.users import get_client_id_from_request, get_client_ip, parse_client_id
from foundation.product import (
    APPLICATION_DESCRIPTION,
    APPLICATION_TITLE,
    APPLICATION_VERSION,
)
from foundation.runtime_settings import allowed_origins, runtime_environment
from foundation.security_audit import classify_request_source, security_audit_logger


class UTF8JSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            separators=(',', ':'),
        ).encode('utf-8')


def _csv_env(name: str, default: str = '') -> list[str]:
    values = [
        value.strip()
        for value in os.getenv(name, default).split(',')
        if value.strip()
    ]
    return values


_SERVICE_AUTHENTICATED_ROUTES = (
    ('POST', re.compile(r'^/api/cluster/workers/register$')),
    ('POST', re.compile(r'^/api/cluster/workers/[^/]+/heartbeat$')),
    ('POST', re.compile(r'^/api/cluster/workers/[^/]+/commands/poll$')),
    ('POST', re.compile(r'^/api/cluster/workers/[^/]+/commands/[^/]+/ack$')),
    ('POST', re.compile(
        r'^/api/cluster/workers/[^/]+/adb-proxy/pair-code$'
    )),
    ('GET', re.compile(r'^/api/cluster/workers/[^/]+/firmware/[^/]+$')),
    ('GET', re.compile(
        r'^/api/cluster/suite-library-download/[^/]+/[^/]+$'
    )),
    ('POST', re.compile(r'^/api/cluster/jobs/[^/]+/events$')),
    ('POST', re.compile(r'^/api/cluster/jobs/[^/]+/artifacts/uploads$')),
    ('GET', re.compile(r'^/api/cluster/jobs/[^/]+/artifacts/uploads/[^/]+$')),
    ('PUT', re.compile(
        r'^/api/cluster/jobs/[^/]+/artifacts/uploads/[^/]+/chunks/\d+$'
    )),
    ('POST', re.compile(
        r'^/api/cluster/jobs/[^/]+/artifacts/uploads/[^/]+/complete$'
    )),
    ('PUT', re.compile(r'^/api/cluster/jobs/[^/]+/artifacts/[^/]+$')),
    ('PUT', re.compile(r'^/api/cluster/transfers/[^/]+/chunks/\d+$')),
    ('POST', re.compile(r'^/api/cluster/transfers/[^/]+/complete$')),
    ('POST', re.compile(r'^/api/automation/gerrit/webhook$')),
    ('GET', re.compile(r'^/metrics$')),
)

_PUBLIC_FIRMWARE_SHARE_DOWNLOAD = re.compile(
    r'^/api/firmware-shares/(?:[0-9a-f]{12}|[0-9a-f]{32})/download$'
)


def _is_service_authenticated_path(path: str, method: str) -> bool:
    normalized_method = method.upper()
    return any(
        normalized_method == allowed_method and pattern.fullmatch(path)
        for allowed_method, pattern in _SERVICE_AUTHENTICATED_ROUTES
    )


def create_app(services: AppServices | None = None) -> FastAPI:
    services = build_services() if services is None else services
    validate_production_security_configuration()
    auth_service.initialize()
    app = FastAPI(
        title=APPLICATION_TITLE,
        description=APPLICATION_DESCRIPTION,
        version=APPLICATION_VERSION,
        lifespan=create_lifespan(services),
        default_response_class=UTF8JSONResponse,
    )
    app.state.services = services
    auth_required = authentication_required()
    app.state.authentication_required = auth_required

    # CORS 与生产校验共用 GMS_ALLOWED_ORIGINS 单一来源，避免
    # validation config != runtime config 的漂移。
    cors_origins = allowed_origins()
    if cors_origins:
        cors_credentials = os.getenv(
            'CORS_ALLOW_CREDENTIALS',
            'false',
        ).strip().lower() == 'true'
        if cors_credentials and '*' in cors_origins:
            raise RuntimeError(
                'GMS_ALLOWED_ORIGINS cannot contain * when credentials are enabled'
            )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=cors_credentials,
            allow_methods=['*'],
            allow_headers=['*'],
            expose_headers=['X-Request-ID', 'X-Trace-ID'],
        )
    # 环境判断走 foundation.runtime_environment() 单一真值，与
    # production_security 校验保持一致，杜绝“校验按生产、HTTP 层按开发”
    # 的 TrustedHosts 漂移。
    environment = runtime_environment()
    trusted_hosts = _csv_env(
        'TRUSTED_HOSTS',
        '*' if environment != 'production' else '',
    )
    if environment == 'production':
        if not trusted_hosts:
            raise RuntimeError('TRUSTED_HOSTS is required in production')
        if '*' in trusted_hosts:
            raise RuntimeError('TRUSTED_HOSTS cannot contain * in production')
    if trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)
    app.add_middleware(GZipMiddleware, minimum_size=500)

    def _is_public_path(path: str, method: str) -> bool:
        if method == 'OPTIONS':
            return True
        if path in {'/', '/favicon.ico'}:
            return True
        if path.startswith('/static/'):
            return True
        if path in {
            '/api/auth/status',
            '/api/auth/setup',
            '/api/auth/login',
            '/api/auth/logout',
            # 登录层主动探测客户端 SSH 端口（未装 SSHD 时提前提示安装）。
            '/api/auth/client-ssh-status',
            # 登录前展示请求主机对应的身份。
            '/api/users/current',
            '/api/system/health',
            '/api/system/health/live',
            '/api/system/health/ready',
        }:
            return True
        if method in {'GET', 'HEAD'} and path in {
            '/api/system/skills',
            '/api/system/skills/install.sh',
        }:
            return True
        if method == 'GET' and _PUBLIC_FIRMWARE_SHARE_DOWNLOAD.fullmatch(path):
            return True
        return False

    @app.middleware('http')
    async def audit_and_security_headers(request, call_next):
        path = request.url.path
        def correlation_id(header: str, prefix: str) -> str:
            supplied = str(request.headers.get(header, '') or '').strip()[:128]
            return supplied if re.fullmatch(r'[A-Za-z0-9_.:-]+', supplied) else f'{prefix}-{uuid.uuid4().hex}'

        request_id = correlation_id('X-Request-ID', 'req')
        trace_id = str(request.headers.get('X-Trace-ID', '') or '').strip()[:128]
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', trace_id):
            trace_id = request_id
        request.state.request_id = request_id
        request.state.trace_id = trace_id
        token = request.cookies.get(AUTH_COOKIE_NAME)
        current_user = auth_service.get_user_for_token(token)
        if current_user:
            request.state.current_user = current_user

        source = classify_request_source(
            request.headers.get('user-agent', ''),
            path,
        )
        # 服务认证流量仅在失败时写入安全审计，避免高频轮询撑大日志。
        is_service_path = _is_service_authenticated_path(path, request.method)
        should_audit = should_audit_request(
            path, source, request.method,
        ) and not is_service_path
        request_summary = {}
        response_summary = {}
        error_text = None
        response = None
        started_at = time.perf_counter()
        try:
            try:
                request_summary = await summarize_audit_request(
                    request,
                    should_audit,
                )
            except Exception:
                request_summary = {'captured': False}
            session_required = (
                not _is_public_path(path, request.method)
                and not _is_service_authenticated_path(path, request.method)
            )
            if auth_required and session_required and not current_user:
                response = JSONResponse(
                    content={
                        'success': False,
                        'error': 'Authentication required',
                        'detail': 'Authentication required',
                    },
                    status_code=401,
                )
            else:
                csrf_error = csrf_rejection_reason(request)
                if csrf_error:
                    response = JSONResponse(
                        content={
                            'success': False,
                            'error': csrf_error,
                            'detail': csrf_error,
                        },
                        status_code=403,
                    )
                else:
                    response = await call_next(request)
        except Exception as exc:
            error_text = str(exc)
            raise
        finally:
            status_code = response.status_code if response else 500
            final_audit = should_audit or (
                status_code >= 400 and can_audit_path(path)
            )
            if final_audit and response:
                try:
                    response, response_summary = await summarize_audit_response(
                        response
                    )
                except Exception:
                    response_summary = {'captured': False}
            if final_audit:
                try:
                    client_id = get_client_id_from_request(request)
                    username, client_ip = parse_client_id(client_id)
                except Exception:
                    client_ip = get_client_ip(request)
                    username = 'unknown'
                    client_id = f'{username}@{client_ip}'
                with suppress(Exception):
                    security_audit_logger.log_event(
                        {
                            'action_type': (
                                'api' if path.startswith('/api/') else 'page_visit'
                            ),
                            'source': source,
                            'operation': get_audit_operation(
                                path,
                                request.method,
                            ),
                            'method': request.method,
                            'path': sanitize_audit_path(path),
                            'query': security_audit_logger.sanitize_mapping(
                                dict(request.query_params)
                            ),
                            'request_summary': request_summary,
                            'response_summary': response_summary,
                            'status_code': status_code,
                            'duration_ms': round(
                                (time.perf_counter() - started_at) * 1000,
                                2,
                            ),
                            'client_ip': client_ip,
                            'client_id': client_id,
                            'username': username,
                            'user_agent': request.headers.get(
                                'user-agent',
                                '',
                            )[:300],
                            'error': error_text,
                            'request_id': request_id,
                            'trace_id': trace_id,
                            'device_claims': list(
                                getattr(
                                    request.state,
                                    'device_lease_tokens',
                                    [],
                                )
                                or []
                            ),
                        }
                    )
            with suppress(Exception):
                observe_request(
                    request,
                    status_code,
                    time.perf_counter() - started_at,
                )
        response.headers['X-Request-ID'] = request_id
        response.headers['X-Trace-ID'] = trace_id
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Permissions-Policy'] = (
            'camera=(), microphone=(), geolocation=(), payment=(), usb=(self)'
        )
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'self'; frame-src 'self'; form-action 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
            "font-src 'self' data:; connect-src 'self' https: ws: wss:; "
            "media-src 'self' blob:; worker-src 'self' blob:"
        )
        if request.url.scheme == 'https':
            response.headers['Strict-Transport-Security'] = (
                'max-age=31536000; includeSubDomains'
            )
        if path.startswith('/static/'):
            response.headers['Cache-Control'] = (
                'public, max-age=300, must-revalidate'
            )
        elif path.startswith('/api/'):
            response.headers['Cache-Control'] = (
                'no-cache, no-store, must-revalidate'
            )
        return response

    static_dir = services.settings.project_root / 'web/static'
    if static_dir.exists():
        app.mount('/static', StaticFiles(directory=static_dir), name='static')
    templates = Jinja2Templates(
        directory=services.settings.project_root / 'web/shell'
    )
    templates.env.globals['url_for'] = (
        lambda endpoint, filename='': (
            f'/static/{filename}' if endpoint == 'static' else f'/{endpoint}'
        )
    )
    include_routes(app, templates, services)

    def secured_openapi():
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        schemes = schema.setdefault('components', {}).setdefault(
            'securitySchemes',
            {},
        )
        schemes['SessionCookie'] = {
            'type': 'apiKey',
            'in': 'cookie',
            'name': AUTH_COOKIE_NAME,
            'description': 'Authenticated browser session cookie.',
        }
        schemes['ServiceBearer'] = {
            'type': 'http',
            'scheme': 'bearer',
            'description': 'Worker or trusted webhook service credential.',
        }
        schema['security'] = [{'SessionCookie': []}]
        for route_path, path_item in schema.get('paths', {}).items():
            for method, operation in path_item.items():
                if method.upper() not in {
                    'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'
                } or not isinstance(operation, dict):
                    continue
                if _is_public_path(route_path, method.upper()):
                    operation['security'] = []
                elif _is_service_authenticated_path(route_path, method.upper()):
                    operation['security'] = [{'ServiceBearer': []}]
        app.openapi_schema = schema
        return schema

    app.openapi = secured_openapi
    return app
