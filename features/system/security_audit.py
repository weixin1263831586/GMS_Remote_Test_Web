"""Security audit logging for web and CLI operations.

The implementation lives in :mod:`foundation.security_audit`; this module
re-exports it so existing ``features.system.security_audit`` import paths
keep working.
"""

from foundation.security_audit import (
    SecurityAuditLogger,
    classify_request_source,
    security_audit_logger,
)


__all__ = [
    "SecurityAuditLogger",
    "classify_request_source",
    "security_audit_logger",
]
