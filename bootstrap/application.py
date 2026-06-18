from __future__ import annotations

import json
import os
import time
from contextlib import suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from bootstrap.dependencies import AppServices, build_services
from bootstrap.lifecycle import create_lifespan
from bootstrap.routes import include_routes
from core.clients import get_client_id_from_request, get_client_ip, parse_client_id
from core.security_audit import classify_request_source, security_audit_logger
from core.security_audit_utils import (
    can_audit_path,
    get_audit_operation,
    should_audit_request,
    summarize_audit_request,
    summarize_audit_response,
)


class UTF8JSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            separators=(',', ':'),
        ).encode('utf-8')


def _csv_env(name: str, default: str) -> list[str]:
    values = [
        value.strip()
        for value in os.getenv(name, default).split(',')
        if value.strip()
    ]
    return values or [default]


def create_app(services: AppServices | None = None) -> FastAPI:
    services = build_services() if services is None else services
    app = FastAPI(
        title='GMS Auto Test - FastAPI Server (Port 5001)',
        description='完整的测试管理服务',
        version='4.0.0',
        lifespan=create_lifespan(services),
        default_response_class=UTF8JSONResponse,
    )
    app.state.services = services

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_csv_env('CORS_ORIGINS', '*'),
        allow_credentials=os.getenv(
            'CORS_ALLOW_CREDENTIALS',
            'false',
        ).strip().lower()
        == 'true',
        allow_methods=['*'],
        allow_headers=['*'],
        expose_headers=['*'],
    )
    trusted_hosts = _csv_env('TRUSTED_HOSTS', '*')
    if trusted_hosts != ['*']:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)
    app.add_middleware(GZipMiddleware, minimum_size=500)

    @app.middleware('http')
    async def audit_and_security_headers(request, call_next):
        path = request.url.path
        source = classify_request_source(
            request.headers.get('user-agent', ''),
            path,
        )
        should_audit = should_audit_request(path, source, request.method)
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
                            'path': path,
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
                        }
                    )
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        if path.startswith('/static/'):
            response.headers['Cache-Control'] = (
                'public, max-age=86400, immutable'
            )
        elif path.startswith('/api/'):
            response.headers['Cache-Control'] = (
                'no-cache, no-store, must-revalidate'
            )
        return response

    static_dir = services.settings.project_root / 'static'
    if static_dir.exists():
        app.mount('/static', StaticFiles(directory=static_dir), name='static')
    templates = Jinja2Templates(
        directory=services.settings.project_root / 'templates'
    )
    templates.env.globals['url_for'] = (
        lambda endpoint, filename='': (
            f'/static/{filename}' if endpoint == 'static' else f'/{endpoint}'
        )
    )
    include_routes(app, templates, services)
    return app
