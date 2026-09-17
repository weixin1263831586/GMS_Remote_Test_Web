"""Machine authority for background orchestration (ADR 0012).

A durable pipeline (ATS AutomationRun) advances stage-by-stage inside a
background worker thread. It must be able to reach the same capability
gated Feature APIs a human operator would click, but it must never inherit
MORE authority than the human who created the run, and it must never be
modeled as an admin cookie/token riding a loopback HTTP request.

Three invariants enforced here:

1. The machine principal is derived from the run's ``created_by`` account:
   ``resource_owner_id`` is the human account, so every owner-scoped API
   (cluster jobs, reports, device leases) resolves to exactly the resources
   the creating user already owns.
2. The machine principal carries ONLY the capability union snapshotted from
   the run plan at creation time (``granted_capabilities_json``) plus the
   fixed ``MACHINE_PERMISSIONS`` floor it needs to execute its own stages.
   An orchestration service can never grant itself anything the creating
   principal did not consent to.
3. The capability travels as a short-TTL HMAC-signed bearer token minted
   in-process. It never touches the agent-token store, cannot be minted by
   any HTTP caller (no endpoint accepts a signing request), and expires on
   its own, so no long-lived "worker admin token" exists to leak.

Any HTTP endpoint reachable with a machine capability must treat it as
non-human: elevation, cookie sessions and CSRF browser semantics do not
apply, while every owner/scope check applies unchanged.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from foundation.secrets import derive_application_key

from .constants import CurrentUser


CAPABILITY_TOKEN_PREFIX = "gmscap_v1_"
_CAPABILITY_TTL_SECONDS = 2 * 60 * 60
_HMAC_PURPOSE = b"gms-machine-authority-v1:"

# Fixed execution floor for automation workers (ADR 0012). These are the
# capabilities the pipeline needs to advance its own stages regardless of
# plan contents. Everything plan-dependent (build.execute, devices.lease,
# tests.execute, firmware.stage) must come from the run's granted snapshot.
MACHINE_PERMISSIONS: frozenset[str] = frozenset({
    "jobs.read",
    "reports.read",
})

# Capabilities a run plan may request for its stages. The creator must hold
# each requested capability at creation time; the union is snapshotted onto
# the run so later role/scope changes cannot silently widen an in-flight run.
AUTOMATION_PLAN_CAPABILITIES: tuple[str, ...] = (
    "build.read",
    "build.execute",
    "build.cancel",
    "devices.read",
    "devices.lease",
    "devices.use_leased",
    "tests.execute",
    "tests.cancel",
    "firmware.stage",
)


class MachineAuthorityError(ValueError):
    """Raised when a machine capability token is malformed or stale."""


def automation_granted_capabilities(
    principal: CurrentUser | None,
    test_plan: dict[str, Any] | None,
) -> list[str]:
    """Compile the plan's capability union the creator must already hold.

    Stage → capability mapping (ADR 0012): build stages need
    ``build.execute``, device reservation needs ``devices.lease``, test
    stages need ``tests.execute`` and firmware flashing needs the explicit
    ``firmware.stage`` capability. Anonymous/dev-mode creators get an empty
    union (same trust level as the caller).
    """

    if principal is None:
        return []
    plan = test_plan if isinstance(test_plan, dict) else {}
    granted: list[str] = []
    # Executor semantics (executors.flash): ONLY flash.mode == "skip" skips
    # flashing — a missing/empty flash plan defaults to firmware flashing.
    # The capability compiler must mirror that exactly, otherwise a bare
    # {"test_type": "CTS"} plan passes preflight/create but the flash stage
    # later fails with 403 firmware.stage (preflight/create/worker must all
    # agree through this single compiler, ADR 0012).
    flash = plan.get("flash")
    flash_mode = flash.get("mode") if isinstance(flash, dict) else None
    if flash_mode != "skip":
        granted.append("firmware.stage")
    if isinstance(plan.get("build"), dict):
        granted.append("build.execute")
        granted.append("build.cancel")
        granted.append("build.read")
    granted.append("devices.lease")
    granted.append("devices.read")
    granted.append("devices.use_leased")
    granted.append("tests.execute")
    granted.append("tests.cancel")
    granted = [name for name in AUTOMATION_PLAN_CAPABILITIES if name in set(granted)]
    missing = [
        name for name in granted
        if not principal.has_permission(name)
    ]
    if missing:
        raise MachineAuthorityError(
            "Automation plan requires capabilities the caller does not hold: "
            + ", ".join(sorted(set(missing)))
        )
    return granted


def automation_authority(
    run_id: str,
    created_by: str,
    granted_capabilities: list[str] | str | None = None,
) -> CurrentUser:
    """Build the machine principal that executes one automation run.

    ``created_by`` is the immutable platform account id stored on the run;
    all resources touched through this principal are owned by that account
    exactly as if the user had clicked through the Feature APIs by hand.
    """

    if isinstance(granted_capabilities, str):
        granted = [
            part.strip()
            for part in granted_capabilities.split(",")
            if part.strip()
        ]
    else:
        granted = [str(item) for item in (granted_capabilities or []) if str(item)]
    known = set(AUTOMATION_PLAN_CAPABILITIES) | set(MACHINE_PERMISSIONS)
    return CurrentUser(
        id=f"automation:{run_id}",
        username=f"automation:{run_id}",
        role="agent_service",
        display_name=f"Automation run {run_id}",
        extra_permissions=frozenset(
            name for name in granted if name in known
        ) | MACHINE_PERMISSIONS,
        resource_owner_id=str(created_by or ""),
    )


def _signing_key() -> bytes:
    return derive_application_key(_HMAC_PURPOSE)


def _encode_field(value: str) -> str:
    """URL-safe Base64 for one variable token field (no ``.`` padding chars)."""

    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_field(value: str) -> str | None:
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None


def mint_capability_token(
    principal: CurrentUser,
    *,
    ttl_seconds: int = _CAPABILITY_TTL_SECONDS,
) -> str:
    """Mint a short-TTL signed bearer token FOR a machine principal.

    Called in-process by the automation worker only — no HTTP surface can
    mint tokens. The signed payload carries the FULL capability snapshot:
    ``verify_capability_token`` must be able to rebuild the exact principal
    the worker started with. Signing only principal/owner ids and
    recomputing capabilities at verify time silently strips every plan
    capability (build.execute, devices.lease, tests.execute,
    firmware.stage) — the run then 403s mid-workflow even though creation
    passed. Variable fields are URL-safe Base64 so ids containing ``.``
    (agent ids, e-mail-like account names) cannot corrupt the framing.
    """

    principal_id = str(principal.id or "")
    owner_id = str(principal.resource_owner_id or "")
    if not principal_id.startswith("automation:"):
        raise MachineAuthorityError(
            "capability tokens can only be minted for automation principals"
        )
    if not owner_id:
        raise MachineAuthorityError(
            "capability tokens require the creating account (resource owner)"
        )
    expires = int(time.time()) + max(60, int(ttl_seconds))
    nonce = secrets.token_hex(8)
    capabilities = sorted(
        str(item) for item in (principal.extra_permissions or frozenset())
    )
    payload = ".".join([
        _encode_field(str(expires)),
        _encode_field(nonce),
        _encode_field(principal_id),
        _encode_field(owner_id),
        _encode_field(json.dumps(capabilities, separators=(",", ":"))),
    ])
    digest = hmac.new(
        _signing_key(), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{CAPABILITY_TOKEN_PREFIX}{payload}.{digest}"


def verify_capability_token(token: str) -> CurrentUser | None:
    """Resolve a minted capability token back into its machine principal.

    Returns None for anything malformed, expired, or wrongly signed; never
    raises into request handling (an invalid capability is just anonymous).
    The signed capability snapshot is restored verbatim — verification must
    never widen it (unknown names are dropped by ``automation_authority``)
    nor narrow it to the MACHINE_PERMISSIONS floor.
    """

    value = str(token or "")
    if not value.startswith(CAPABILITY_TOKEN_PREFIX):
        return None
    parts = value[len(CAPABILITY_TOKEN_PREFIX):].split(".")
    if len(parts) != 6:
        return None
    payload = ".".join(parts[:5])
    digest = parts[5]
    expected = hmac.new(
        _signing_key(), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, digest):
        return None
    fields = [_decode_field(part) for part in parts[:5]]
    if any(field is None for field in fields):
        return None
    expires_raw, _nonce, principal_id, owner_id, capabilities_raw = fields
    try:
        expires = int(expires_raw)
    except ValueError:
        return None
    if expires < int(time.time()):
        return None
    if not principal_id.startswith("automation:") or not owner_id:
        return None
    try:
        capabilities = json.loads(capabilities_raw)
    except ValueError:
        return None
    if not isinstance(capabilities, list):
        return None
    return automation_authority(
        principal_id[len("automation:"):],
        owner_id,
        [str(item) for item in capabilities],
    )


def is_machine_principal(user: Any) -> bool:
    """Whether this principal is an in-process automation authority."""

    return bool(
        user is not None
        and str(getattr(user, "id", "") or "").startswith("automation:")
    )


def machine_has_permission(request: Any, permission: str) -> bool:
    """Gate helper for endpoints that admit machine principals.

    True only when the request carries a machine capability principal that
    holds ``permission``. Elevation-gated endpoints (firmware staging) use
    this to admit the signed capability while keeping the elevation
    requirement for every human session.
    """

    from .access import get_authenticated_user

    try:
        user = get_authenticated_user(request)
    except AttributeError:
        return False
    return is_machine_principal(user) and user.has_permission(permission)
