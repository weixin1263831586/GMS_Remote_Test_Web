"""Auth feature constants shared by service.py and agent_tokens.py.

Kept in a leaf module so agent_tokens.py can import them without a circular
import with service.py (which mixes the AgentTokenServiceMixin in).
"""

from __future__ import annotations


# Agent Service Token principal role (ADR 0006): agent tokens are
# not a role ladder step; their power comes entirely from AGENT_SCOPES.
AGENT_ROLE = "agent_service"

# Agent Service Token scopes (ADR 0006). Scopes reuse the
# platform permission vocabulary so ``has_permission`` composes naturally;
# role-based admin gates (require_role) never match an agent principal.
AGENT_SCOPES: dict[str, str] = {
    "devices.read": "read device inventory",
    "devices.lease": "lease/claim devices",
    "devices.use_leased": "operate on leased devices",
    "devices.inventory": "device inventory management (device_operator level)",
    "tests.execute": "start test jobs",
    "tests.cancel": "cancel own test jobs",
    "jobs.read": "read durable job status/events",
    "reports.read": "read finished test reports",
    # Redmine evidence / APK analysis / SDK source scopes (ADR 0006).
    # Read-only analysis chain; Redmine stays GET-only this phase.
    "redmine.read": "read Redmine issues/attachments visible to the owner identity",
    "artifacts.read_own": "read own evidence artifacts and derived text",
    "apk.analyze_own": "run JADX analysis on own artifacts and read results",
    "sdk.read": "search/read admin-configured SDK source providers",
}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "user": frozenset({
        "tests.execute",
        # cancel/list formalize the operator UI so the tests.cancel
        # / jobs.read API gates keep accepting humans while agents need scopes.
        "tests.cancel",
        "jobs.read",
        "reports.read",
        "devices.use_leased",
        # Human operators use the evidence/APK/SDK readers through their own
        # Redmine identity and per-owner storage (ADR 0006); agent
        # tokens must be granted the matching scopes explicitly.
        "redmine.read",
        "artifacts.read_own",
        "apk.analyze_own",
        "sdk.read",
    }),
    "device_operator": frozenset({
        "tests.execute",
        "tests.cancel",  # see "user" above
        "jobs.read",
        "reports.read",  # see "user" above
        "devices.use_leased",
        "devices.inventory",
        "devices.lease",
        "redmine.read",
        "artifacts.read_own",
        "apk.analyze_own",
        "sdk.read",
    }),
    "admin": frozenset({"*"}),
    "worker_service": frozenset({
        "worker.register",
        "worker.heartbeat",
        "worker.commands",
        "worker.artifacts",
    }),
    AGENT_ROLE: frozenset(),
}


DEFAULT_AGENT_TOKEN_DAYS = 90
APPROVAL_TOKEN_TTL_SECONDS = 300

# ``CurrentUser`` moved to ``principal.py``; re-exported here for the
# historical ``from .constants import CurrentUser`` import paths.
from .principal import CurrentUser  # noqa: E402,F401

