"""Auth feature constants shared by service.py and agent_tokens.py.

Leaf module so agent_tokens.py can import them without a circular import with
service.py (which mixes the AgentTokenServiceMixin in).

Also hosts the ``CurrentUser`` principal dataclass (merged from the former
``principal.py`` split) — it only depends on ``ROLE_PERMISSIONS`` above.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Agent Service Token principal role (ADR 0006): agent tokens are
# not a role ladder step; their power comes entirely from AGENT_SCOPES.
AGENT_ROLE = "agent_service"

# Agent Service Token scopes (ADR 0006); permission-vocabulary reuse,
# ``has_permission`` composes, require_role never matches an agent principal.
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
    # ADR 0014: background-only external knowledge (never root-cause evidence).
    "knowledge.read": "search read-only external Android knowledge sources",
    # Build orchestration scopes (ADR 0006 least-privilege): creating or
    # driving a build job eventually executes shell commands on build
    # servers, so agents must be granted these explicitly — a valid token
    # with zero scopes is rejected by the build API.
    "build.read": "discover build servers/workspaces/lunch options",
    "build.execute": "create, start, poll and supply passwords for build jobs",
    "build.cancel": "cancel or delete build jobs",
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
        "knowledge.read",  # ADR 0014 背景知识只读检索
        "email.send",
        "build.read",  # Build 面向人类操作员开放;agent token 需显式 build.* scope。
        "build.execute",
        "build.cancel",
    }),
    "device_operator": frozenset({
        "tests.execute",
        "tests.cancel",  # see "user" above
        "jobs.read",
        "reports.read",  # see "user" above
        "devices.use_leased",
        "devices.inventory",
        "devices.lease", "devices.read",  # ATS 能力并集（ADR 0012）需要两者
        "redmine.read",
        "artifacts.read_own",
        "apk.analyze_own",
        "sdk.read",
        "knowledge.read",
        "email.send",
        "build.read",
        "build.execute",
        "build.cancel",
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


@dataclass(frozen=True)
class CurrentUser:
    id: str
    username: str
    role: str
    display_name: str = ""
    # Agent Service Token scopes granted to this principal beyond the role
    # defaults. Human principals keep this empty; role-based admin gates are
    # decided by ``role`` and never lifted by scopes.
    extra_permissions: frozenset[str] = frozenset()
    # Actor vs resource owner (ADR 0010): ``id`` is the ACTING principal
    # (audit identity). ``resource_owner_id`` is the account that owns
    # resources created through this principal. Human principals: both are
    # the account id. Agent principals: ``id`` is the synthetic
    # ``agent:<token_id>`` (stable for audit), ``resource_owner_id`` is the
    # token's enrolling account so resources survive token rotation
    # instead of becoming orphans.
    resource_owner_id: str = ""

    def __post_init__(self) -> None:
        if not self.resource_owner_id:
            # dataclass(frozen=True) — use object.__setattr__ for the default.
            object.__setattr__(self, "resource_owner_id", self.id)

    @property
    def actor_id(self) -> str:
        """Audit identity of the acting principal (alias of ``id``)."""
        return self.id

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "display_name": self.display_name,
            "permissions": sorted(self.effective_permissions()),
            "resource_owner_id": self.resource_owner_id,
        }

    def effective_permissions(self) -> frozenset[str]:
        return ROLE_PERMISSIONS.get(self.role, frozenset()) | set(self.extra_permissions)

    def has_permission(self, permission: str) -> bool:
        return "*" in self.effective_permissions() or permission in self.effective_permissions()
