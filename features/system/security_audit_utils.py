"""Helpers for request/response security audit summarization."""

import json
import re
from typing import Any

from fastapi import Request
from fastapi.responses import Response

from features.system.security_audit import security_audit_logger


AUDIT_SKIP_PREFIXES = (
    '/static/',
    '/novnc/',
    '/api/security-audit/page-view',
    '/api/security-audit/logs',
    '/api/security-audit/detail',
    '/api/security-audit/export',
    '/api/notifications',
)
AUDIT_SKIP_PATHS = {'/favicon.ico'}
AUDIT_WEB_READONLY_NOISE_PATHS = {
    '/',
    '/templates/architecture.html',
    '/api/config/ai',
    '/api/config/opengrok',
    '/api/config/read',
    '/api/config/redmine',
    '/api/desktop/vnc/status',
    '/api/devices/list',
    '/api/devices/management',
    '/api/devices/user-locked',
    '/api/reports/list',
    '/api/ssh/sshd',
    '/api/system/docs',
    '/api/system/health',
    '/api/system/help',
    '/api/system/skills',
    '/api/agent/install.sh',
    '/api/test/logs/get',
    '/api/test/logs/list',
    '/api/test/status',
    '/api/test/suites',
    '/api/test/suites/files',
    '/api/websites/load',
    '/api/usbip/status',
    '/api/users/current',
    '/api/users/list',
    '/api/users/workspace-context',
    '/api/vpn/status',
    # 高频只读集群轮询不记录常规审计。
    '/api/cluster/status',
    '/api/cluster/workers',
    '/api/automation/worker/status',
}
AUDIT_WEB_READONLY_NOISE_PREFIXES = (
    '/api/favicon/',
)
# 高频只读状态轮询的参数化路径（任务详情/日志轮询、命令结果轮询）。
# 与精确名单同语义：仅跳过 web GET/HEAD 的常规审计，失败仍会记录。
AUDIT_WEB_READONLY_NOISE_PATTERNS = (
    re.compile(r'^/api/cluster/jobs/[^/]+$'),
    re.compile(r'^/api/cluster/jobs/[^/]+/events$'),
    re.compile(r'^/api/cluster/commands/[^/]+$'),
)
AUDIT_PAGE_VIEW_SKIP_PAGES = {'security-audit'}

MAX_AUDIT_REQUEST_BODY_BYTES = 64 * 1024
MAX_AUDIT_RESPONSE_BODY_BYTES = 128 * 1024
_FIRMWARE_SHARE_DOWNLOAD_PATH = re.compile(
    r'^(/api/firmware-shares/)[^/]+(/download)$'
)

# R09（2026-09-08 审核）：这些认证入口的请求/响应正文携带一次性配对码
# 或 Agent Service Token。现有递归脱敏只按"键名"匹配（token/password
# 等），而配对码放在 `enrollment.code` / 请求体的 `code` 键下，会原样
# 进入审计。对这些路径记录不解析的占位标记；其他业务接口不受影响，
# 避免把所有 `code` 字段一刀切屏蔽。
_AUDIT_CREDENTIAL_BODY_PATHS = frozenset({
    '/api/auth/agent-enroll',
    '/api/auth/agent-enrollment-codes',
})
AUDIT_CREDENTIAL_BODY_REDACTED = {
    'captured': True,
    'redacted': True,
    'reason': '认证凭据正文不记录',
}


def can_audit_path(path: str) -> bool:
    if path in AUDIT_SKIP_PATHS:
        return False
    return not any(path.startswith(prefix) for prefix in AUDIT_SKIP_PREFIXES)


def should_audit_request(path: str, source: str, method: str) -> bool:
    if not can_audit_path(path):
        return False

    method_upper = method.upper()
    if source == 'web' and method_upper in {'GET', 'HEAD'}:
        if path in AUDIT_WEB_READONLY_NOISE_PATHS:
            return False
        if any(path.startswith(prefix) for prefix in AUDIT_WEB_READONLY_NOISE_PREFIXES):
            return False
        if any(pattern.fullmatch(path) for pattern in AUDIT_WEB_READONLY_NOISE_PATTERNS):
            return False

    return True


def get_audit_operation(path: str, method: str) -> str:
    if path == '/':
        return '打开Web首页'
    return f"{method} {path}"


def sanitize_audit_path(path: str) -> str:
    """Hide bearer-style identifiers that are carried in URL path segments."""

    return _FIRMWARE_SHARE_DOWNLOAD_PATH.sub(
        r'\1***REDACTED***\2',
        str(path or ''),
    )


def _is_credential_body_path(path: str) -> bool:
    return str(path or '') in _AUDIT_CREDENTIAL_BODY_PATHS


def safe_int(value: str | None, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


async def summarize_audit_request(request: Request, should_audit: bool) -> dict[str, Any]:
    """Build a safe request summary without recording file contents or secrets."""
    if not should_audit:
        return {}

    content_type = (request.headers.get('content-type') or '').lower()
    content_length = safe_int(request.headers.get('content-length'))
    summary = {
        'content_type': content_type.split(';')[0] if content_type else '',
        'content_length': content_length,
        'query': security_audit_logger.sanitize_mapping(dict(request.query_params)),
    }

    if request.method.upper() not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        return summary

    if _is_credential_body_path(request.url.path):
        # 不读取/不解析正文，避免配对码进入审计；也不需要回放包装。
        summary['body'] = dict(AUDIT_CREDENTIAL_BODY_REDACTED)
        return summary

    if 'multipart/form-data' in content_type:
        summary['body'] = {'body_type': 'multipart', 'captured': False, 'reason': '文件上传内容不记录'}
        return summary

    if content_length > MAX_AUDIT_REQUEST_BODY_BYTES:
        summary['body'] = {'captured': False, 'reason': f'请求体超过 {MAX_AUDIT_REQUEST_BODY_BYTES} 字节'}
        return summary

    if 'application/json' not in content_type and 'application/x-www-form-urlencoded' not in content_type:
        return summary

    body = await request.body()
    body_sent = False

    async def replay_body():
        nonlocal body_sent
        if body_sent:
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        body_sent = True
        return {'type': 'http.request', 'body': body, 'more_body': False}

    request._receive = replay_body

    if 'application/json' in content_type:
        summary['body'] = security_audit_logger.summarize_json_body(body)
    else:
        summary['body'] = security_audit_logger.summarize_form_body(body)
    return summary


async def summarize_audit_response(
    response,
    path: str = '',
) -> tuple[Any, dict[str, Any]]:
    """Capture small JSON responses for audit detail and rebuild the response.

    R09（2026-09-08 审核）：认证入口（配对码兑换/签发）的响应正文换成
    占位标记，原始配对码与 Service Token 不进入审计，但响应本身仍正常
    重建返回给客户端。
    """
    if response is None:
        return response, {}

    content_type = (response.headers.get('content-type') or '').lower()
    content_encoding = (response.headers.get('content-encoding') or '').lower()
    content_length = safe_int(response.headers.get('content-length'))
    redact_body = _is_credential_body_path(path)

    summary = {
        'content_type': content_type.split(';')[0] if content_type else '',
        'content_length': content_length,
    }

    if 'application/json' not in content_type or content_encoding:
        return response, summary
    if content_length > MAX_AUDIT_RESPONSE_BODY_BYTES:
        summary.update({'captured': False, 'reason': '响应体过大'})
        return response, summary
    if not hasattr(response, 'body_iterator'):
        return response, summary

    body_parts = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, str):
            chunk = chunk.encode('utf-8')
        body_parts.append(chunk)

    body = b''.join(body_parts)
    summary['content_length'] = len(body)
    if redact_body:
        summary['body'] = dict(AUDIT_CREDENTIAL_BODY_REDACTED)
    else:
        try:
            parsed = json.loads(body.decode('utf-8'))
            if isinstance(parsed, dict):
                summary['body'] = security_audit_logger.sanitize_mapping(parsed)
            else:
                summary['body'] = security_audit_logger.sanitize_value('response', parsed)
        except Exception as e:
            summary['parse_error'] = str(e)
            summary['preview'] = security_audit_logger.sanitize_value(
                'preview',
                body[:300].decode('utf-8', errors='replace'),
            )

    headers = dict(response.headers)
    headers.pop('content-length', None)
    rebuilt = Response(
        content=body,
        status_code=response.status_code,
        headers=headers,
        media_type=response.media_type
    )
    return rebuilt, summary
